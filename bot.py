import os
import shutil
import asyncio
import logging
import subprocess
import re
from pathlib import Path
from dataclasses import dataclass, field

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
MAX_FILES = int(os.getenv("MAX_MEDIA_FILES", "20"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "20"))
WORK_DIR = Path(os.getenv("WORK_DIR", "/tmp/video_bot"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO
)
log = logging.getLogger("auto-video-bot")

@dataclass
class Job:
    reference: Path | None = None
    clips: list[Path] = field(default_factory=list)
    soundtrack: Path | None = None

JOBS: dict[int, Job] = {}
LOCKS: dict[int, asyncio.Lock] = {}

def lock_for(uid: int) -> asyncio.Lock:
    return LOCKS.setdefault(uid, asyncio.Lock())

def run_ffmpeg(args: list[str]) -> None:
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode:
        raise RuntimeError(p.stderr[-2500:] or "FFmpeg failed.")

def probe_duration(path: Path) -> float:
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True
    )
    if p.returncode:
        raise RuntimeError(f"Could not read media duration: {path.name}")
    try:
        duration = float(p.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"Invalid media duration: {path.name}") from exc
    if duration <= 0:
        raise RuntimeError(f"Media has no usable duration: {path.name}")
    return duration

def scene_times(video: Path) -> list[float]:
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(video),
         "-vf", "select='gt(scene,0.30)',showinfo", "-an", "-f", "null", "-"],
        capture_output=True, text=True
    )
    times = [0.0]
    for match in re.finditer(r"pts_time:([0-9.]+)", p.stderr):
        t = float(match.group(1))
        if t > 0.35 and t - times[-1] > 0.45:
            times.append(t)
    duration = probe_duration(video)
    times = [t for t in times if t < duration - 0.25][:39]
    times.append(duration)
    return times

def build_edit(reference: Path, clips: list[Path], soundtrack: Path | None, output: Path) -> None:
    if not clips:
        raise RuntimeError("Upload at least one of your own video clips.")

    ref_duration = probe_duration(reference)
    cuts = scene_times(reference)
    target_durations = [cuts[i + 1] - cuts[i] for i in range(len(cuts) - 1)]
    if not target_durations:
        target_durations = [ref_duration]

    clip_durations = [probe_duration(c) for c in clips]
    segments = []
    idx = 0
    remaining = ref_duration
    while remaining > 0.15 and len(segments) < 80:
        src = clips[idx % len(clips)]
        src_dur = clip_durations[idx % len(clips)]
        wanted = min(target_durations[idx % len(target_durations)], remaining, src_dur)
        if wanted < 0.15:
            wanted = min(remaining, src_dur)
        segments.append((src, wanted))
        remaining -= wanted
        idx += 1

    if remaining > 0.15:
        raise RuntimeError("The reference is too long or has too many cuts for this first version.")

    filter_parts = []
    inputs = []
    input_offset = 1 if soundtrack else 0
    for i, (src, dur) in enumerate(segments):
        inputs += ["-i", str(src)]
        input_index = i + input_offset
        filter_parts.append(
            f"[{input_index}:v]trim=duration={dur:.4f},setpts=PTS-STARTPTS,"
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,fps=30,setsar=1,format=yuv420p"
            f"[v{i}]"
        )
    concat_inputs = "".join(f"[v{i}]" for i in range(len(segments)))
    filter_parts.append(f"{concat_inputs}concat=n={len(segments)}:v=1:a=0[outv]")

    args = ["ffmpeg", "-y"]
    if soundtrack:
        args += ["-stream_loop", "-1", "-i", str(soundtrack)]
    args += inputs
    args += ["-filter_complex", ";".join(filter_parts), "-map", "[outv]"]
    if soundtrack:
        args += ["-map", "0:a:0", "-af", "volume=1.0", "-shortest"]
    else:
        args += ["-an"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
             "-movflags", "+faststart"]
    if soundtrack:
        args += ["-c:a", "aac", "-b:a", "192k"]
    args += [str(output)]
    run_ffmpeg(args)

async def download_media(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix: str) -> Path:
    message = update.effective_message
    media = message.video or message.document
    if not media:
        raise RuntimeError("Please send a video file.")
    size = getattr(media, "file_size", 0) or 0
    if size > MAX_UPLOAD_MB * 1024 * 1024:
        raise RuntimeError(f"That file is larger than this bot's {MAX_UPLOAD_MB} MB limit.")
    tg_file = await context.bot.get_file(media.file_id)
    uid = update.effective_user.id
    user_dir = WORK_DIR / str(uid)
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / f"{prefix}_{len(list(user_dir.iterdir()))}.media"
    await tg_file.download_to_drive(custom_path=path)
    return path

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    shutil.rmtree(WORK_DIR / str(uid), ignore_errors=True)
    JOBS[uid] = Job()
    await update.message.reply_text(
        "🎬 Automatic Video Editor\n\n"
        "1) Send one reference video (the edit whose rhythm you want to follow).\n"
        "2) Then send your own video clips, one by one.\n"
        "3) Optionally send your own soundtrack as an audio file.\n"
        "4) Send /done to render your edit.\n\n"
        "The bot analyzes the reference duration and approximate scene-cut timing. "
        "It uses your clips in the final video; it does not copy the reference footage."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/start — start a new edit\n"
        "/done — render using uploaded clips\n"
        "/cancel — delete this session\n\n"
        "Upload order: reference video → your clips → optional soundtrack → /done."
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    async with lock_for(uid):
        JOBS.pop(uid, None)
        shutil.rmtree(WORK_DIR / str(uid), ignore_errors=True)
    await update.message.reply_text("Session cleared. Send /start to begin again.")

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    job = JOBS.setdefault(uid, Job())
    async with lock_for(uid):
        try:
            if job.reference is None:
                path = await download_media(update, context, "reference")
                job.reference = path
                await update.message.reply_text(
                    "✅ Reference received. Now send your own video clips."
                )
            else:
                if len(job.clips) >= MAX_FILES:
                    await update.message.reply_text(f"Maximum {MAX_FILES} clips per edit.")
                    return
                path = await download_media(update, context, "clip")
                job.clips.append(path)
                await update.message.reply_text(
                    f"✅ Your clip {len(job.clips)} received. Send more clips or /done."
                )
        except Exception as e:
            await update.message.reply_text(f"Upload error: {e}")

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    async with lock_for(uid):
        try:
            msg = update.effective_message
            media = msg.audio
            if not media:
                return
            size = getattr(media, "file_size", 0) or 0
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                await msg.reply_text(f"Audio exceeds the {MAX_UPLOAD_MB} MB limit.")
                return
            tg_file = await context.bot.get_file(media.file_id)
            user_dir = WORK_DIR / str(uid)
            user_dir.mkdir(parents=True, exist_ok=True)
            path = user_dir / "soundtrack.audio"
            await tg_file.download_to_drive(custom_path=path)
            JOBS.setdefault(uid, Job()).soundtrack = path
            await msg.reply_text("🎵 Soundtrack received. Send /done when your clips are ready.")
        except Exception as e:
            await update.effective_message.reply_text(f"Audio upload error: {e}")

async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    job = JOBS.get(uid)
    if not job or not job.reference:
        await update.message.reply_text("Send /start, then upload a reference video first.")
        return
    if not job.clips:
        await update.message.reply_text("Upload at least one of your own video clips first.")
        return

    async with lock_for(uid):
        status = await update.message.reply_text("⏳ Analyzing reference and editing your clips…")
        output = WORK_DIR / str(uid) / "final_edit.mp4"
        try:
            await asyncio.to_thread(build_edit, job.reference, job.clips, job.soundtrack, output)
            await status.edit_text("✅ Edit complete. Sending your MP4…")
            with output.open("rb") as f:
                await update.message.reply_video(
                    video=f, caption="🎬 Your automatic edit is ready!",
                    supports_streaming=True, read_timeout=300, write_timeout=300
                )
        except Exception as e:
            log.exception("Render failed")
            await status.edit_text(
                "❌ Rendering failed. Try shorter clips or fewer files.\n"
                f"Details: {str(e)[:900]}"
            )
        finally:
            shutil.rmtree(WORK_DIR / str(uid), ignore_errors=True)
            JOBS.pop(uid, None)

def main() -> None:
    webhook_secret = os.environ.get("WEBHOOK_SECRET", "")
    external_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL", "")
    if not webhook_secret:
        raise RuntimeError("WEBHOOK_SECRET is missing. Set it in Render Environment.")
    if not external_url:
        raise RuntimeError("RENDER_EXTERNAL_URL is missing. Deploy this app as a Render Web Service.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("done", done))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.AUDIO, handle_audio))

    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
        url_path=webhook_secret,
        webhook_url=f"{external_url.rstrip('/')}/{webhook_secret}",
        secret_token=webhook_secret,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()

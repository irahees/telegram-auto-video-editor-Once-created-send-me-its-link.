import os
import shutil
import asyncio
import logging
import subprocess
import re
import struct
import statistics
from pathlib import Path
from dataclasses import dataclass, field

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.environ["BOT_TOKEN"]
MAX_FILES = int(os.getenv("MAX_MEDIA_FILES", "20"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "20"))
WORK_DIR = Path(os.getenv("WORK_DIR", "/tmp/video_bot"))
OUTPUT_WIDTH = int(os.getenv("OUTPUT_WIDTH", "480"))
OUTPUT_HEIGHT = int(os.getenv("OUTPUT_HEIGHT", "854"))
OUTPUT_FPS = int(os.getenv("OUTPUT_FPS", "24"))
FFMPEG_THREADS = max(1, int(os.getenv("FFMPEG_THREADS", "1")))
WORK_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("auto-video-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

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
        raise RuntimeError(p.stderr[-2200:] or "FFmpeg failed.")

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
        ["ffmpeg", "-hide_banner", "-threads", str(FFMPEG_THREADS), "-filter_threads", "1",
         "-i", str(video), "-vf", "fps=3,scale=320:-2,select='gt(scene,0.30)',showinfo",
         "-an", "-f", "null", "-"],
        capture_output=True, text=True
    )
    duration = probe_duration(video)
    if p.returncode:
        log.warning("Scene analysis failed; using a single segment.")
        return [0.0, duration]
    times = [0.0]
    for match in re.finditer(r"pts_time:([0-9.]+)", p.stderr):
        t = float(match.group(1))
        if t > 0.30 and t - times[-1] > 0.35:
            times.append(t)
    times = [t for t in times if t < duration - 0.20][:23]
    times.append(duration)
    return times

def detect_beats(media: Path) -> list[float]:
    """Lightweight transient/RMS beat estimate; heuristic, not a music transcription engine."""
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(media), "-vn",
         "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    raw = p.stdout
    if p.returncode or len(raw) < 8000:
        return []
    samples = struct.unpack("<" + "h" * (len(raw) // 2), raw[:len(raw) // 2 * 2])
    rate = 8000
    window = 400  # 50 ms
    energy = []
    for start in range(0, len(samples) - window, window):
        chunk = samples[start:start + window]
        energy.append((sum(x * x for x in chunk) / window) ** 0.5)
    if len(energy) < 8:
        return []
    baseline = statistics.median(energy)
    if baseline <= 1:
        return []
    peaks = []
    last = -10
    for i in range(1, len(energy) - 1):
        value = energy[i]
        if value > max(baseline * 1.65, 500) and value >= energy[i - 1] and value > energy[i + 1]:
            t = i * window / rate
            if t - last >= 0.24:
                peaks.append(t)
                last = t
    return peaks[:240]

def align_cuts_to_beats(cuts: list[float], beats: list[float], duration: float) -> list[float]:
    if len(beats) < 3 or len(cuts) <= 2:
        return cuts
    result = [0.0]
    for cut in cuts[1:-1]:
        nearest = min(beats, key=lambda b: abs(b - cut))
        # Keep the visual cut timing unless a clear nearby beat exists.
        chosen = nearest if abs(nearest - cut) <= 0.14 else cut
        if chosen - result[-1] >= 0.30 and duration - chosen >= 0.20:
            result.append(chosen)
    result.append(duration)
    return result

def make_segments(reference: Path, clips: list[Path], ref_beats: list[float]) -> list[tuple[Path, float]]:
    ref_duration = probe_duration(reference)
    cuts = align_cuts_to_beats(scene_times(reference), ref_beats, ref_duration)
    durations = [cuts[i + 1] - cuts[i] for i in range(len(cuts) - 1)]
    clip_durations = [probe_duration(c) for c in clips]
    segments = []
    remaining = ref_duration
    idx = 0
    while remaining > 0.16 and len(segments) < 48:
        clip_index = idx % len(clips)
        src = clips[clip_index]
        available = clip_durations[clip_index]
        wanted = min(durations[idx % len(durations)], remaining, available)
        if wanted < 0.16:
            wanted = min(remaining, available)
        if wanted < 0.16:
            break
        segments.append((src, wanted))
        remaining -= wanted
        idx += 1
    if remaining > 0.20:
        raise RuntimeError("Your clips are too short for the reference duration. Upload longer clips or more clips.")
    return segments

def build_edit(reference: Path, clips: list[Path], soundtrack: Path | None, output: Path) -> None:
    if not clips:
        raise RuntimeError("Upload at least one of your own clips.")
    ref_beats = detect_beats(reference)
    segments = make_segments(reference, clips, ref_beats)
    segment_dir = output.parent / "segments"
    shutil.rmtree(segment_dir, ignore_errors=True)
    segment_dir.mkdir(parents=True, exist_ok=True)
    segment_files = []
    durations = []

    for i, (src, duration) in enumerate(segments):
        segment = segment_dir / f"segment_{i:03d}.mp4"
        # Small, restrained punch-in and consistent colour treatment; no face reshaping.
        vf = (
            f"scale={OUTPUT_WIDTH + 24}:{OUTPUT_HEIGHT + 24}:force_original_aspect_ratio=increase,"
            f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},fps={OUTPUT_FPS},setsar=1,"
            "eq=contrast=1.04:saturation=1.06:brightness=0.005,format=yuv420p"
        )
        run_ffmpeg([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-threads", str(FFMPEG_THREADS), "-filter_threads", "1",
            "-i", str(src), "-t", f"{duration:.4f}", "-vf", vf, "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-threads", str(FFMPEG_THREADS), "-pix_fmt", "yuv420p", str(segment)
        ])
        segment_files.append(segment)
        durations.append(duration)

    # A light dissolve on alternating boundaries approximates common reference transitions.
    # Keep transition length short to preserve the edit's pace on a free CPU instance.
    inputs = []
    for path in segment_files:
        inputs += ["-i", str(path)]
    filters = []
    if len(segment_files) == 1:
        video_map = "[0:v]"
    else:
        previous = "[0:v]"
        elapsed = durations[0]
        for i in range(1, len(segment_files)):
            trans = 0.12 if i % 2 == 0 else 0.045
            trans = min(trans, max(0.02, durations[i] * 0.25), max(0.02, elapsed * 0.25))
            offset = max(0.0, elapsed - trans)
            target = f"[mix{i}]"
            transition = "fade" if i % 2 == 0 else "smoothleft"
            filters.append(
                f"{previous}[{i}:v]xfade=transition={transition}:duration={trans:.3f}:offset={offset:.3f}{target}"
            )
            previous = target
            elapsed += durations[i] - trans
        video_map = previous

    if filters:
        filter_arg = ";".join(filters)
        if soundtrack:
            run_ffmpeg([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-threads", str(FFMPEG_THREADS), *inputs,
                "-stream_loop", "-1", "-i", str(soundtrack),
                "-filter_complex", filter_arg,
                "-map", video_map, "-map", f"{len(segment_files)}:a:0",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-threads", str(FFMPEG_THREADS), "-c:a", "aac", "-b:a", "128k",
                "-shortest", "-movflags", "+faststart", str(output)
            ])
        else:
            run_ffmpeg([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-threads", str(FFMPEG_THREADS), *inputs,
                "-filter_complex", filter_arg, "-map", video_map,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-threads", str(FFMPEG_THREADS), "-an", "-movflags", "+faststart", str(output)
            ])
    else:
        if soundtrack:
            run_ffmpeg([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-stream_loop", "-1", "-i", str(soundtrack), "-i", str(segment_files[0]),
                "-map", "1:v:0", "-map", "0:a:0", "-c:v", "copy", "-c:a", "aac",
                "-b:a", "128k", "-shortest", "-movflags", "+faststart", str(output)
            ])
        else:
            shutil.copy2(segment_files[0], output)
    shutil.rmtree(segment_dir, ignore_errors=True)

async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE, prefix: str) -> Path:
    message = update.effective_message
    media = message.video or message.document
    if not media:
        raise RuntimeError("Please send a video file.")
    size = getattr(media, "file_size", 0) or 0
    if size > MAX_UPLOAD_MB * 1024 * 1024:
        raise RuntimeError(f"File exceeds this bot's {MAX_UPLOAD_MB} MB limit.")
    tg_file = await context.bot.get_file(media.file_id)
    user_dir = WORK_DIR / str(update.effective_user.id)
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / f"{prefix}_{len(list(user_dir.iterdir()))}.media"
    await tg_file.download_to_drive(custom_path=path)
    return path

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    shutil.rmtree(WORK_DIR / str(uid), ignore_errors=True)
    JOBS[uid] = Job()
    await update.message.reply_text(
        "🎬 Reference-Style Video Editor\\n\\n"
        "1) Send one reference edit.\\n"
        "2) Send your own raw video clips.\\n"
        "3) Send your own soundtrack as an audio file (recommended).\\n"
        "4) Send /done to render.\\n\\n"
        "The editor analyses scene rhythm and estimates audio beat peaks in the reference. "
        "It uses your footage and your soundtrack in the final video. "
        "It adds restrained colour treatment and short transitions. "
        "Complex effects, exact text animations and every overlay cannot be reconstructed automatically."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "/start — new edit\\n/done — render\\n/cancel — clear session\\n\\n"
        "Upload order: reference → your clips → optional soundtrack → /done."
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
                job.reference = await download_video(update, context, "reference")
                await update.message.reply_text("✅ Reference received. Now send your own raw clips.")
            else:
                if len(job.clips) >= MAX_FILES:
                    await update.message.reply_text(f"Maximum {MAX_FILES} clips per edit.")
                    return
                path = await download_video(update, context, "clip")
                job.clips.append(path)
                await update.message.reply_text(f"✅ Raw clip {len(job.clips)} received. Send more or /done.")
        except Exception as exc:
            await update.message.reply_text(f"Upload error: {exc}")

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    async with lock_for(uid):
        try:
            media = update.effective_message.audio
            if not media:
                return
            size = getattr(media, "file_size", 0) or 0
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                await update.effective_message.reply_text(f"Audio exceeds the {MAX_UPLOAD_MB} MB limit.")
                return
            tg_file = await context.bot.get_file(media.file_id)
            user_dir = WORK_DIR / str(uid)
            user_dir.mkdir(parents=True, exist_ok=True)
            path = user_dir / "soundtrack.audio"
            await tg_file.download_to_drive(custom_path=path)
            JOBS.setdefault(uid, Job()).soundtrack = path
            await update.effective_message.reply_text("🎵 Your soundtrack is ready. Send /done when clips are uploaded.")
        except Exception as exc:
            await update.effective_message.reply_text(f"Audio upload error: {exc}")

async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    job = JOBS.get(uid)
    if not job or not job.reference:
        await update.message.reply_text("Send /start, then upload a reference video first.")
        return
    if not job.clips:
        await update.message.reply_text("Upload at least one raw clip first.")
        return
    async with lock_for(uid):
        status = await update.message.reply_text("⏳ Analysing cuts and beat rhythm, then building your edit…")
        output = WORK_DIR / str(uid) / "final_edit.mp4"
        try:
            await asyncio.to_thread(build_edit, job.reference, job.clips, job.soundtrack, output)
            await status.edit_text("✅ Edit complete. Sending your MP4…")
            with output.open("rb") as handle:
                await update.message.reply_video(
                    video=handle, caption="🎬 Your reference-style edit is ready!",
                    supports_streaming=True, read_timeout=300, write_timeout=300
                )
        except Exception as exc:
            log.exception("Render failed")
            await status.edit_text("❌ Rendering failed. Try fewer or shorter clips.\\n" + str(exc)[:900])
        finally:
            shutil.rmtree(WORK_DIR / str(uid), ignore_errors=True)
            JOBS.pop(uid, None)

def main() -> None:
    webhook_secret = os.environ.get("WEBHOOK_SECRET", "")
    external_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL", "")
    if not webhook_secret:
        raise RuntimeError("WEBHOOK_SECRET is missing. Set it in Render Environment.")
    if not external_url:
        raise RuntimeError("RENDER_EXTERNAL_URL is missing. Deploy as a Render Web Service.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("done", done))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.AUDIO, handle_audio))
    app.run_webhook(
        listen="0.0.0.0", port=int(os.getenv("PORT", "10000")),
        url_path=webhook_secret,
        webhook_url=f"{external_url.rstrip('/')}/{webhook_secret}",
        secret_token=webhook_secret, drop_pending_updates=True
    )

if __name__ == "__main__":
    main()

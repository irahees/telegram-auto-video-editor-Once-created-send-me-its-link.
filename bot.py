from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("auto-video-editor")
# HTTPX logs full Telegram Bot API URLs; suppress them to avoid exposing the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PORT = int(os.getenv("PORT", "10000"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "20"))
MAX_MEDIA_FILES = int(os.getenv("MAX_MEDIA_FILES", "20"))
MAX_JOB_SECONDS = int(os.getenv("MAX_JOB_SECONDS", "900"))
WORK_ROOT = Path(os.getenv("WORK_DIR", "/tmp/video-editor"))
CACHE = WORK_ROOT / "cache"
JOBS = WORK_ROOT / "jobs"
STYLES = WORK_ROOT / "styles"
for folder in (CACHE, JOBS, STYLES):
    folder.mkdir(parents=True, exist_ok=True)

FORMATS = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
}
ENCODER = None
ACTIVE_JOBS: dict[int, "EditJob"] = {}
USER_LOCKS: dict[int, asyncio.Lock] = {}
USER_STYLE: dict[int, dict] = {}
QUEUE_SEMAPHORE = asyncio.Semaphore(max(1, int(os.getenv("WORKERS", "1"))))


@dataclass
class EditJob:
    user_id: int
    chat_id: int
    root: Path
    reference: Optional[Path] = None
    media: list[Path] = field(default_factory=list)
    media_kinds: list[str] = field(default_factory=list)
    own_music: Optional[Path] = None
    music_source: Optional[str] = None
    format_name: str = "9:16"
    style: str = "reference"
    energy: float = 1.0
    timeline: dict = field(default_factory=dict)
    created: float = field(default_factory=time.monotonic)

    def clean(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def run_cmd(args: list[str], timeout: int = 180) -> str:
    proc = subprocess.run(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=timeout, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stdout[-2500:] or f"Command failed: {args[0]}")
    return proc.stdout


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def probe(path: Path) -> dict:
    raw = run_cmd([
        "ffprobe", "-v", "error", "-show_format", "-show_streams",
        "-of", "json", str(path),
    ])
    obj = json.loads(raw)
    streams = obj.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), {})
    duration = float(obj.get("format", {}).get("duration") or v.get("duration") or 0)
    return {
        "duration": duration, "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0), "has_video": bool(v),
        "has_audio": bool(a), "fps": v.get("avg_frame_rate", "0/1"),
    }


def extract_audio(source: Path, output: Path) -> bool:
    if not probe(source)["has_audio"]:
        return False
    run_cmd([
        "ffmpeg", "-y", "-v", "error", "-i", str(source), "-vn",
        "-ac", "1", "-ar", "22050", "-c:a", "pcm_s16le", str(output),
    ])
    return True


def analyze_music(audio: Path) -> dict:
    try:
        import librosa
        import numpy as np
        y, sr = librosa.load(str(audio), sr=22050, mono=True)
        if y.size == 0:
            return {"tempo_bpm": None, "beats": [], "onsets": []}
        tempo, frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
        onsets = librosa.onset.onset_detect(y=y, sr=sr, units="frames")
        bpm = float(np.asarray(tempo).reshape(-1)[0]) if np.size(tempo) else None
        return {
            "tempo_bpm": round(bpm, 2) if bpm else None,
            "beats": [round(float(x), 3) for x in librosa.frames_to_time(frames, sr=sr)[:1000]],
            "onsets": [round(float(x), 3) for x in librosa.frames_to_time(onsets, sr=sr)[:1000]],
        }
    except Exception as exc:
        log.warning("Music analysis unavailable: %s", exc)
        return {"tempo_bpm": None, "beats": [], "onsets": [], "warning": str(exc)[:200]}


def remap_shots_to_beats(timeline: dict, music: dict) -> dict:
    """Snap reference pacing boundaries to nearby beats when a replacement track permits it."""
    beats = music.get("beats") or []
    shots = timeline.get("shots") or []
    if not beats or len(shots) < 2:
        return timeline
    total = sum(float(s.get("duration", 0)) for s in shots)
    boundaries = [0.0]
    cursor = 0.0
    for shot in shots[:-1]:
        cursor += float(shot.get("duration", 0))
        nearby = min(beats, key=lambda beat: abs(float(beat) - cursor))
        if abs(float(nearby) - cursor) <= 0.18:
            cursor = float(nearby)
        cursor = max(boundaries[-1] + 0.45, min(cursor, total - 0.45))
        boundaries.append(cursor)
    boundaries.append(total)
    updated = []
    for start, end in zip(boundaries, boundaries[1:]):
        updated.append({"start": round(start, 3), "end": round(end, 3), "duration": round(end-start, 3)})
    result = dict(timeline)
    result["shots"] = updated
    result["replacement_music_sync"] = "approximate_nearby_beat_snap"
    return result


def scene_cuts(path: Path, duration: float) -> list[float]:
    log_text = run_cmd([
        "ffmpeg", "-hide_banner", "-i", str(path), "-vf",
        "fps=2,scale=320:-2,select='gt(scene,0.28)',showinfo",
        "-an", "-f", "null", "-",
    ], timeout=min(MAX_JOB_SECONDS, max(180, int(duration * 3) + 30)))
    import re
    found = []
    for m in re.finditer(r"pts_time:([0-9]+(?:\.[0-9]+)?)", log_text):
        t = float(m.group(1))
        if 0.2 < t < duration - 0.2 and (not found or t - found[-1] > 0.15):
            found.append(round(t, 3))
    return found


def analyze_reference(reference: Path) -> dict:
    key = sha256(reference)
    cache_file = CACHE / f"{key}.json"
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
            if cached.get("sha256") == key:
                return cached
        except (OSError, json.JSONDecodeError):
            cache_file.unlink(missing_ok=True)
    started = time.monotonic()
    meta = probe(reference)
    if not meta["has_video"] or meta["duration"] <= 0:
        raise ValueError("Reference must contain a readable video stream.")
    cuts = scene_cuts(reference, meta["duration"])
    bounds = [0.0, *cuts, round(meta["duration"], 3)]
    shots = [
        {"start": a, "end": b, "duration": round(b - a, 3)}
        for a, b in zip(bounds, bounds[1:]) if b > a
    ]
    audio = CACHE / f"{key}.wav"
    music = {"tempo_bpm": None, "beats": [], "onsets": []}
    if extract_audio(reference, audio):
        music = analyze_music(audio)
    timeline = {
        "sha256": key, "media": meta, "cuts": cuts, "shots": shots,
        "music": music,
        "speed_effects": {
            "status": "not_reliably_recoverable",
            "note": "A flattened export does not reveal original speed-ramp keyframes."
        },
        "detection_limits": [
            "Cuts are sampled scene-change estimates, not guaranteed original edit points.",
            "Beat/onset detection is approximate.",
            "Transition type, original grading values, text animation and speed ramps cannot be recovered exactly from a flattened export."
        ],
        "analysis_seconds": round(time.monotonic() - started, 3),
    }
    tmp = cache_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(timeline, indent=2))
    tmp.replace(cache_file)
    return timeline


def choose_encoder() -> tuple[str, list[str]]:
    global ENCODER
    tests = [
        ("h264_nvenc", ["-f", "lavfi", "-i", "color=c=black:s=16x16:d=0.1", "-frames:v", "1", "-c:v", "h264_nvenc", "-f", "null", "-"]),
        ("h264_qsv", ["-f", "lavfi", "-i", "color=c=black:s=16x16:d=0.1", "-frames:v", "1", "-c:v", "h264_qsv", "-f", "null", "-"]),
        ("h264_vaapi", ["-f", "lavfi", "-i", "color=c=black:s=16x16:d=0.1", "-frames:v", "1", "-c:v", "h264_vaapi", "-f", "null", "-"]),
        ("h264_videotoolbox", ["-f", "lavfi", "-i", "color=c=black:s=16x16:d=0.1", "-frames:v", "1", "-c:v", "h264_videotoolbox", "-f", "null", "-"]),
    ]
    for name, args in tests:
        try:
            run_cmd(["ffmpeg", "-hide_banner", "-loglevel", "error", *args], timeout=8)
            ENCODER = name
            return name, ["-c:v", name]
        except Exception:
            continue
    ENCODER = "libx264"
    return ENCODER, ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"]


def media_duration(path: Path) -> float:
    try:
        return max(0.1, probe(path)["duration"])
    except Exception:
        return 3.0


def make_filter(job: EditJob, durations: list[float], preview: bool = False) -> tuple[list[str], str, str]:
    width, height = FORMATS[job.format_name]
    if preview:
        width = min(width, 480)
        height = int(width * FORMATS[job.format_name][1] / FORMATS[job.format_name][0])
        height += height % 2
    args: list[str] = []
    graph: list[str] = []
    labels = []
    style_grades = {
        "cinematic": "eq=contrast=1.12:saturation=0.92:brightness=-0.01",
        "hype": "eq=contrast=1.16:saturation=1.18:brightness=0.01",
        "aesthetic": "eq=contrast=0.98:saturation=1.10:brightness=0.025",
        "vlog": "eq=contrast=1.05:saturation=1.08:brightness=0.015",
        "auto": "eq=contrast=1.06:saturation=1.08:brightness=0.01",
        "reference": "eq=contrast=1.04:saturation=1.06:brightness=0.01",
    }
    grade = style_grades.get(job.style, style_grades["reference"])
    for i, (path, kind, dur) in enumerate(zip(job.media, job.media_kinds, durations)):
        if kind == "image":
            args += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(path)]
            # Still-image motion: gentle Ken Burns approximation using zoompan.
            graph.append(
                f"[{i}:v]scale={width*2}:{height*2}:force_original_aspect_ratio=increase,"
                f"crop={width*2}:{height*2},zoompan=z='min(zoom+0.0008,1.12)':"
                f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={width}x{height}:fps=24,"
                f"trim=duration={dur:.3f},setpts=PTS-STARTPTS,{grade},setsar=1,format=yuv420p,settb=AVTB[v{i}]"
            )
        else:
            args += ["-i", str(path)]
            graph.append(
                f"[{i}:v]trim=duration={dur:.3f},setpts=PTS-STARTPTS,"
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},fps=24,setsar=1,"
                f"{grade},format=yuv420p,settb=AVTB[v{i}]"
            )
        labels.append(f"[v{i}]")
    if len(labels) == 1:
        graph.append(f"{labels[0]}format=yuv420p[vout]")
    else:
        transition = {"cinematic": "fade", "hype": "wipeleft", "aesthetic": "dissolve", "vlog": "smoothleft"}.get(job.style, "fade")
        overlap = min(0.18, max(0.02, min(durations) * 0.25))
        previous = labels[0]
        elapsed = float(durations[0])
        for i in range(1, len(labels)):
            out_label = f"[xf{i}]"
            offset = max(0.0, elapsed - overlap * i)
            graph.append(
                f"{previous}{labels[i]}xfade=transition={transition}:duration={overlap:.3f}:"
                f"offset={offset:.3f},format=yuv420p{out_label}"
            )
            previous = out_label
            elapsed += float(durations[i])
        graph.append(f"{previous}format=yuv420p[vout]")
    return args, ";".join(graph), "[vout]"



def _sample_frame(path: Path, timestamp: Optional[float] = None) -> "np.ndarray":
    """Decode one small frame locally with FFmpeg; no external API or upload."""
    import cv2
    if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
        frame = cv2.imread(str(path))
    else:
        cap = cv2.VideoCapture(str(path))
        try:
            if timestamp is not None:
                cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, float(timestamp)) * 1000.0)
            ok, frame = cap.read()
        finally:
            cap.release()
        if not ok:
            raise ValueError(f"Could not sample frame from {path.name}")
    if frame is None:
        raise ValueError(f"Could not decode {path.name}")
    return cv2.resize(frame, (256, 144), interpolation=cv2.INTER_AREA)


def _visual_quality(frame) -> tuple[float, float, float]:
    """Return sharpness, exposure suitability and saturation from a local frame."""
    import cv2
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    mean = float(gray.mean())
    exposure = max(0.0, 1.0 - abs(mean - 128.0) / 128.0)
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    saturation = float(hsv[:, :, 1].mean()) / 255.0
    return sharpness, exposure, saturation


def analyze_visual_content(job: EditJob) -> dict:
    """Offline frame-quality analysis. No API key, cloud inference, or per-request charge."""
    import cv2  # OpenCV runs locally inside the worker.
    qualities = []
    descriptions = []
    for index, (path, kind) in enumerate(zip(job.media, job.media_kinds)):
        try:
            sample_time = media_duration(path) * 0.5 if kind == "video" else None
            frame = _sample_frame(path, sample_time)
            sharpness, exposure, saturation = _visual_quality(frame)
            # Log-scaled sharpness prevents high-texture clips from dominating completely.
            score = (min(1.0, math.log1p(max(0.0, sharpness)) / 8.0) * 0.55
                     + exposure * 0.35 + min(1.0, saturation * 1.4) * 0.10)
            qualities.append((index, score))
            focus = "sharp" if sharpness >= 45 else "soft"
            light = "well-exposed" if exposure >= 0.65 else "dark-or-overexposed"
            descriptions.append(f"Clip {index + 1}: {focus}, {light}, saturation {saturation:.2f}")
        except Exception as exc:
            log.warning("Local frame analysis failed for %s: %s", path.name, exc)
            qualities.append((index, 0.0))
            descriptions.append(f"Clip {index + 1}: analysis unavailable")
    # Keep stable upload order by default; quality scores are metadata, not a story/semantic guess.
    return {
        "enabled": True,
        "mode": "offline_opencv_quality",
        "clip_order": list(range(len(job.media))),
        "clip_descriptions": descriptions,
        "quality_scores": {str(i): round(score, 3) for i, score in qualities},
        "reference_style": "Reference cut timing and audio are analyzed separately; no cloud vision is used.",
        "detected_text": [],
        "transition_hints": [],
    }


def render(job: EditJob, output: Path, preview: bool = False) -> dict:
    started = time.monotonic()
    if not job.media:
        raise ValueError("Send at least one raw video or image.")
    ref_shots = job.timeline.get("shots", [])
    if ref_shots:
        total = min(60.0, sum(float(s["duration"]) for s in ref_shots))
        # Never discard uploaded raw clips just because the reference has fewer detected shots.
        # When there are more raw clips than reference shots, divide the reference runtime
        # across every uploaded clip so each user's clip appears at least once.
        if len(job.media) > len(ref_shots):
            durations = [total / len(job.media)] * len(job.media)
        else:
            durations = [max(0.8, float(s["duration"])) for s in ref_shots]
    else:
        total = min(30.0, max(8.0, sum(media_duration(p) for p in job.media)))
        durations = []
    if not durations:
        remaining = total
        for i, path in enumerate(job.media):
            d = min(media_duration(path), max(1.0, remaining / (len(job.media) - i)))
            durations.append(d)
            remaining -= d
        if remaining > 0.5:
            durations[-1] += remaining
    # Reuse user media only; reference video is never added as a render input.
    if len(durations) > 80:
        durations = durations[:80]
    clip_order = (job.timeline or {}).get("visual_analysis", {}).get("clip_order", [])
    if not clip_order:
        clip_order = list(range(len(job.media)))
    chosen_indices = [clip_order[i % len(clip_order)] for i in range(len(durations))]
    chosen = [job.media[i] for i in chosen_indices]
    kinds = [job.media_kinds[i] for i in chosen_indices]
    old_media, old_kinds = job.media, job.media_kinds
    job.media, job.media_kinds = chosen, kinds
    input_args, graph, video_label = make_filter(job, durations, preview)
    job.media, job.media_kinds = old_media, old_kinds

    input_count = len(durations)
    transition_overlap = min(0.18, max(0.02, min(durations) * 0.25)) if len(durations) > 1 else 0.0
    final_duration = max(0.1, sum(durations) - transition_overlap * max(0, len(durations) - 1))
    audio_path = None
    if job.music_source == "reference" and job.reference:
        audio_path = CACHE / f"{sha256(job.reference)}.music.m4a"
        if not audio_path.exists():
            run_cmd(["ffmpeg", "-y", "-v", "error", "-i", str(job.reference), "-vn", "-c:a", "aac", str(audio_path)])
    elif job.music_source == "own":
        audio_path = job.own_music
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *input_args]
    if audio_path and audio_path.exists():
        cmd += ["-stream_loop", "-1", "-i", str(audio_path)]
        graph += ";[{}:a]atrim=duration={:.3f},asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.4,afade=t=out:st={:.3f}:d=0.8,loudnorm=I=-16:TP=-1.5:LRA=11[aout]".format(input_count, final_duration, max(0.0, final_duration-0.8))
    encoder, encoder_args = choose_encoder()
    width, height = FORMATS[job.format_name]
    if preview:
        width = min(width, 480)
        height = int(width * FORMATS[job.format_name][1] / FORMATS[job.format_name][0])
        height += height % 2
    cmd += [
        "-filter_complex", graph, "-map", video_label,
    ]
    if audio_path and audio_path.exists():
        cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-an"]
    bitrate = max(800, min(8000, int((45 * 8 * 1024) / max(1, sum(durations)))))
    cmd += encoder_args + [
        "-b:v", f"{bitrate}k", "-r", "24", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", "-t", f"{sum(durations):.3f}", str(output),
    ]
    try:
        run_cmd(cmd, timeout=MAX_JOB_SECONDS)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Render timed out after {MAX_JOB_SECONDS} seconds.")
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("FFmpeg completed without producing an output file.")
    output_meta = probe(output)
    if audio_path and audio_path.exists() and not output_meta["has_audio"]:
        raise RuntimeError("A soundtrack was selected but the rendered file has no audio stream. Render rejected.")
    return {
        "seconds": round(time.monotonic() - started, 2),
        "encoder": encoder, "size_mb": round(output.stat().st_size / 1024 / 1024, 2),
        "duration": round(final_duration, 2), "preview": preview,
        "has_audio": output_meta["has_audio"],
    }


async def progress(update: Update, text: str) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(text)


def new_job(user_id: int, chat_id: int, style: str = "reference") -> EditJob:
    root = JOBS / f"{user_id}-{uuid.uuid4().hex[:10]}"
    root.mkdir(parents=True, exist_ok=True)
    job = EditJob(user_id=user_id, chat_id=chat_id, root=root, style=style)
    ACTIVE_JOBS[user_id] = job
    if user_id in USER_STYLE:
        job.timeline = USER_STYLE[user_id]
    return job


async def download_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    job = ACTIVE_JOBS.get(user.id)
    if job is None:
        job = new_job(user.id, message.chat_id)
        await message.reply_text(
            "🎬 Send the finished reference video first, then your own raw clips/images. "
            "For raw-only editing, send /auto first."
        )
        return
    if len(job.media) >= MAX_MEDIA_FILES:
        await message.reply_text(f"Limit reached: {MAX_MEDIA_FILES} media files per job.")
        return
    media_obj = message.video or message.document or message.photo[-1] if message.photo else message.video or message.document
    if not media_obj:
        return
    file_size = getattr(media_obj, "file_size", 0) or 0
    if file_size > MAX_UPLOAD_MB * 1024 * 1024:
        await message.reply_text(f"File is too large. Telegram bot download limit configured here is {MAX_UPLOAD_MB} MB.")
        return
    filename = getattr(media_obj, "file_name", None) or f"upload-{len(job.media)}"
    ext = Path(filename).suffix.lower()
    if message.photo:
        ext = ".jpg"
        kind = "image"
    elif ext in {".jpg", ".jpeg", ".png", ".webp"}:
        kind = "image"
    else:
        kind = "video"
        if ext not in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}:
            ext = ".mp4"
    target = job.root / f"media-{len(job.media)}{ext}"
    try:
        tgfile = await media_obj.get_file()
        await tgfile.download_to_drive(custom_path=str(target))
        if kind == "video":
            meta = await asyncio.to_thread(probe, target)
            if not meta["has_video"]:
                target.unlink(missing_ok=True)
                await message.reply_text("That file has no video stream. Send a video or image.")
                return
        job.media.append(target)
        job.media_kinds.append(kind)
        await message.reply_text(f"Received {kind} {len(job.media)}. Send more, then /done.")
    except Exception as exc:
        target.unlink(missing_ok=True)
        log.exception("Download failed")
        await message.reply_text(f"Could not download that file: {str(exc)[:250]}")


async def handle_video_or_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    job = ACTIVE_JOBS.get(user.id)
    is_image = bool(message.photo) or bool(message.document and (message.document.mime_type or "").startswith("image/"))
    # While waiting for a soundtrack, a video upload is audio input, not another raw clip.
    if job and job.music_source == "waiting":
        await music_upload(update, context)
        return
    if job is None:
        job = new_job(user.id, message.chat_id, "auto" if is_image else "reference")
        if is_image:
            await message.reply_text("Auto-Edit mode started. This image is your first raw shot. Send more clips/images, then /done.")
            await download_media(update, context)
        else:
            await message.reply_text("Reference video received next. Then send your own raw clips/images. Send /done when finished.")
            await download_reference(update, context)
        return
    if job.style == "auto" and not job.reference:
        await download_media(update, context)
    elif not job.reference and not is_image:
        await download_reference(update, context)
    else:
        await download_media(update, context)


async def download_reference(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    media_obj = message.video or message.document
    if not media_obj:
        await message.reply_text("Please send a video file as the reference.")
        return
    size = getattr(media_obj, "file_size", 0) or 0
    if size > MAX_UPLOAD_MB * 1024 * 1024:
        await message.reply_text(f"Reference is larger than the configured {MAX_UPLOAD_MB} MB download limit.")
        return
    job = ACTIVE_JOBS.get(user.id) or new_job(user.id, message.chat_id)
    name = getattr(media_obj, "file_name", None) or "reference.mp4"
    ext = Path(name).suffix.lower()
    if ext not in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}:
        ext = ".mp4"
    target = job.root / f"reference{ext}"
    try:
        await (await media_obj.get_file()).download_to_drive(custom_path=str(target))
        meta = await asyncio.to_thread(probe, target)
        if not meta["has_video"]:
            target.unlink(missing_ok=True)
            await message.reply_text("The reference has no video stream.")
            return
        job.reference = target
        await message.reply_text("Reference received. Now send your own raw clips/images, then /done.")
    except Exception as exc:
        target.unlink(missing_ok=True)
        await message.reply_text(f"Reference download failed: {str(exc)[:250]}")


async def music_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    job = ACTIVE_JOBS.get(user.id)
    if not job or job.music_source != "waiting":
        await message.reply_text("Use /done first, then choose your own music.")
        return
    media_obj = message.audio or message.voice or message.document or message.video
    if not media_obj:
        await message.reply_text("Send an MP3, M4A, WAV, voice message, or video containing the soundtrack.")
        return
    size = getattr(media_obj, "file_size", 0) or 0
    if size > MAX_UPLOAD_MB * 1024 * 1024:
        await message.reply_text(f"Music file exceeds {MAX_UPLOAD_MB} MB. Please send a smaller file.")
        return
    target = job.root / "own-music"
    try:
        await (await media_obj.get_file()).download_to_drive(custom_path=str(target))
        info = probe(target) if message.video else None
        if message.video:
            extracted = job.root / "own-music.m4a"
            if not extract_audio(target, extracted):
                raise ValueError("That video has no audio track.")
            target.unlink(missing_ok=True)
            target = extracted
        else:
            # Verify decodable audio and normalize to a known container for FFmpeg.
            normalized = job.root / "own-music.m4a"
            run_cmd(["ffmpeg", "-y", "-v", "error", "-i", str(target), "-vn", "-c:a", "aac", str(normalized)])
            target.unlink(missing_ok=True)
            target = normalized
        job.own_music = target
        job.music_source = "own"
        await message.reply_text("Music received. Analyzing music and syncing the edit...")
        await finish_render(update, context, job)
    except Exception as exc:
        target.unlink(missing_ok=True)
        await message.reply_text(f"Invalid music file: {str(exc)[:250]}. Please send another audio file.")


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    job = ACTIVE_JOBS.get(user.id)
    if not job or (not job.media and not job.reference):
        await progress(update, "No edit is waiting. Send a reference video and raw media, or /auto for raw-only editing.")
        return
    if not job.media:
        await progress(update, "Send at least one raw clip or image before /done.")
        return
    if job.reference:
        await progress(update, "Analyzing reference cuts and music...")
        try:
            job.timeline = await asyncio.to_thread(analyze_reference, job.reference)
            # Align the reference's estimated rhythm to its own detected beat grid too.
            job.timeline = remap_shots_to_beats(job.timeline, job.timeline.get("music", {}))
            await progress(update, f"Analysis ready: {len(job.timeline.get('cuts', []))} estimated cuts. Choose music:")
            reference_has_audio = bool(job.timeline.get("media", {}).get("has_audio"))
            keyboard = [[
                *([InlineKeyboardButton("🎵 Music from reference video", callback_data="music:reference")] if reference_has_audio else []),
                InlineKeyboardButton("📁 My own music file", callback_data="music:own"),
            ]]
            await update.effective_message.reply_text(
                "Which music do you want for the final video?",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            job.music_source = "choosing"
        except Exception as exc:
            await progress(update, f"Reference analysis failed: {str(exc)[:400]}")
    else:
        keyboard = [[
            InlineKeyboardButton("Cinematic", callback_data="style:cinematic"),
            InlineKeyboardButton("Hype / Fast", callback_data="style:hype"),
        ], [
            InlineKeyboardButton("Aesthetic / Soft", callback_data="style:aesthetic"),
            InlineKeyboardButton("Vlog", callback_data="style:vlog"),
        ], [
            InlineKeyboardButton("Let AI decide", callback_data="style:auto"),
        ]]
        await update.effective_message.reply_text("Pick an Auto-Edit style:", reply_markup=InlineKeyboardMarkup(keyboard))


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user_id = query.from_user.id
    job = ACTIVE_JOBS.get(user_id)
    if not job:
        await query.edit_message_text("This edit session expired. Start again with /start.")
        return
    data = query.data or ""
    if data.startswith("music:"):
        choice = data.split(":", 1)[1]
        if choice == "reference":
            if not job.reference:
                await query.edit_message_text("No reference audio is available.")
                return
            job.music_source = "reference"
            await query.edit_message_text("Using reference audio. Analyzing music, syncing cuts, then rendering...")
            await finish_render(update, context, job)
        elif choice == "own":
            job.music_source = "waiting"
            await query.edit_message_text("Send your music file (MP3, M4A, WAV, voice message, or a video with audio).")
        elif choice == "silent":
            job.music_source = "silent"
            await query.edit_message_text("Rendering without a soundtrack...")
            await finish_render(update, context, job)
        return
    if data.startswith("style:"):
        job.style = data.split(":", 1)[1]
        await query.edit_message_text(
            f"Auto-Edit style set to {job.style}. Choose music:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📁 Upload my music", callback_data="music:own"),
                InlineKeyboardButton("🔇 No music", callback_data="music:silent"),
            ]]),
        )
        return
    if data.startswith("control:"):
        action = data.split(":", 1)[1]
        if action == "energy":
            job.energy = min(1.5, job.energy + 0.2)
        elif action == "smooth":
            job.energy = max(0.6, job.energy - 0.2)
        await query.edit_message_text(f"Setting updated ({action}). Send /rerender to create the revised export.")
        return


async def finish_render(update: Update, context: ContextTypes.DEFAULT_TYPE, job: EditJob) -> None:
    chat_id = job.chat_id
    status = await context.bot.send_message(chat_id, "Matching clips and preparing the edit...")
    lock = USER_LOCKS.setdefault(job.user_id, asyncio.Lock())
    started = time.monotonic()
    async with QUEUE_SEMAPHORE, lock:
        try:
            await context.bot.edit_message_text("Analyzing raw clips locally (no external AI API)...", chat_id, status.message_id)
            visual = await asyncio.to_thread(analyze_visual_content, job)
            job.timeline = dict(job.timeline or {})
            job.timeline["visual_analysis"] = visual
            if job.music_source == "own" and job.own_music:
                await context.bot.edit_message_text("Analyzing music... Syncing cuts...", chat_id, status.message_id)
                music_map = await asyncio.to_thread(analyze_music, job.own_music)
                if job.timeline:
                    job.timeline = remap_shots_to_beats(job.timeline, music_map)
            elif job.music_source == "reference" and job.timeline:
                job.timeline = remap_shots_to_beats(job.timeline, job.timeline.get("music", {}))
            elif job.music_source not in {"reference", "own", "silent"}:
                await context.bot.edit_message_text("Choose a music option before rendering.", chat_id, status.message_id)
                return
            final_path = job.root / "final.mp4"
            await context.bot.edit_message_text("Rendering final video...", chat_id, status.message_id)
            final_info = await asyncio.wait_for(asyncio.to_thread(render, job, final_path, False), timeout=MAX_JOB_SECONDS)
            with final_path.open("rb") as stream:
                await context.bot.send_video(
                    chat_id, video=stream,
                    caption=f"Finished edit • {final_info['duration']}s • {final_info['encoder']} • {final_info['size_mb']} MB",
                    supports_streaming=True,
                )
            elapsed = round(time.monotonic() - started, 2)
            await context.bot.send_message(
                chat_id,
                "Speed report\\n"
                f"Preview: {preview_info['seconds']}s\\n"
                f"Final render: {final_info['seconds']}s\\n"
                f"Total: {elapsed}s\\n"
                f"Encoder: {final_info['encoder']}\\n"
                f"Output: {final_info['size_mb']} MB\\n"
                "Note: actual time depends on CPU, input resolution, duration and available encoders.",
            )
        except asyncio.TimeoutError:
            await context.bot.send_message(chat_id, "The render exceeded the configured timeout. Try shorter or smaller source files.")
        except Exception as exc:
            log.exception("Render job failed")
            await context.bot.send_message(chat_id, f"Render failed: {str(exc)[:700]}")
        finally:
            ACTIVE_JOBS.pop(job.user_id, None)
            job.clean()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await progress(update,
        "🎬 Automatic Video Editor\\n\\n"
        "Reference mode: send one finished reference video, then your own clips/images, then /done.\\n"
        "Auto-Edit mode: send /auto, upload only your clips/images, then /done.\\n\\n"
        "Commands: /auto /done /cancel /format /music /savestyle name /mystyles /usestyle name /styles /benchmark /help"
    )


async def auto_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    if user.id in ACTIVE_JOBS:
        ACTIVE_JOBS[user.id].clean()
    new_job(user.id, update.effective_chat.id, "auto")
    await progress(update, "Auto-Edit mode ready. Send your raw clips and/or images, then /done.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user and user.id in ACTIVE_JOBS:
        ACTIVE_JOBS.pop(user.id).clean()
    await progress(update, "Current edit cancelled and temporary files removed.")


async def set_format(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    job = ACTIVE_JOBS.get(user.id) if user else None
    if not job:
        await progress(update, "Start an edit first. Usage: /format 9:16, /format 1:1, or /format 16:9")
        return
    value = context.args[0] if context.args else ""
    if value not in FORMATS:
        await progress(update, "Choose /format 9:16, /format 1:1, or /format 16:9.")
        return
    job.format_name = value
    await progress(update, f"Output format set to {value}.")


async def music_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await progress(update, "For a new job, choose reference music or your own track after /done. Change-music re-render is available in a later iteration; this running build currently handles one music choice per job.")


async def save_style(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    job = ACTIVE_JOBS.get(user.id) if user else None
    name = " ".join(context.args).strip()
    if not user or not job or not job.timeline or not name:
        await progress(update, "Analyze a reference first, then use /savestyle name.")
        return
    safe_name = "".join(ch for ch in name if ch.isalnum() or ch in "-_ ")[:40].strip()
    if not safe_name:
        await progress(update, "Use a style name containing letters or numbers.")
        return
    path = STYLES / f"{user.id}.json"
    try:
        saved = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        saved = {}
    saved[safe_name] = job.timeline
    path.write_text(json.dumps(saved, indent=2))
    await progress(update, f"Saved reference timeline as '{safe_name}'. Render-host local storage may be temporary.")


async def mystyles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    path = STYLES / f"{user.id}.json"
    try:
        saved = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        saved = {}
    await progress(update, "Saved styles: " + (", ".join(saved.keys()) if saved else "none yet"))


async def use_style(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    name = " ".join(context.args).strip()
    if not user or not name:
        await progress(update, "Usage: /usestyle name")
        return
    path = STYLES / f"{user.id}.json"
    try:
        saved = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        saved = {}
    if name not in saved:
        await progress(update, "Style not found. Use /mystyles to see saved names.")
        return
    USER_STYLE[user.id] = saved[name]
    await progress(update, f"Loaded '{name}'. Start a new edit; it will use the saved shot rhythm.")


async def styles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await progress(update, "Available presets: cinematic, hype, aesthetic, vlog. These are lightweight presets, not learned copies of a reference.")


async def rerender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await progress(update, "The previous job's temporary media is cleaned after delivery. Upload the media again to re-render. Persistent re-render caching requires durable storage.")


async def benchmark(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    started = time.monotonic()
    try:
        version = run_cmd(["ffmpeg", "-version"]).splitlines()[0]
        encoder, args = choose_encoder()
        await progress(update,
            "Benchmark environment\\n"
            f"FFmpeg: {version}\\n"
            f"Encoder available: {encoder}\\n"
            f"Probe test: {round(time.monotonic()-started, 3)}s\\n"
            "A true render benchmark needs a source clip; send /benchmark with a job active after uploading media."
        )
    except Exception as exc:
        await progress(update, f"Benchmark failed: {str(exc)[:300]}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled Telegram handler error", exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("auto", auto_mode))
    app.add_handler(CommandHandler("done", done))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("format", set_format))
    app.add_handler(CommandHandler("music", music_command))
    app.add_handler(CommandHandler("mystyles", mystyles))
    app.add_handler(CommandHandler("savestyle", save_style))
    app.add_handler(CommandHandler("usestyle", use_style))
    app.add_handler(CommandHandler("styles", styles))
    app.add_handler(CommandHandler("rerender", rerender))
    app.add_handler(CommandHandler("benchmark", benchmark))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE | filters.Document.AUDIO, music_upload))
    app.add_handler(MessageHandler(filters.VIDEO | filters.PHOTO | filters.Document.ALL, handle_video_or_image))
    app.add_error_handler(error_handler)

    if PUBLIC_URL:
        webhook_url = f"{PUBLIC_URL}/{WEBHOOK_SECRET}" if WEBHOOK_SECRET else PUBLIC_URL
        app.run_webhook(
            listen="0.0.0.0", port=PORT, url_path=WEBHOOK_SECRET or "",
            webhook_url=webhook_url, secret_token=WEBHOOK_SECRET or None,
            drop_pending_updates=True,
        )
    else:
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

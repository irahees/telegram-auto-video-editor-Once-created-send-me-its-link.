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


def make_segments(reference: Path, clips: list[Path], ref_beats: list[float]) -> list[tuple[Path, float, float]]:
    """Map reference cut durations onto different portions of the user's clips.

    The source offset advances for each clip instead of repeatedly taking its
    first frames. This is a timing-based assignment, not semantic AI matching.
    """
    if not clips:
        raise RuntimeError("Upload at least one of your own clips.")
    ref_duration = probe_duration(reference)
    cuts = align_cuts_to_beats(scene_times(reference), ref_beats, ref_duration)
    durations = [cuts[i + 1] - cuts[i] for i in range(len(cuts) - 1)]
    clip_durations = [probe_duration(c) for c in clips]
    cursors = [0.0 for _ in clips]
    segments: list[tuple[Path, float, float]] = []
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

        source_start = cursors[clip_index]
        if source_start + wanted > available - 0.04:
            source_start = 0.0
        segments.append((src, source_start, wanted))
        cursors[clip_index] = source_start + wanted
        remaining -= wanted
        idx += 1

    if remaining > 0.20:
        raise RuntimeError(
            "Your clips are too short for the reference duration. "
            "Upload longer clips or more clips."
        )
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

    for i, (src, source_start, duration) in enumerate(segments):
        segment = segment_dir / f"segment_{i:03d}.mp4"
        vf = (
            f"scale={OUTPUT_WIDTH + 24}:{OUTPUT_HEIGHT + 24}:force_original_aspect_ratio=increase,"
            f"crop={OUTPUT_WIDTH}:{OUTPUT_HEIGHT},fps={OUTPUT_FPS},setsar=1,"
            "eq=contrast=1.04:saturation=1.06:brightness=0.005,format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS"
        )
        run_ffmpeg([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-threads", str(FFMPEG_THREADS), "-filter_threads", "1",
            "-ss", f"{source_start:.4f}", "-i", str(src), "-t", f"{duration:.4f}",
            "-vf", vf, "-an", "-c:v", "libx264", "-preset", "ultrafast",
            "-crf", "28", "-threads", str(FFMPEG_THREADS),
            "-pix_fmt", "yuv420p", str(segment)
        ])
        if not segment.exists() or segment.stat().st_size < 1000:
            raise RuntimeError(f"FFmpeg produced an empty segment from {src.name}.")
        segment_files.append(segment)
        durations.append(duration)

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
                "-filter_complex", filter_arg, "-map", video_map,
                "-map", f"{len(segment_files)}:a:0",
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
                "-threads", str(FFMPEG_THREADS), "-an",
                "-movflags", "+faststart", str(output)
            ])
    else:
        if soundtrack:
            run_ffmpeg([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-stream_loop", "-1", "-i", str(soundtrack),
                "-i", str(segment_files[0]), "-map", "1:v:0", "-map", "0:a:0",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                "-shortest", "-movflags", "+faststart", str(output)
            ])
        else:
            shutil.copy2(segment_files[0], output)

    compatible = output.with_name("final_edit_compatible.mp4")
    run_ffmpeg([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(output), "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
        "-profile:v", "main", "-level", "3.1", "-pix_fmt", "yuv420p",
        "-r", str(OUTPUT_FPS), "-fps_mode", "cfr",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart", str(compatible)
    ])
    if not compatible.exists() or compatible.stat().st_size < 10000:
        raise RuntimeError("The rendered MP4 failed its output validation.")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,pix_fmt", "-of", "csv=p=0",
         str(compatible)], capture_output=True, text=True
    )
    if probe.returncode or "h264" not in probe.stdout or "yuv420p" not in probe.stdout:
        raise RuntimeError("The rendered video is not in a compatible H.264 format.")
    output.unlink(missing_ok=True)
    compatible.replace(output)
    shutil.rmtree(segment_dir, ignore_errors=True)
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

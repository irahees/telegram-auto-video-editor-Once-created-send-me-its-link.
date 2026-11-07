# Telegram Automatic Video Editor

Integrated rebuild baseline for reference-style and raw-only editing.

## Runtime
- Python 3.11
- FFmpeg + FFprobe
- python-telegram-bot 21.10
- librosa for approximate beat/onset analysis

## Render deployment
Set `BOT_TOKEN` privately in Render environment variables. Do not paste it into GitHub or chat.
Optional variables: `WEBHOOK_SECRET`, `MAX_UPLOAD_MB` (default 20), `MAX_MEDIA_FILES` (default 20), `MAX_JOB_SECONDS` (default 900), `WORKERS` (default 1). Visual analysis is performed locally with OpenCV; it does not require an OpenAI API key or send sampled frames to an external AI API.

## User flow
Reference: send a reference video, upload raw clips/images, send `/done`, choose reference audio or upload music.
Raw-only: send `/auto`, upload raw clips/images, send `/done`, choose a style.
Commands: `/start`, `/auto`, `/done`, `/cancel`, `/format 9:16|1:1|16:9`, `/music`, `/mystyles`, `/usestyle`, `/styles`, `/benchmark`.

## What this build does
- Reference SHA-256 cache; low-resolution sampled scene-change estimates.
- Approximate tempo, beat and onset analysis using librosa.
- Reference-audio extraction or user-supplied audio.
- Offline OpenCV frame-quality analysis (sharpness, exposure and saturation) for raw clips. It requires no API key and sends no frames to a cloud AI service. It preserves upload order rather than pretending to understand clip semantics.
- Raw video/image-only output; reference video is never a render input.
- Images receive a Ken Burns-style zoom; style presets apply basic color grading and crossfade/wipe transitions.
- Aspect-ratio crop, H.264 export, faststart, preview then final render.
- Encoder capability probing with NVENC/QSV/VAAPI/VideoToolbox attempts and CPU fallback.
- Timeouts, error messages and per-job temporary-directory cleanup.

## Honest feature limits
This is a functional baseline, not a full AI NLE clone. The finished reference cannot reveal exact original timeline/effect settings. Local visual analysis measures frame quality only; it does not provide semantic scene understanding, text recognition or true subject matching. No OpenAI API key is required, and no cloud vision requests are made.

## Local setup
```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
ffmpeg -version
ffprobe -version
BOT_TOKEN='your-token' python bot.py
```

## Test checklist
1. Confirm `python -m py_compile bot.py` succeeds.
2. Confirm `ffmpeg -version` and `ffprobe -version`.
3. Start bot with a privately configured token.
4. Send a short reference video, then one raw clip and /done.
5. Test both music choices and an image-only /auto job.
6. Verify output contains only user raw media, not reference footage.
7. Test invalid audio, oversized uploads, /cancel and /benchmark.

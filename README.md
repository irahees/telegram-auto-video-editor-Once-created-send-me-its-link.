# Telegram Automatic Video Editor

Integrated rebuild baseline for reference-style and raw-only editing.

## Runtime
- Python 3.11
- FFmpeg + FFprobe
- python-telegram-bot 21.10
- librosa for approximate beat/onset analysis

## Render deployment
Set `BOT_TOKEN` privately in Render environment variables. Do not paste it into GitHub or chat.
Optional variables: `WEBHOOK_SECRET`, `MAX_UPLOAD_MB` (default 20), `MAX_MEDIA_FILES` (default 20), `MAX_JOB_SECONDS` (default 900), `WORKERS` (default 1). Optional visual matching: set `OPENAI_API_KEY` privately and optionally `OPENAI_VISION_MODEL` (default `gpt-4o-mini`). Without the key, the editor runs without cloud vision analysis. When enabled, representative frames from the reference and user uploads are sent to the OpenAI API for semantic clip ordering; never put the key in source code or GitHub.

## User flow
Reference: send a reference video, upload raw clips/images, send `/done`, choose reference audio or upload music.
Raw-only: send `/auto`, upload raw clips/images, send `/done`, choose a style.
Commands: `/start`, `/auto`, `/done`, `/cancel`, `/format 9:16|1:1|16:9`, `/music`, `/mystyles`, `/usestyle`, `/styles`, `/benchmark`.

## What this build does
- Reference SHA-256 cache; low-resolution sampled scene-change estimates.
- Approximate tempo, beat and onset analysis using librosa.
- Reference-audio extraction or user-supplied audio.
- Optional OpenAI vision analysis of sampled reference/raw frames, returning clip descriptions, visual notes, detected text hints and a suggested raw-clip order. It is opt-in through `OPENAI_API_KEY`.
- Raw video/image-only output; reference video is never a render input.
- Images receive a Ken Burns-style zoom; style presets apply basic color grading and crossfade/wipe transitions.
- Aspect-ratio crop, H.264 export, faststart, preview then final render.
- Encoder capability probing with NVENC/QSV/VAAPI/VideoToolbox attempts and CPU fallback.
- Timeouts, error messages and per-job temporary-directory cleanup.

## Honest feature limits
This is a functional baseline, not a full AI NLE clone. The finished reference cannot reveal exact original timeline/effect settings. Vision matching is a lightweight sampled-frame ranking aid, not full CLIP embeddings or frame-by-frame semantic search. Detected text is reported as a hint but is not yet animated/recreated; OCR tracking, Whisper captions, parallax segmentation, stabilization/denoise/upscale, true speed-ramp recovery, J/L-cut reconstruction, generated impact/riser SFX, and three independent edit variations remain unimplemented. Persistent cross-job style storage is not guaranteed on Render Free. Some inline controls are informational only. Render Free CPU cannot guarantee 15 seconds for a 30-second 1080p export or a GPU encoder.

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

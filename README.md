# Telegram Automatic Video Editor

A Telegram bot that accepts one reference video, your own video clips, and an optional soundtrack. It detects approximate scene changes in the reference, uses that timing as a rough cut rhythm, formats the result as vertical 1080x1920 / 30 fps, and sends the MP4 back.

## First-version limitations

This is an automatic editing MVP, not frame-perfect AI style transfer. It matches the reference duration and approximate hard-cut rhythm. It does not understand semantic shot selection or recreate complex transitions, motion graphics, typography, masks, color grading, or beat-synchronized effects. The reference video's audio is not copied.

## Deploy on Render

This repository is configured for a Render Web Service using Telegram webhooks and the free plan.

1. Create a Render Web Service from this GitHub repository.
2. Select the Docker runtime, Singapore region, and Free plan.
3. Set `BOT_TOKEN` to the token from Telegram's @BotFather.
4. Set `WEBHOOK_SECRET` to a long random value containing only letters, numbers, underscores, or hyphens.
5. Deploy. Render supplies `RENDER_EXTERNAL_URL`; the bot registers its webhook automatically at startup.

Never commit either secret to GitHub or post the bot token in chat.

## Use

- Send /start
- Upload one reference video.
- Upload your own video clips one by one.
- Optionally upload an audio soundtrack.
- Send /done to render, or /cancel to clear the session.

## Limits, free-tier behavior, and privacy

- Per-file upload limit defaults to 20 MB. Telegram's hosted Bot API limits bot file downloads to 20 MB.
- Render free web services can sleep after inactivity and may have cold-start delays. This is not a guaranteed always-on worker.
- Session state and uploaded files are held in memory and temporary local storage. A restart or sleep/redeploy can interrupt an unfinished edit; this first version does not have durable session storage.
- Free instances have limited CPU and memory, so long videos or many clips may fail during FFmpeg processing.
- Temporary media is deleted after rendering or cancellation.
- Only upload videos and audio you own or have permission to use.

# Telegram Automatic Video Editor

A Telegram bot that accepts one reference video, your own video clips, and an optional soundtrack. It detects approximate scene changes in the reference, uses that timing as a rough cut rhythm, formats the result as vertical 1080x1920 / 30 fps, and sends the MP4 back.

## First-version limitations

This is an automatic editing MVP, not frame-perfect AI style transfer. It matches the reference duration and approximate hard-cut rhythm. It does not understand semantic shot selection or recreate complex transitions, motion graphics, typography, masks, color grading, or beat-synchronized effects. The reference video's audio is not copied.

## Create a Telegram bot

1. Open Telegram and search for @BotFather.
2. Send /newbot and follow the prompts.
3. Copy the bot token and keep it private. Never commit it to GitHub or post it in chat.

## Deploy on Render

1. Open Render and choose New → Blueprint.
2. Connect this GitHub repository.
3. Render reads render.yaml and creates the background worker.
4. Add BOT_TOKEN as a secret environment variable in Render.
5. Deploy the worker.

A continuously running polling bot needs an always-on background worker; Render's free web-service sleep behavior is not suitable. The included Blueprint uses the Starter worker plan, which may incur charges.

## Use

- Send /start
- Upload one reference video.
- Upload your own video clips one by one.
- Optionally upload an audio soundtrack.
- Send /done to render, or /cancel to clear the session.

## Limits and privacy

- Per-file upload limit defaults to 45 MB and can be changed with MAX_UPLOAD_MB.
- Telegram Bot API hosted file-download limits apply.
- Render's local worker storage is temporary; session files are deleted after rendering or cancellation.
- Only upload videos and audio you own or have permission to use.

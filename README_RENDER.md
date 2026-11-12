# Telegram Image Bot — Render free webhook (24/7, no card)

Same bot you already tested end-to-end, but as a webhook web service
so Render's free tier can run it. No card needed for Render.

How it works: Telegram sends every message to your Render URL
(`POST /webhook`). Render sleeps after 15 minutes with no traffic,
wakes up when the next message arrives. First reply after sleep
can take 30-50 seconds — Telegram waits, the picture still arrives.

Free limits, honest:
- Render free: 750 hours/month, one web service, sleeps after 15 min idle.
- Cloudflare: 10,000 Neurons/day shared, ~173 pictures or ~91 edits/day.
  Per-user caps in this bot: 5 pictures + 3 edits/day.
- Render's free disk is ephemeral: usage counts reset when the
  service restarts. That's fine for a free bot.

## What you need (you already have all 3)

1. Telegram bot token — from @BotFather, for @clfreimagegenrbot
2. Cloudflare Account ID — Cloudflare dashboard, right sidebar
3. Cloudflare API token — Workers AI Edit permission

Never put any of these in code or in GitHub. They go in Render
as Environment Variables only.

## Deploy — step by step

### 1. Put this folder on GitHub

Create a new GitHub repo, e.g. `telegram-image-bot`, and upload
these files (the whole `render-webhook/` folder contents, at the
repo root):

- `app.py`
- `cloudflare.py`
- `usage.py`
- `requirements.txt`
- `render.yaml`
- `set_webhook.py`
- `README_RENDER.md` (this file)

Do NOT upload `.env`, `usage.db`, or any file with a token in it.
There are none in this folder — check before you push.

### 2. Create the Render service

1. Go to https://dashboard.render.com — sign up free with GitHub.
2. New + > Web Service > Build and deploy from a Git repository.
3. Pick your `telegram-image-bot` repo.
4. Settings:
   - Name: `telegram-image-bot`
   - Region: Singapore (closest to Dhaka)
   - Branch: main
   - Root Directory: leave blank (files are at repo root)
   - Runtime: Python 3
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120`
   - Instance Type: Free
5. Environment Variables (Dashboard > Environment > Add):
   - `TELEGRAM_BOT_TOKEN` = your BotFather token
   - `CF_ACCOUNT_ID` = your Cloudflare Account ID
   - `CF_API_TOKEN` = your Cloudflare API token
   - `DAILY_TXT_LIMIT` = `5`
   - `DAILY_EDIT_LIMIT` = `3`
   - `WEBHOOK_SECRET` = any random long string you make up, e.g. 32 letters
6. Create Web Service. Wait for Deploy — Live.
7. Copy your URL, e.g. `https://telegram-image-bot-xxxx.onrender.com`
8. Open that URL in a browser — you should see:
   `ok - image bot webhook alive`
   If you see that, Render is running. If not, check Logs.

### 3. Point Telegram at Render

On your PC (or any terminal):

```
export TELEGRAM_BOT_TOKEN='paste your BotFather token here'
export RENDER_URL='https://telegram-image-bot-xxxx.onrender.com'
export WEBHOOK_SECRET='the same secret you put in Render'
python3 set_webhook.py set
python3 set_webhook.py info
```

`info` should show `"url": "https://.../webhook"` and
`"pending_update_count": 0`. That means Telegram will now send
messages to Render instead of waiting for polling.

Important: once the webhook is set, do NOT also run `bot.py`
polling at the same time — Telegram only delivers to one place.
Polling and webhook fight each other and messages get lost.

### 4. Test it

1. In Telegram, open @clfreimagegenrbot, send `/start`.
   First time after sleep: wait up to a minute.
2. Send: `/imagine a cat wearing sunglasses`
3. You should get a picture back in 20-40 seconds.
4. Send a photo with caption `make it sunset` — you should get an edit back.

If `/start` gets no reply after 2 minutes, check in order:
- Render Dashboard > Logs — look for errors, or "Missing TELEGRAM_BOT_TOKEN"
- Open your Render URL — does it say `ok - image bot webhook alive`?
- Run `python3 set_webhook.py info` — is the URL correct, with `/webhook`?
- Render free sleeps: send `/start` twice, 60 seconds apart — the
  first wakes it, the second usually gets through.

## To stop it / go back to polling

```
python3 set_webhook.py delete
```

Then run `bot.py` on your PC again if you want polling back.

## Files

- `app.py` — Flask webhook app, answers 200 fast, generates in background
- `cloudflare.py` — FLUX.1-schnell txt2img, FLUX.2-klein-4b img2img
- `usage.py` — per-user daily caps + shared Neuron budget (SQLite, ephemeral on Render free)
- `render.yaml` — Blueprint for one-click deploy
- `set_webhook.py` — set / check / delete the Telegram webhook

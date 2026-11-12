#!/usr/bin/env python3
"""Point @clfreimagegenrbot at your Render URL, or check/remove it.

Usage:
  export TELEGRAM_BOT_TOKEN='...'
  export RENDER_URL='https://telegram-image-bot-xxxx.onrender.com'
  export WEBHOOK_SECRET='...'   # optional, must match Render env var
  python3 set_webhook.py set     # set webhook to $RENDER_URL/webhook
  python3 set_webhook.py info    # show current webhook
  python3 set_webhook.py delete  # stop webhook, back to polling

No token is stored in this file. Never paste a token in chat.
"""
import os
import sys

import requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
RENDER_URL = os.environ.get("RENDER_URL", "").strip().rstrip("/")
SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()


def call(method, **params):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/{method}", json=params, timeout=30
    )
    print(r.status_code, r.text[:1000])
    return r


def main():
    if not TOKEN:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "info"
    if cmd == "set":
        if not RENDER_URL:
            raise SystemExit("Missing RENDER_URL, e.g. https://telegram-image-bot-xxxx.onrender.com")
        params = {"url": f"{RENDER_URL}/webhook", "drop_pending_updates": True}
        if SECRET:
            params["secret_token"] = SECRET
        call("setWebhook", **params)
    elif cmd == "info":
        call("getWebhookInfo")
    elif cmd == "delete":
        call("deleteWebhook", drop_pending_updates=True)
    else:
        raise SystemExit("Use: set | info | delete")


if __name__ == "__main__":
    main()

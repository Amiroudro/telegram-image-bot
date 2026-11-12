"""
Telegram image bot - Render free webhook version.

Same bot as bot.py (polling), but for Render's free Web Service:
- Render sleeps after 15 min with no traffic, wakes on the next
  Telegram webhook POST. First reply after sleep can take 30-50s.
- Telegram sends each message as a POST to /webhook.
- We answer 200 OK immediately, then do the slow image work
  in a background thread and send the picture back via the
  Telegram Bot API. This stops Telegram retrying while
  Cloudflare is still drawing (20-30s).

Commands: /start /help /imagine <prompt> /edit (reply to photo)
  /status, plain text = imagine, photo + caption = edit,
  photo then next text = edit.

Env vars (set in Render Dashboard > Environment, never in code):
  TELEGRAM_BOT_TOKEN - from @BotFather
  CF_ACCOUNT_ID      - Cloudflare dashboard, right sidebar
  CF_API_TOKEN       - Cloudflare API token, Workers AI Edit
  DAILY_TXT_LIMIT    - default 5
  DAILY_EDIT_LIMIT   - default 3
  WEBHOOK_SECRET     - optional, set the same in set_webhook.py
  USAGE_DB           - default /tmp/usage.db (Render free disk is
                       ephemeral, caps reset on restart - fine for free)

Free limits (Cloudflare, Nov 2026):
  10,000 Neurons/day shared, resets 00:00 UTC
  ~173 text pictures/day at 1024x1024, ~91 edits/day
"""

import io
import logging
import os
import threading

import requests
from flask import Flask, jsonify, request
from PIL import Image

import cloudflare
from usage import UsageStore

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("image-bot-webhook")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "").strip()
CF_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
TXT_LIMIT = int(os.environ.get("DAILY_TXT_LIMIT", "5"))
EDIT_LIMIT = int(os.environ.get("DAILY_EDIT_LIMIT", "3"))

TG_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Pending edit: user_id -> file_id, photo sent without caption.
# In-memory only: lost if Render sleeps/restarts. Acceptable on free.
pending_photo: dict[int, str] = {}

usage = UsageStore(os.environ.get("USAGE_DB", "/tmp/usage.db"))

app = Flask(__name__)


def tg_post(method, data=None, files=None, timeout=60):
    url = f"{TG_API}/{method}"
    r = requests.post(url, data=data, files=files, timeout=timeout)
    if r.status_code != 200:
        log.warning("Telegram %s failed %s: %s", method, r.status_code, r.text[:500])
    return r.json() if r.content else {}


def send_message(chat_id, text):
    return tg_post("sendMessage", data={"chat_id": chat_id, "text": text})


def send_photo(chat_id, image_bytes, caption):
    files = {"photo": ("image.jpg", io.BytesIO(image_bytes), "image/jpeg")}
    return tg_post(
        "sendPhoto",
        data={"chat_id": chat_id, "caption": caption},
        files=files,
        timeout=120,
    )


def download_telegram_photo(file_id):
    info = tg_post("getFile", data={"file_id": file_id})
    file_path = info["result"]["file_path"]
    url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content


def resize_input_for_klein(image_bytes):
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img.thumbnail((500, 500), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


def do_text_to_image(chat_id, user_id, prompt):
    prompt = (prompt or "").strip()
    if len(prompt) < 3:
        send_message(chat_id, "Send a longer prompt, e.g.\n/imagine a red fox in fresh snow")
        return
    if len(prompt) > 2000:
        send_message(chat_id, "That prompt is too long - keep it under 2000 characters.")
        return
    allowed, reason, txt_used, _ = usage.check_and_count(
        user_id, "txt", TXT_LIMIT, EDIT_LIMIT, cloudflare.NEURONS_SCHNELL_1024
    )
    if not allowed:
        send_message(chat_id, reason)
        return
    send_message(chat_id, f"Drawing it... ({txt_used}/{TXT_LIMIT} pictures used today)")
    try:
        image_bytes, neurons = cloudflare.text_to_image(
            CF_ACCOUNT_ID, CF_TOKEN, prompt
        )
        usage.add_neurons(neurons)
        send_photo(
            chat_id, image_bytes,
            f"\u201c{prompt}\u201d\nFLUX.1-schnell \u2022 1024\u00d71024 \u2022 ~{neurons:.1f} Neurons",
        )
    except Exception as e:
        log.exception("txt2img failed")
        msg = str(e)
        if "429" in msg or "used up" in msg.lower():
            send_message(
                chat_id,
                "Today's free Cloudflare quota is used up for everyone. "
                "Try again after 00:00 UTC.",
            )
            return
        send_message(chat_id, f"Sorry, that picture failed: {msg}\nTry a simpler prompt, or try again in a minute.")


def do_image_to_image(chat_id, user_id, prompt, file_id):
    prompt = (prompt or "").strip()
    if len(prompt) < 3:
        send_message(chat_id, "Tell me how to change the photo, e.g. \u201cmake it sunset\u201d.")
        return
    allowed, reason, _, edits_used = usage.check_and_count(
        user_id, "edit", TXT_LIMIT, EDIT_LIMIT, cloudflare.NEURONS_KLEIN_EDIT_1024
    )
    if not allowed:
        send_message(chat_id, reason)
        return
    send_message(chat_id, f"Editing it... ({edits_used}/{EDIT_LIMIT} edits used today)")
    try:
        raw = download_telegram_photo(file_id)
        resized = resize_input_for_klein(raw)
        image_bytes, neurons = cloudflare.image_to_image(
            CF_ACCOUNT_ID, CF_TOKEN, prompt, resized
        )
        usage.add_neurons(neurons)
        send_photo(
            chat_id, image_bytes,
            f"Edited: \u201c{prompt}\u201d\nFLUX.2-klein-4b \u2022 1024\u00d71024 \u2022 ~{neurons:.1f} Neurons",
        )
    except Exception as e:
        log.exception("img2img failed")
        msg = str(e)
        if "429" in msg or "used up" in msg.lower():
            send_message(chat_id, "Today's free Cloudflare quota is used up for everyone. Try again after 00:00 UTC.")
            return
        send_message(chat_id, f"Sorry, that edit failed: {msg}\nTry again with a clear photo and a simple edit prompt.")


def handle_update(update):
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    user = msg.get("from") or {}
    user_id = user.get("id")
    if not chat_id or not user_id:
        return

    text = (msg.get("text") or "").strip()
    caption = (msg.get("caption") or "").strip()
    photos = msg.get("photo") or []

    # Photo handling first
    if photos:
        file_id = photos[-1]["file_id"]
        if caption:
            threading.Thread(
                target=do_image_to_image,
                args=(chat_id, user_id, caption, file_id),
                daemon=True,
            ).start()
        else:
            pending_photo[user_id] = file_id
            send_message(
                chat_id,
                "Got your photo. Now send the edit prompt as your next message, e.g. \u201cmake it sunset, oil painting\u201d.",
            )
        return

    if not text:
        return

    if text.startswith("/start"):
        send_message(
            chat_id,
            "Hi! I make pictures for free, on Cloudflare's free tier.\n\n"
            "\u2022 Text to image: just send a prompt, or /imagine <prompt>\n"
            "  e.g. /imagine a red fox in fresh snow\n"
            "\u2022 Image to image: send a photo with a caption describing the edit\n"
            "  e.g. photo + \u201cmake it sunset\u201d\n"
            "  Or send a photo, then send the edit prompt next.\n\n"
            f"Free limits: {TXT_LIMIT} pictures + {EDIT_LIMIT} edits per person per day, "
            "shared Cloudflare quota resets 00:00 UTC.\n"
            "Use /status to see what you have left, /help for examples.",
        )
        return

    if text.startswith("/help"):
        send_message(
            chat_id,
            "How to use me:\n\n"
            "/imagine a Bangladeshi rickshaw in Dhaka at sunset, cinematic\n"
            "\u2192 makes a new picture from your words.\n\n"
            "Send a photo, caption: turn this into a watercolour painting\n"
            "\u2192 edits your photo.\n\n"
            "/edit make the sky stormy\n"
            "\u2192 reply to a photo with this, same as a captioned photo.\n\n"
            "/status \u2192 your pictures/edits used today + free quota left.",
        )
        return

    if text.startswith("/status"):
        s = usage.status(user_id, TXT_LIMIT, EDIT_LIMIT)
        send_message(
            chat_id,
            f"Today ({s['day']}, UTC):\n"
            f"\u2022 You: {s['txt_used']}/{s['txt_limit']} pictures, {s['edits_used']}/{s['edit_limit']} edits\n"
            f"\u2022 Shared free quota: ~{s['neurons_left']:.0f} of {s['neurons_budget']:.0f} Neurons left\n"
            "Quota resets 00:00 UTC.",
        )
        return

    if text.startswith("/imagine") or text.startswith("/generate"):
        prompt = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""
        threading.Thread(
            target=do_text_to_image, args=(chat_id, user_id, prompt), daemon=True
        ).start()
        return

    if text.startswith("/edit"):
        prompt = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""
        reply = msg.get("reply_to_message") or {}
        reply_photos = reply.get("photo") or []
        if not reply_photos:
            send_message(
                chat_id,
                "Reply to a photo with /edit <prompt>, e.g. reply to your photo with:\n/edit make it sunset",
            )
            return
        threading.Thread(
            target=do_image_to_image,
            args=(chat_id, user_id, prompt, reply_photos[-1]["file_id"]),
            daemon=True,
        ).start()
        return

    # Plain text: pending edit, or imagine
    if user_id in pending_photo:
        file_id = pending_photo.pop(user_id)
        threading.Thread(
            target=do_image_to_image, args=(chat_id, user_id, text, file_id), daemon=True
        ).start()
        return

    threading.Thread(
        target=do_text_to_image, args=(chat_id, user_id, text), daemon=True
    ).start()


@app.route("/", methods=["GET"])
def health():
    return "ok - image bot webhook alive", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    if WEBHOOK_SECRET:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if secret != WEBHOOK_SECRET:
            return jsonify({"ok": False, "error": "bad secret"}), 403
    update = request.get_json(force=True, silent=True) or {}
    # Answer fast, work in background - Telegram retries slow webhooks.
    threading.Thread(target=handle_update, args=(update,), daemon=True).start()
    return jsonify({"ok": True}), 200


if __name__ == "__main__":
    if not TELEGRAM_TOKEN:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN")
    if not CF_ACCOUNT_ID or not CF_TOKEN:
        raise SystemExit("Missing CF_ACCOUNT_ID or CF_API_TOKEN")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)

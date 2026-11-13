"""
Telegram image bot - Cloudflare Workers AI (FLUX), free tier.

Text-to-image:  /imagine a red fox in fresh snow
                or just send the prompt as a plain message
Image-to-image: send a photo WITH a caption describing the edit,
                e.g. photo + caption "make it sunset, oil painting style"
                or send a photo, then send the edit prompt as the next message.
                You can also reply to a photo with /edit <prompt>.

Commands: /start /help /imagine <prompt> /edit <prompt> (reply to photo) /status

Free limits (Cloudflare, Nov 2026):
- 10,000 Neurons/day, shared across all users, resets 00:00 UTC, no card.
- ~173 text pictures/day at 1024x1024, ~91 edits/day at 1024x1024.
- Per-user caps in this bot: 5 pictures + 3 edits per day (change in .env).

Setup: copy .env.example to .env, fill in the 3 values, then:
    pip install -r requirements.txt
    python bot.py

Needs: a Telegram token from @BotFather, and a free Cloudflare
account ID + API token with Workers AI permission.
See README.md for the exact steps. No token is stored in this code.
"""

import asyncio
import io
import logging
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from PIL import Image
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import cloudflare
import pollinations
from usage import UsageStore

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("image-bot")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TXT_LIMIT = int(os.environ.get("DAILY_TXT_LIMIT", "5"))
EDIT_LIMIT = int(os.environ.get("DAILY_EDIT_LIMIT", "3"))

# Pending edit: user_id -> Telegram file_id of the photo they sent
# without a caption. The next text message is treated as the edit prompt.
pending_photo: dict[int, str] = {}

usage = UsageStore(os.environ.get("USAGE_DB", "usage.db"))


def resize_input_for_klein(image_bytes: bytes) -> bytes:
    """
    klein-4b input images must be SMALLER than 512x512
    (Cloudflare changelog, 15 Jan 2026). Resize to max 500px,
    keep aspect ratio, save as JPEG.
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img.thumbnail((500, 500), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


async def send_photo_bytes(update: Update, image_bytes: bytes, caption: str):
    await update.message.reply_photo(
        photo=io.BytesIO(image_bytes), caption=caption
    )


async def do_text_to_image(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           prompt: str):
    user = update.effective_user
    prompt = (prompt or "").strip()
    if len(prompt) < 3:
        await update.message.reply_text("Send a longer prompt, e.g.\n/imagine a red fox in fresh snow")
        return
    if len(prompt) > 2000:
        await update.message.reply_text("That prompt is too long - keep it under 2000 characters.")
        return

    allowed, reason, txt_used, _ = usage.check_and_count(
        user.id, "txt", TXT_LIMIT, EDIT_LIMIT, cloudflare.NEURONS_SCHNELL_1024
    )
    if not allowed:
        await update.message.reply_text(reason)
        return

    wait = await update.message.reply_text(
        f"Drawing it... ({txt_used}/{TXT_LIMIT} pictures used today)"
    )
    try:
        account_id, token = cloudflare.env_config()
        image_bytes, neurons = await asyncio.to_thread(
            cloudflare.text_to_image, account_id, token, prompt
        )
        usage.add_neurons(neurons)
        await wait.delete()
        await send_photo_bytes(
            update, image_bytes,
            f"“{prompt}”\nFLUX.1-schnell • 1024×1024 • ~{neurons:.1f} Neurons",
        )
    except Exception as e:
        await wait.delete()
        log.exception("txt2img failed")
        msg = str(e)
        # Optional Pollinations fallback, only if a key is configured.
        if "429" in msg or "used up" in msg.lower():
            if pollinations.is_configured():
                try:
                    await update.message.reply_text(
                        "Cloudflare free quota is used up - trying the Pollinations fallback..."
                    )
                    image_bytes = await asyncio.to_thread(
                        pollinations.text_to_image, prompt
                    )
                    await send_photo_bytes(
                        update, image_bytes,
                        f"“{prompt}”\nPollinations fallback • flux",
                    )
                    return
                except Exception:
                    log.exception("Pollinations fallback failed")
            await update.message.reply_text(
                "Today's free Cloudflare quota is used up for everyone. "
                "Try again after 00:00 UTC.\n\n"
                "If this keeps happening, the fix is a per-user cap (already on: "
                f"{TXT_LIMIT} pictures/day) or a small paid top-up - see README."
            )
            return
        await update.message.reply_text(
            f"Sorry, that picture failed: {msg}\nTry a simpler prompt, or try again in a minute."
        )


async def do_image_to_image(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            prompt: str, file_id: str):
    user = update.effective_user
    prompt = (prompt or "").strip()
    if len(prompt) < 3:
        await update.message.reply_text("Tell me how to change the photo, e.g. “make it sunset”.")
        return

    allowed, reason, _, edits_used = usage.check_and_count(
        user.id, "edit", TXT_LIMIT, EDIT_LIMIT, cloudflare.NEURONS_KLEIN_EDIT_1024
    )
    if not allowed:
        await update.message.reply_text(reason)
        return

    wait = await update.message.reply_text(
        f"Editing it... ({edits_used}/{EDIT_LIMIT} edits used today)"
    )
    try:
        account_id, token = cloudflare.env_config()
        tg_file = await context.bot.get_file(file_id)
        raw = await tg_file.download_as_bytearray()
        resized = resize_input_for_klein(bytes(raw))
        image_bytes, neurons = await asyncio.to_thread(
            cloudflare.image_to_image, account_id, token, prompt, resized
        )
        usage.add_neurons(neurons)
        await wait.delete()
        await send_photo_bytes(
            update, image_bytes,
            f"Edited: “{prompt}”\nFLUX.2-klein-4b • 1024×1024 • ~{neurons:.1f} Neurons",
        )
    except Exception as e:
        await wait.delete()
        log.exception("img2img failed")
        msg = str(e)
        if "429" in msg or "used up" in msg.lower():
            await update.message.reply_text(
                "Today's free Cloudflare quota is used up for everyone. "
                "Try again after 00:00 UTC."
            )
            return
        await update.message.reply_text(
            "Sorry, that edit failed. Two things to check:\n"
            "1. The photo must be under 512×512 for the input - the bot resizes it, but a very small or odd photo can still fail.\n"
            f"2. Cloudflare said: {msg}\n"
            "Try again with a clear photo and a simple edit prompt."
        )


# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I make pictures for free, on Cloudflare's free tier.\n\n"
        "• Text to image: just send a prompt, or /imagine <prompt>\n"
        "  e.g. /imagine a red fox in fresh snow\n"
        "• Image to image: send a photo with a caption describing the edit\n"
        "  e.g. photo + “make it sunset”\n"
        "  Or send a photo, then send the edit prompt next.\n\n"
        f"Free limits: {TXT_LIMIT} pictures + {EDIT_LIMIT} edits per person per day, "
        "shared Cloudflare quota resets 00:00 UTC.\n"
        "Use /status to see what you have left, /help for examples."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "How to use me:\n\n"
        "/imagine a Bangladeshi rickshaw in Dhaka at sunset, cinematic\n"
        "→ makes a new picture from your words.\n\n"
        "Send a photo, caption: turn this into a watercolour painting\n"
        "→ edits your photo. Keep the caption simple: what to change, what style.\n\n"
        "/edit make the sky stormy\n"
        "→ reply to a photo with this, same as a captioned photo.\n\n"
        "/status → your pictures/edits used today + free quota left.\n\n"
        "Good prompts name the subject, place, light and style. "
        "Edits work best for style, background and light - not for tiny text."
    )


async def imagine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args or [])
    await do_text_to_image(update, context, prompt)


async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args or [])
    reply = update.message.reply_to_message
    if not reply or not reply.photo:
        await update.message.reply_text(
            "Reply to a photo with /edit <prompt>, e.g. reply to your photo with:\n/edit make it sunset"
        )
        return
    await do_image_to_image(update, context, prompt, reply.photo[-1].file_id)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = usage.status(update.effective_user.id, TXT_LIMIT, EDIT_LIMIT)
    await update.message.reply_text(
        f"Today ({s['day']}, UTC):\n"
        f"• You: {s['txt_used']}/{s['txt_limit']} pictures, {s['edits_used']}/{s['edit_limit']} edits\n"
        f"• Shared free quota: ~{s['neurons_left']:.0f} of {s['neurons_budget']:.0f} Neurons left\n"
        "Quota resets 00:00 UTC."
    )


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    caption = (update.message.caption or "").strip()
    if caption:
        await do_image_to_image(update, context, caption, photo.file_id)
    else:
        pending_photo[update.effective_user.id] = photo.file_id
        await update.message.reply_text(
            "Got your photo. Now send the edit prompt as your next message, e.g. “make it sunset, oil painting”."
        )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    # If they just sent a photo without a caption, this text is the edit prompt.
    if user_id in pending_photo:
        file_id = pending_photo.pop(user_id)
        await do_image_to_image(update, context, text, file_id)
        return
    # Otherwise, plain text is a text-to-image prompt.
    await do_text_to_image(update, context, text)


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit(
            "Missing TELEGRAM_BOT_TOKEN. Copy .env.example to .env and "
            "paste the token from @BotFather - see README.md."
        )
    # Fail fast if Cloudflare is not configured, instead of failing
    # on the first picture.
    cloudflare.env_config()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("imagine", imagine))
    app.add_handler(CommandHandler("generate", imagine))
    app.add_handler(CommandHandler("edit", edit_cmd))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Bot starting. txt limit %s/day, edit limit %s/day.", TXT_LIMIT, EDIT_LIMIT)
    app.run_polling()


if __name__ == "__main__":
    main()

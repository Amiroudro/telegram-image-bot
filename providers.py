"""
Multi-provider failover for text-to-image.

Chain, in order:
1. Cloudflare Workers AI - FLUX.1-schnell, 1024x1024 (primary).
   Skipped cleanly if CF_ACCOUNT_ID / CF_API_TOKEN are not set.
2. Pollinations - key-based if POLLINATIONS_API_KEY is set, otherwise
   the legacy no-key endpoint (slow, Sana, 768x768).
3. AI Horde - anonymous key, queue-based, 512x512.

Only a DELIVERED picture counts against the user's daily cap - see
bot.py, which records the cap only after this function returns.
Each provider failure is logged and the chain moves on; only if ALL
three fail does this raise, with one line per provider.
"""

import logging

import aihorde
import cloudflare
import pollinations

log = logging.getLogger("image-bot.providers")

LABEL_CLOUDFLARE = "Cloudflare FLUX.1-schnell"
LABEL_POLLINATIONS = "Pollinations (fallback)"
LABEL_HORDE = "AI Horde (fallback)"


def generate_text_image(prompt: str) -> tuple[bytes, str, float | None]:
    """
    Try each engine in order; return (image_bytes, provider_label,
    neurons_or_None). Neurons is only meaningful for Cloudflare.
    Raises RuntimeError if every engine fails, listing each reason.
    """
    errors: list[tuple[str, str]] = []

    # 1. Cloudflare (primary)
    try:
        account_id, token = cloudflare.env_config()
        image_bytes, neurons = cloudflare.text_to_image(account_id, token, prompt)
        return image_bytes, LABEL_CLOUDFLARE, neurons
    except Exception as e:
        errors.append((LABEL_CLOUDFLARE, _short(e)))
        log.warning("Cloudflare txt2img failed, trying Pollinations: %s", e)

    # 2. Pollinations (key-based if configured, else legacy no-key)
    try:
        image_bytes = pollinations.text_to_image(prompt)
        return image_bytes, LABEL_POLLINATIONS, None
    except Exception as e:
        errors.append((LABEL_POLLINATIONS, _short(e)))
        log.warning("Pollinations txt2img failed, trying AI Horde: %s", e)

    # 3. AI Horde (anonymous, queue-based)
    try:
        image_bytes = aihorde.text_to_image(prompt)
        return image_bytes, LABEL_HORDE, None
    except Exception as e:
        errors.append((LABEL_HORDE, _short(e)))
        log.warning("AI Horde txt2img failed: %s", e)

    detail = "; ".join(f"{label}: {msg}" for label, msg in errors)
    raise RuntimeError(f"All image engines failed. {detail}")


def _short(exc: Exception, limit: int = 160) -> str:
    """One-line summary of an exception for the user-facing message."""
    msg = " ".join(str(exc).split())
    return msg[:limit] if len(msg) > limit else msg

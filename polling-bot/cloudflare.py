"""
Cloudflare Workers AI client for the Telegram image bot.

Best overall pick from deep research, Nov 2026:
- Free quota: 10,000 Neurons/day, resets 00:00 UTC, no credit card.
  Source: https://developers.cloudflare.com/workers-ai/platform/pricing/
- Text-to-image: @cf/black-forest-labs/flux-1-schnell
  1024x1024, 4 steps = 57.6 neurons -> ~173/day
  512x512, 4 steps = 43.2 neurons -> ~231/day
- Image-to-image / edit: @cf/black-forest-labs/flux-2-klein-4b
  Flat (not per-step): 5.37 neurons/input tile + 26.05/output tile
  1024 output (4 tiles) + 1 input tile = ~109.6 neurons -> ~91 edits/day
  Steps are fixed at 4 for klein-4b, do not send steps.

VERIFIED 2026-11-13 from Cloudflare changelog, 15 Jan 2026:
https://developers.cloudflare.com/changelog/post/2026-01-15-flux-2-klein-4b-workers-ai/
- klein-4b uses multipart/form-data EVEN for prompt-only calls.
- Input fields are input_image_0, input_image_1, input_image_2, input_image_3.
- All input images must be smaller than 512x512. We resize to max 500px.
- Prompt can reference images by index, e.g. "take the subject of image 1
  and style it like image 0".

WARNING: one third-party test (Nov 2026) used the field name `input_image`
(without _0) and got back an image unrelated to the input - i.e. it was
treated as text-to-image, not an edit. Use input_image_0, as in the
official Cloudflare docs above. If an edit ever comes back unrelated to
your photo, that field name is the first thing to check.

No live generation test was run here - no Cloudflare account / token
was provided. All limits are from the docs above.
"""

import base64
import io
import os

import requests

CF_BASE = "https://api.cloudflare.com/client/v4/accounts"

SCHNELL_MODEL = "@cf/black-forest-labs/flux-1-schnell"
KLEIN_MODEL = "@cf/black-forest-labs/flux-2-klein-4b"

# Neuron estimates, from Cloudflare pricing page, Nov 2026.
# schnell: 4.80 neurons / 512 tile + 9.60 / step
#   1024x1024 (4 tiles) at 4 steps = 4*4.80 + 4*9.60 = 57.6
NEURONS_SCHNELL_1024 = 57.6
NEURONS_SCHNELL_512 = 43.2
# klein-4b: 5.37 / input tile + 26.05 / output tile, flat
#   1024 output (4 tiles) = 104.2, + 1 input tile (500px) = 109.57
NEURONS_KLEIN_EDIT_1024 = 109.6
NEURONS_KLEIN_TXT_1024 = 104.2


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _decode_result(response: requests.Response) -> bytes:
    """Workers AI returns either raw image bytes or JSON with base64."""
    ctype = response.headers.get("Content-Type", "")
    if ctype.startswith("image/"):
        return response.content
    data = response.json()
    if not data.get("success", True):
        errors = data.get("errors", [])
        raise RuntimeError(f"Cloudflare error: {errors}")
    result = data.get("result")
    if isinstance(result, dict) and result.get("image"):
        return base64.b64decode(result["image"])
    if isinstance(result, str):
        return base64.b64decode(result)
    raise RuntimeError(f"Unexpected Cloudflare response: {str(data)[:500]}")


def text_to_image(account_id: str, token: str, prompt: str,
                  width: int = 1024, height: int = 1024,
                  steps: int = 4, seed: int | None = None) -> tuple[bytes, float]:
    """
    FLUX.1-schnell text-to-image, JSON API.
    Returns (image_bytes, neurons_estimated).
    """
    url = f"{CF_BASE}/{account_id}/ai/run/{SCHNELL_MODEL}"
    payload: dict = {"prompt": prompt, "steps": steps}
    if width != 1024 or height != 1024:
        # schnell pricing in the research is for 1024/512; keep the
        # estimate honest and only use the two tested sizes.
        payload["width"] = width
        payload["height"] = height
    if seed is not None:
        payload["seed"] = seed
    r = requests.post(url, headers=_headers(token), json=payload, timeout=120)
    if r.status_code in (401, 403):
        raise RuntimeError("Cloudflare auth failed (401/403). Check CF_API_TOKEN - it needs Workers AI permission - and CF_ACCOUNT_ID.")
    if r.status_code == 429:
        raise RuntimeError("Cloudflare rate limit / daily Neurons used up (429). Free quota resets 00:00 UTC.")
    r.raise_for_status()
    neurons = NEURONS_SCHNELL_1024 if (width, height) == (1024, 1024) else NEURONS_SCHNELL_512
    return _decode_result(r), neurons


def image_to_image(account_id: str, token: str, prompt: str,
                   image_bytes: bytes,
                   width: int = 1024, height: int = 1024) -> tuple[bytes, float]:
    """
    FLUX.2-klein-4b image-to-image / edit, multipart API.
    image_bytes must already be resized to <512x512 (see bot.py).
    Returns (image_bytes, neurons_estimated).
    """
    url = f"{CF_BASE}/{account_id}/ai/run/{KLEIN_MODEL}"
    # requests sets the multipart boundary automatically - do NOT set
    # Content-Type manually.
    files = {
        "input_image_0": ("input.jpg", io.BytesIO(image_bytes), "image/jpeg"),
    }
    data = {
        "prompt": prompt,
        "width": str(width),
        "height": str(height),
    }
    r = requests.post(url, headers=_headers(token), files=files, data=data, timeout=180)
    if r.status_code in (401, 403):
        raise RuntimeError("Cloudflare auth failed (401/403). Check CF_API_TOKEN and CF_ACCOUNT_ID.")
    if r.status_code == 429:
        raise RuntimeError("Cloudflare rate limit / daily Neurons used up (429). Free quota resets 00:00 UTC.")
    if r.status_code == 500 or "5006" in r.text[:500]:
        raise RuntimeError("Cloudflare rejected the multipart call (5006). This usually means the model wants multipart form-data - which this code already sends. Check the model page for a format change.")
    r.raise_for_status()
    return _decode_result(r), NEURONS_KLEIN_EDIT_1024


def klein_text_to_image(account_id: str, token: str, prompt: str,
                        width: int = 1024, height: int = 1024) -> tuple[bytes, float]:
    """
    klein-4b text-to-image, multipart with prompt only.
    More expensive than schnell (104.2 vs 57.6 neurons at 1024),
    so the bot uses schnell by default. Kept here as a fallback
    if schnell is ever unavailable.
    """
    url = f"{CF_BASE}/{account_id}/ai/run/{KLEIN_MODEL}"
    data = {"prompt": prompt, "width": str(width), "height": str(height)}
    r = requests.post(url, headers=_headers(token), data=data, timeout=180)
    r.raise_for_status()
    return _decode_result(r), NEURONS_KLEIN_TXT_1024


def env_config() -> tuple[str, str]:
    account_id = os.environ.get("CF_ACCOUNT_ID", "").strip()
    token = os.environ.get("CF_API_TOKEN", "").strip()
    if not account_id or not token:
        raise RuntimeError("Missing CF_ACCOUNT_ID or CF_API_TOKEN. Copy .env.example to .env and fill them in - see README.md.")
    return account_id, token

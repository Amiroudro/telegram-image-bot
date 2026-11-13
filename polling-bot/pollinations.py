"""
OPTIONAL fallback: Pollinations.ai (gen.pollinations.ai).

Only used if POLLINATIONS_API_KEY is set in .env AND Cloudflare
fails with a quota error. Do not rely on it as the free backbone.

Nov 2026 facts from the deep research:
- A key is now required (401 without one). Billed in "pollen",
  1 pollen = $1. Free part is a one-time Quest Pollen balance,
  ~3 pollen reported - NOT a recurring free quota.
- flux.1-schnell ~0.002 pollen/image, z-image-turbo ~0.004.
- Premium edit models (Seedream, Nano Banana, GPT-Image) are
  paid-only and cannot be paid with quest pollen.
- Legacy no-key image.pollinations.ai is degraded (Sana only,
  ~1 image / 40s). Do not use it.

If no key is set, the bot simply reports the Cloudflare quota
message and stops. That is the intended free behaviour.
"""

import os

import requests

BASE = "https://gen.pollinations.ai"


def is_configured() -> bool:
    return bool(os.environ.get("POLLINATIONS_API_KEY", "").strip())


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['POLLINATIONS_API_KEY'].strip()}"}


def text_to_image(prompt: str, width: int = 1024, height: int = 1024) -> bytes:
    r = requests.post(
        f"{BASE}/v1/images/generations",
        headers=_headers(),
        json={"prompt": prompt, "model": "flux", "width": width, "height": height},
        timeout=120,
    )
    r.raise_for_status()
    data = r.json()
    # OpenAI-compatible shape: data[0].url (or base64)
    import base64 as _b64
    item = data["data"][0]
    if item.get("b64_json"):
        return _b64.b64decode(item["b64_json"])
    url = item["url"]
    img = requests.get(url, timeout=60)
    img.raise_for_status()
    return img.content


def image_to_image(prompt: str, image_bytes: bytes) -> bytes:
    import io
    files = {"image": ("input.jpg", io.BytesIO(image_bytes), "image/jpeg")}
    data = {"prompt": prompt, "model": "kontext", "size": "1024x1024"}
    r = requests.post(
        f"{BASE}/v1/images/edits",
        headers=_headers(),
        files=files,
        data=data,
        timeout=180,
    )
    r.raise_for_status()
    # Edits endpoint returns image bytes directly.
    if r.headers.get("Content-Type", "").startswith("image/"):
        return r.content
    return r.content

"""
Fallback: Pollinations.ai - text-to-image (and edits, key-based only).

Two paths, best first:
1. KEY-BASED (gen.pollinations.ai, OpenAI-compatible) - only if
   POLLINATIONS_API_KEY is set. Billed in "pollen", 1 pollen = $1.
   Free part is a one-time Quest Pollen balance (~3 pollen reported),
   NOT a recurring free quota. flux model, 1024x1024.
2. LEGACY NO-KEY (image.pollinations.ai) - used when no key is set.
   Nov 2026 facts from the deep research: the legacy endpoint is
   degraded (Sana model, ~1 image / 40s). It is now used ANYWAY as a
   fallback, because a slow lower-quality picture beats no picture
   when Cloudflare is down.

VERIFIED LIVE 2026-11-13 from this machine (legacy endpoint):
- GET https://image.pollinations.ai/prompt/<url-encoded prompt>
      ?width=1024&height=1024&nologo=true
  -> 200 image/jpeg in ~2.6 s, 65,686 bytes.
  SURPRISE: it returned 768x768, not the requested 1024x1024 -
  the legacy endpoint caps at 768. The bot sends the image as-is.
"""

import os
import urllib.parse

import requests

BASE = "https://gen.pollinations.ai"
LEGACY_BASE = "https://image.pollinations.ai"


def is_configured() -> bool:
    """True if a Pollinations API key is set (key-based path)."""
    return bool(os.environ.get("POLLINATIONS_API_KEY", "").strip())


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['POLLINATIONS_API_KEY'].strip()}"}


def _legacy_text_to_image(prompt: str, width: int = 1024,
                          height: int = 1024) -> bytes:
    """No-key legacy endpoint. Slow, Sana model, max 768x768 in practice."""
    url = (f"{LEGACY_BASE}/prompt/{urllib.parse.quote(prompt)}"
           f"?width={width}&height={height}&nologo=true")
    r = requests.get(url, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(
            f"Pollinations legacy failed ({r.status_code}): {r.text[:200]}")
    if not r.headers.get("Content-Type", "").startswith("image/"):
        raise RuntimeError(
            f"Pollinations legacy returned non-image: {r.text[:200]}")
    return r.content


def text_to_image(prompt: str, width: int = 1024, height: int = 1024) -> bytes:
    if is_configured():
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
    return _legacy_text_to_image(prompt, width, height)


def image_to_image(prompt: str, image_bytes: bytes) -> bytes:
    """Edits need the key-based API - the legacy endpoint has no edit path."""
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

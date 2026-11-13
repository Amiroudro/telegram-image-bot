"""
AI Horde client (stablehorde.net, API v2) - free, no signup.

Anonymous API key "0000000000" is the public anonymous key documented
by AI Horde itself - it is not a secret. Anonymous requests get the
lowest queue priority and are capped at 580x580 while demand is high,
so this client asks for 512x512.

VERIFIED LIVE 2026-11-13 from this machine:
- POST https://stablehorde.net/api/v2/generate/async
  -> 202 {"id": "<uuid>", "kudos": 6.0}
  (NOTE: the image endpoint is /generate/async, NOT
  /generate/async/text2img - that path hangs/404s. The /text/async
  suffix is for the TEXT endpoint only.)
- GET https://stablehorde.net/api/v2/generate/status/{id}
  -> {"done": true, "generations": [{"img": "<r2 url>", "seed": ...,
     "censored": false, "model": "SDXL 1.0", "state": "ok", ...}]}
- Image downloads as WEBP (512x512, ~63 KB) - converted to JPEG here
  because Telegram photos are reliably JPEG/PNG.
- One real job: submitted, done in under ~90 s including queue.
- aihorde.net as a base did NOT answer the submit POST (read timeout);
  stablehorde.net is the base to use.

Limits to be honest about: queue-based, so a picture can take
seconds or several minutes; anonymous size cap 580x580; some prompts
can come back censored (state != "ok" / censored true) and that is
treated here as a failure.
"""

import io
import os
import time

import requests
from PIL import Image

BASE = "https://stablehorde.net/api/v2"

# Public anonymous key, documented by AI Horde. Not a secret.
ANON_KEY = "0000000000"
DEFAULT_MODEL = "SDXL 1.0"

# Anonymous requests are capped at 580x580 while demand is high
# (a 768x768 request is refused). 512x512 is the safe size.
WIDTH = 512
HEIGHT = 512


def _api_key() -> str:
    return os.environ.get("AIHORDE_API_KEY", "").strip() or ANON_KEY


def _model() -> str:
    return os.environ.get("AIHORDE_MODEL", "").strip() or DEFAULT_MODEL


def _headers() -> dict:
    return {
        "apikey": _api_key(),
        "Content-Type": "application/json",
        "Client-Agent": "telegram-image-bot/1.0",
    }


def _to_jpeg(image_bytes: bytes) -> bytes:
    """Horde serves WEBP; Telegram photos are reliably JPEG/PNG."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue()


def text_to_image(prompt: str, timeout: int = 300,
                  poll_interval: int = 10) -> bytes:
    """
    Submit an async job, poll until done, download and JPEG-convert.
    Returns image bytes (JPEG, 512x512 for anonymous use).
    Raises RuntimeError on submit failure, timeout, censorship or
    a faulted generation.
    """
    payload = {
        "prompt": prompt,
        "params": {
            "sampler_name": "k_euler",
            "steps": 20,
            "width": WIDTH,
            "height": HEIGHT,
            "cfg_scale": 7.0,
        },
        "nsfw": False,
        "censor_nsfw": True,
        "models": [_model()],
        "r2": True,
    }
    try:
        r = requests.post(f"{BASE}/generate/async", headers=_headers(),
                          json=payload, timeout=90)
    except requests.RequestException as e:
        raise RuntimeError(f"AI Horde submit failed: {e}")
    if r.status_code not in (200, 202):
        raise RuntimeError(
            f"AI Horde submit failed ({r.status_code}): {r.text[:200]}")
    job = r.json()
    job_id = job.get("id")
    if not job_id:
        raise RuntimeError(f"AI Horde submit returned no job id: {str(job)[:200]}")

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = requests.get(f"{BASE}/generate/status/{job_id}", timeout=30)
            s.raise_for_status()
            st = s.json()
        except requests.RequestException as e:
            # A single failed poll is not fatal - the job may still finish.
            time.sleep(poll_interval)
            continue
        if st.get("done"):
            gens = st.get("generations") or []
            if not gens:
                raise RuntimeError("AI Horde finished with no image.")
            gen = gens[0]
            if gen.get("state") != "ok" or gen.get("censored"):
                raise RuntimeError(
                    "AI Horde blocked this prompt (censored/faulted).")
            img_url = gen.get("img")
            if not img_url:
                raise RuntimeError("AI Horde finished with no image URL.")
            try:
                img_r = requests.get(img_url, timeout=120)
                img_r.raise_for_status()
            except requests.RequestException as e:
                raise RuntimeError(f"AI Horde download failed: {e}")
            return _to_jpeg(img_r.content)
        time.sleep(poll_interval)
    raise RuntimeError(
        f"AI Horde timed out after {timeout}s in the queue - try again later.")

# Telegram Image Bot — Cloudflare Workers AI (FLUX), free

Text-to-image **and** image-to-image, on the best free option found by deep research in Nov 2026.

**Best overall: Cloudflare Workers AI**
- Free quota: **10,000 Neurons/day, resets 00:00 UTC, no credit card**
  Source: https://developers.cloudflare.com/workers-ai/platform/pricing/
- Text-to-image: FLUX.1-schnell, 1024×1024, 4 steps = **57.6 Neurons → ~173 pictures/day**
- Image-to-image / edit: FLUX.2-klein-4b, 1024×1024 output + 1 input = **~109.6 Neurons → ~91 edits/day**
- Per-user caps in this bot: **5 pictures + 3 edits per person per day** — so one user cannot drain the shared quota.

**Honest limit:** this is *not* unlimited. It is ~173 pictures *or* ~91 edits a day, shared across all users. After that, Cloudflare returns an error until 00:00 UTC. Truly unlimited is only local: your own PC + ComfyUI + FLUX.1-schnell (needs ~12 GB VRAM).

**Correction:** Pollinations was previously suggested as free with no key. That is no longer true in Nov 2026 — it now needs an API key, is billed in pollen, and the free part is a one-time Quest Pollen balance (~3 pollen reported). It is included here only as an *optional* fallback, not the backbone.

## What the bot does

| You send | Bot does |
|---|---|
| `/imagine a red fox in fresh snow` or just a plain prompt | New picture, FLUX.1-schnell, 1024×1024 |
| A photo **with a caption** — e.g. “make it sunset” | Edits your photo, FLUX.2-klein-4b |
| A photo, then the edit prompt as the next message | Same edit |
| Reply to a photo with `/edit make it stormy` | Same edit |
| `/status` | Your pictures/edits used today + free quota left |
| `/start`, `/help` | Help and examples |

Good prompts name the subject, place, light and style. Edits work best for style, background and light — not for tiny text.

## Setup — 5 minutes, 3 keys

### 1. Telegram token — free
1. In Telegram, open **@BotFather**, send `/newbot`, follow it, copy the token.
2. It looks like `123456:ABC-DEF...` — keep it secret.

### 2. Cloudflare — free, no card
1. Create a free account at https://dash.cloudflare.com
2. **Account ID:** on the dashboard, right sidebar — copy it.
3. **API token:** top-right profile → My Profile → API Tokens → Create Token → Custom token.
   Permissions: **Account — Workers AI — Edit**. Copy the token once — you will not see it again.

### 3. Run it on your PC
```bash
cd telegram-image-bot
cp .env.example .env
# edit .env and paste the 3 values above
pip install -r requirements.txt
python bot.py
```
Open your bot in Telegram and send:
```
/imagine a Bangladeshi rickshaw in Dhaka at sunset, cinematic
```
Then send a photo with the caption `turn this into a watercolour painting`.

Test the logic without any keys:
```bash
python -m pytest test_bot.py -v
# or: python test_bot.py
```

## Hosting — so it stays online

Polling bots need a process that stays on. Best free options, Nov 2026:

- **Your own PC — $0, easiest to start.** Run `python bot.py` and leave the PC on. Downside: load-shedding / internet drops in Bangladesh stop the bot.
- **Oracle Cloud Always Free VPS — best free 24/7.** 2 OCPU + 12 GB RAM ARM, card + phone required for signup, idle instances can be reclaimed. Install Python there, copy this folder, run with `nohup python bot.py &` or a systemd service.
- **Render free — demo only.** Free web service sleeps after 15 min idle, first reply is slow. Fine to test, not for 24/7.

Do not pay for hosting until the bot has real users — the image quota, not hosting, is the binding limit.

## Files

- `bot.py` — Telegram handlers, caps, photo flow
- `cloudflare.py` — Workers AI client (schnell + klein-4b), neuron math
- `usage.py` — per-user caps + shared Neuron budget, SQLite (`usage.db`)
- `pollinations.py` — optional fallback, only if `POLLINATIONS_API_KEY` is set
- `test_bot.py` — offline tests, no network / no keys
- `.env.example` — copy to `.env`; `.env` is git-ignored, never share it

## Troubleshooting

- **“Missing TELEGRAM_BOT_TOKEN / CF_ACCOUNT_ID”** — you did not copy `.env.example` to `.env`, or a value is blank.
- **“Cloudflare auth failed (401/403)”** — wrong Account ID, or the API token has no Workers AI permission. Recreate the token as above.
- **“Today's free quota is used up”** — 10,000 Neurons spent. Wait until 00:00 UTC, or lower `DAILY_TXT_LIMIT` / `DAILY_EDIT_LIMIT` in `.env`.
- **Edit comes back unrelated to your photo** — the input field name is the first suspect. This bot uses `input_image_0`, verified from Cloudflare's own changelog, 15 Jan 2026: https://developers.cloudflare.com/changelog/post/2026-01-15-flux-2-klein-4b-workers-ai/ — input images must be **smaller than 512×512**, which the bot enforces by resizing to max 500px. One third-party test that used `input_image` (without `_0`) got an unrelated image, i.e. it was treated as text-to-image.
- **Edit fails on a very small / odd photo** — try a clearer photo and a simple prompt first.
- **FLUX.2-dev is not used** — it costs ~3,375 Neurons per 1024 edit (~3/day). klein-4b is the practical free edit model. Dev is for a few “best quality” edits only, if you ever pay for overage.

## Costs if you outgrow free

Cloudflare Paid overage: $0.011 / 1,000 Neurons — one 1024 schnell picture is ~$0.00063. Pollinations / fal.ai / Replicate FLUX-schnell is ~$0.003/image. That is ~$3 per 1,000 pictures on the paid fallbacks.

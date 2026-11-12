# Telegram Image Bot — Cloudflare Workers version

Free, no card, no sleep, 24/7. Same Cloudflare account that
already makes the pictures also hosts the bot.

How it works: Telegram sends each message to your Worker
(`POST /webhook`). The Worker answers 200 immediately and
puts the job in a Queue. The Queue consumer then has 15
minutes to draw the picture with Workers AI and send it back.
That Queue step is the whole trick — a plain Worker only gets
30 seconds after answering, and a FLUX picture takes 20-30s.

Free limits, honest (Cloudflare, Nov 2026):
- Workers: 100,000 requests/day
- Workers AI: 10,000 Neurons/day, resets 00:00 UTC
  ~173 text pictures/day, ~91 edits/day at 1024x1024
  Per-user caps in this bot: 5 pictures + 3 edits/day
- Queues: 10,000 operations/day free, 1 job = 3 ops
  (~3,300 jobs/day — Neurons run out first, not the queue)
- KV: 100,000 reads/day, 1,000 writes/day — used for caps.
  If you skip KV, caps are off and the bot still works.

## What you need

1. A free Cloudflare account — you already have one
2. Telegram bot token — from @BotFather, for @clfreimagegenrbot
3. Node.js 18+ on your PC, or deploy from the Cloudflare dashboard

No token goes in code or in GitHub. Telegram token goes in as
a Worker secret only. Workers AI needs no token inside the
Worker — it uses the AI binding.
Image edits also need CF_API_TOKEN + CF_ACCOUNT_ID as Worker
secrets (the klein-4b edit API is multipart, which the AI
binding does not expose — so edits call the REST API).
Text-to-image works with the AI binding alone.

## Deploy — with Wrangler (recommended)

```bash
cd cloudflare-workers
npm install
npx wrangler login
# 1. Queue (also create the dead-letter queue)
npx wrangler queues create telegram-image-jobs
npx wrangler queues create telegram-image-jobs-dlq
# 2. KV (optional but recommended for caps)
npx wrangler kv namespace create USAGE_KV
#    Paste the id it prints into wrangler.toml, replacing
#    PLACEHOLDER_KV_ID. If you skip this step, delete the
#    [[kv_namespaces]] block from wrangler.toml.
# 3. Secrets - paste when asked, never in code
npx wrangler secret put TELEGRAM_TOKEN
npx wrangler secret put CF_API_TOKEN
npx wrangler secret put CF_ACCOUNT_ID
npx wrangler secret put WEBHOOK_SECRET
#    WEBHOOK_SECRET: any random long string you make up
# 4. Deploy
npx wrangler deploy
#    Copy the URL it prints, e.g.
#    https://telegram-image-bot.<your-subdomain>.workers.dev
# 5. Point Telegram at it
curl "https://api.telegram.org/bot<TELEGRAM_TOKEN>/setWebhook?url=https://telegram-image-bot.<your-subdomain>.workers.dev/webhook&secret_token=<WEBHOOK_SECRET>&drop_pending_updates=true"
```

Check health first: open the Worker URL in a browser —
you should see `ok - image bot worker alive`.

## Deploy — from the Cloudflare dashboard (no terminal)

1. dash.cloudflare.com > Workers & Pages > Create > Worker
2. Name it `telegram-image-bot`, paste in `src/index.js`
3. Settings > Bindings: add AI binding `AI`, Queue producer
   `IMAGE_QUEUE` -> `telegram-image-jobs`, Queue consumer on
   the same queue, KV namespace `USAGE_KV` (create one first)
4. Settings > Variables: add secrets `TELEGRAM_TOKEN`,
   `CF_API_TOKEN`, `CF_ACCOUNT_ID`, `WEBHOOK_SECRET`
5. Deploy, copy the `*.workers.dev` URL, set the webhook
   with the curl command above.

## Test it

1. In Telegram, open @clfreimagegenrbot, send `/start`
2. Send: `/imagine a cat wearing sunglasses`
3. You should get a picture back in 20-40 seconds
4. Send a photo with caption `make it sunset` — edit back

If `/start` gets no reply after 2 minutes, check in order:
- Worker URL in a browser — does it say `ok - image bot worker alive`?
- Cloudflare dashboard > Workers > telegram-image-bot > Logs —
  look for errors, or "Missing TELEGRAM_TOKEN"
- Is the webhook set? Run:
  `curl https://api.telegram.org/bot<TOKEN>/getWebhookInfo`
  It should show your Worker URL with `/webhook`.

## To stop it / go back to polling

```bash
curl "https://api.telegram.org/bot<TOKEN>/deleteWebhook?drop_pending_updates=true"
```

Then run `bot.py` on your PC again if you want polling back.

## Files

- `src/index.js` — Worker: webhook fetch handler + queue consumer
- `wrangler.toml` — AI binding, Queue, KV, vars
- `package.json` — wrangler
- `README_WORKERS.md` — this file

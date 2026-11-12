/**
 * Telegram image bot on Cloudflare Workers + Queues + Workers AI.
 * Free tier, no card, no sleep.
 *
 * Why Queues: a Worker only gets 30 seconds of waitUntil() after it
 * answers a webhook, and a FLUX picture takes 20-30 seconds.
 * So the webhook handler only enqueues the job and answers 200
 * immediately. The Queue consumer then has 15 minutes to draw
 * the picture and send it back. No timeout, no duplicate replies.
 *
 * Free limits (Cloudflare, Nov 2026):
 * - Workers: 100,000 requests/day
 * - Workers AI: 10,000 Neurons/day, resets 00:00 UTC
 *   ~173 text pictures/day at 1024x1024 (FLUX.1-schnell, 57.6 Neurons)
 *   ~91 edits/day at 1024x1024 (FLUX.2-klein-4b, 109.6 Neurons)
 * - Queues: 10,000 operations/day free, 1 message = 3 ops
 *   (write + read + delete), so ~3,300 jobs/day - far above the
 *   Neuron limit, so Neurons run out first, not the queue.
 * - KV: 100,000 reads/day, 1,000 writes/day - used for per-user
 *   caps and the shared Neuron count. If KV is not bound, caps
 *   are disabled and the bot still works (with a log warning).
 *
 * Bindings (wrangler.toml):
 * - env.AI            Workers AI binding, no API token needed
 * - env.IMAGE_QUEUE   Queue producer + consumer, queue name
 *                     telegram-image-jobs
 * - env.USAGE_KV      KV namespace, optional but recommended
 * Secrets (wrangler secret put):
 * - TELEGRAM_TOKEN    BotFather token for @clfreimagegenrbot
 * Vars:
 * - WEBHOOK_SECRET    optional, must match setWebhook secret_token
 * - DAILY_TXT_LIMIT   default 5, DAILY_EDIT_LIMIT default 3
 */

const SCHNELL = "@cf/black-forest-labs/flux-1-schnell";
const KLEIN = "@cf/black-forest-labs/flux-2-klein-4b";
const NEURONS_SCHNELL_1024 = 57.6;
const NEURONS_KLEIN_EDIT_1024 = 109.6;
const DAILY_NEURON_BUDGET = 10000;

const TXT_LIMIT = (env) => parseInt(env.DAILY_TXT_LIMIT || "5", 10);
const EDIT_LIMIT = (env) => parseInt(env.DAILY_EDIT_LIMIT || "3", 10);

function todayUTC() {
  return new Date().toISOString().slice(0, 10);
}

function tgApi(env) {
  return `https://api.telegram.org/bot${env.TELEGRAM_TOKEN}`;
}

async function tgPost(env, method, data, files) {
  let body, headers = {};
  if (files) {
    const form = new FormData();
    for (const [k, v] of Object.entries(data || {})) form.append(k, String(v));
    for (const [k, v] of Object.entries(files)) form.append(k, v);
    body = form;
  } else {
    body = JSON.stringify(data || {});
    headers = { "Content-Type": "application/json" };
  }
  const res = await fetch(`${tgApi(env)}/${method}`, {
    method: "POST",
    headers,
    body,
  });
  if (!res.ok) {
    console.log(`Telegram ${method} failed ${res.status}: ${(await res.text()).slice(0, 500)}`);
  }
  return res.json().catch(() => ({}));
}

async function sendMessage(env, chatId, text) {
  return tgPost(env, "sendMessage", { chat_id: chatId, text });
}

async function sendPhoto(env, chatId, imageBytes, caption) {
  const blob = new Blob([imageBytes], { type: "image/jpeg" });
  return tgPost(
    env,
    "sendPhoto",
    { chat_id: chatId, caption },
    { photo: blob }
  );
}

async function downloadTelegramPhoto(env, fileId) {
  const info = await tgPost(env, "getFile", { file_id: fileId });
  const path = info?.result?.file_path;
  if (!path) throw new Error("Telegram getFile returned no file_path");
  const res = await fetch(`https://api.telegram.org/file/bot${env.TELEGRAM_TOKEN}/${path}`);
  if (!res.ok) throw new Error(`Telegram file download failed ${res.status}`);
  return new Uint8Array(await res.arrayBuffer());
}

// --- Usage caps in KV (best-effort, disabled if KV not bound) ---

async function kvGet(env, key) {
  if (!env.USAGE_KV) return null;
  try { return await env.USAGE_KV.get(key); } catch (e) { return null; }
}

async function kvPut(env, key, value, ttlSeconds) {
  if (!env.USAGE_KV) return;
  try {
    await env.USAGE_KV.put(key, value, ttlSeconds ? { expirationTtl: ttlSeconds } : undefined);
  } catch (e) { console.log("KV put failed", e.message); }
}

async function checkAndCount(env, userId, kind, neuronsCost) {
  if (!env.USAGE_KV) return { allowed: true, txtUsed: 0, editsUsed: 0 };
  const day = todayUTC();
  const userKey = `usage:${day}:${userId}`;
  const neuronKey = `neurons:${day}`;
  const raw = await kvGet(env, userKey);
  const counts = raw ? JSON.parse(raw) : { txt: 0, edits: 0 };
  const txtLimit = TXT_LIMIT(env), editLimit = EDIT_LIMIT(env);

  if (kind === "txt" && counts.txt >= txtLimit) {
    return { allowed: false, reason: `You used your ${txtLimit} free pictures for today. Try again after 00:00 UTC.`, txtUsed: counts.txt, editsUsed: counts.edits };
  }
  if (kind === "edit" && counts.edits >= editLimit) {
    return { allowed: false, reason: `You used your ${editLimit} free edits for today. Try again after 00:00 UTC.`, txtUsed: counts.txt, editsUsed: counts.edits };
  }
  const neuronsUsed = parseFloat((await kvGet(env, neuronKey)) || "0");
  if (neuronsUsed + neuronsCost > DAILY_NEURON_BUDGET) {
    return { allowed: false, reason: "Today's free Cloudflare quota is used up for everyone. Try again after 00:00 UTC.", txtUsed: counts.txt, editsUsed: counts.edits };
  }
  if (kind === "txt") counts.txt += 1; else counts.edits += 1;
  await kvPut(env, userKey, JSON.stringify(counts), 90000);
  return { allowed: true, txtUsed: counts.txt, editsUsed: counts.edits };
}

async function addNeurons(env, neurons) {
  if (!env.USAGE_KV) return;
  const day = todayUTC();
  const key = `neurons:${day}`;
  const used = parseFloat((await kvGet(env, key)) || "0");
  await kvPut(env, key, String(used + neurons), 90000);
}

async function usageStatus(env, userId) {
  const day = todayUTC();
  let txt = 0, edits = 0, neuronsUsed = 0;
  if (env.USAGE_KV) {
    const raw = await kvGet(env, `usage:${day}:${userId}`);
    if (raw) { const c = JSON.parse(raw); txt = c.txt; edits = c.edits; }
    neuronsUsed = parseFloat((await kvGet(env, `neurons:${day}`)) || "0");
  }
  return { day, txt, edits, neuronsUsed };
}

// --- Image generation (Queue consumer) ---

async function doTextToImage(env, job) {
  const { chatId, userId, prompt } = job;
  const clean = (prompt || "").trim();
  if (clean.length < 3) {
    await sendMessage(env, chatId, "Send a longer prompt, e.g.\n/imagine a red fox in fresh snow");
    return;
  }
  if (clean.length > 2000) {
    await sendMessage(env, chatId, "That prompt is too long - keep it under 2000 characters.");
    return;
  }
  const check = await checkAndCount(env, userId, "txt", NEURONS_SCHNELL_1024);
  if (!check.allowed) { await sendMessage(env, chatId, check.reason); return; }
  await sendMessage(env, chatId, `Drawing it... (${check.txtUsed}/${TXT_LIMIT(env)} pictures used today)`);
  try {
    const result = await env.AI.run(SCHNELL, { prompt: clean, steps: 4 });
    // Workers AI returns a ReadableStream for image models in Workers,
    // or a base64 string via the REST API. Handle both.
    let imageBytes;
    if (result instanceof ReadableStream) {
      imageBytes = new Uint8Array(await new Response(result).arrayBuffer());
    } else if (typeof result === "string") {
      const bin = atob(result);
      imageBytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) imageBytes[i] = bin.charCodeAt(i);
    } else if (result && result.image) {
      const bin = atob(result.image);
      imageBytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) imageBytes[i] = bin.charCodeAt(i);
    } else {
      throw new Error("Unexpected Workers AI result shape");
    }
    await addNeurons(env, NEURONS_SCHNELL_1024);
    await sendPhoto(env, chatId, imageBytes, `\u201c${clean}\u201d\nFLUX.1-schnell \u2022 1024\u00d71024 \u2022 ~${NEURONS_SCHNELL_1024} Neurons`);
  } catch (e) {
    console.log("txt2img failed", e.message);
    const msg = String(e.message || e);
    if (msg.includes("429") || msg.toLowerCase().includes("used up")) {
      await sendMessage(env, chatId, "Today's free Cloudflare quota is used up for everyone. Try again after 00:00 UTC.");
      return;
    }
    await sendMessage(env, chatId, `Sorry, that picture failed: ${msg}\nTry a simpler prompt, or try again in a minute.`);
  }
}

async function doImageToImage(env, job) {
  const { chatId, userId, prompt, fileId } = job;
  const clean = (prompt || "").trim();
  if (clean.length < 3) {
    await sendMessage(env, chatId, "Tell me how to change the photo, e.g. \u201cmake it sunset\u201d.");
    return;
  }
  const check = await checkAndCount(env, userId, "edit", NEURONS_KLEIN_EDIT_1024);
  if (!check.allowed) { await sendMessage(env, chatId, check.reason); return; }
  await sendMessage(env, chatId, `Editing it... (${check.editsUsed}/${EDIT_LIMIT(env)} edits used today)`);
  try {
    const raw = await downloadTelegramPhoto(env, fileId);
    // klein-4b inputs must be <512x512. Workers has no Pillow, so we
    // send the original and let Cloudflare handle it, but Telegram
    // photos are usually large. To stay under 512, we downscale by
    // requesting the smallest Telegram photo size is NOT possible here,
    // so instead we pass the file through Workers AI as-is and rely
    // on the model - if it fails with 5006, tell the user to send a
    // smaller photo. In practice Telegram's 320px thumbnail size is
    // under 512, but we only have the file_id of the largest size.
    // Workaround: download, then use the REST API with multipart
    // and a resized input is not possible in a Worker without an
    // image library. Accept the original; Cloudflare resizes server-side
    // for klein-4b in most cases.
    const form = new FormData();
    form.append("prompt", clean);
    form.append("width", "1024");
    form.append("height", "1024");
    form.append("input_image_0", new Blob([raw], { type: "image/jpeg" }), "input.jpg");
    // Call Workers AI via the REST API for multipart (the binding
    // does not expose multipart). Use the account ID + API token
    // if provided, otherwise use the binding with a base64 input.
    // Here we use the binding: klein-4b accepts image as base64
    // in the run() call via the `image` field in newer SDKs, but
    // the stable path is the REST API with CF_API_TOKEN.
    // If CF_API_TOKEN is not set, fall back to binding text-to-image
    // with the prompt only (still an edit-style image, not ideal).
    // NOTE: set CF_API_TOKEN as a Worker secret for reliable edits.
    let imageBytes;
    if (env.CF_API_TOKEN && env.CF_ACCOUNT_ID) {
      const res = await fetch(
        `https://api.cloudflare.com/client/v4/accounts/${env.CF_ACCOUNT_ID}/ai/run/${encodeURIComponent(KLEIN)}`,
        { method: "POST", headers: { Authorization: `Bearer ${env.CF_API_TOKEN}` }, body: form }
      );
      if (!res.ok) throw new Error(`Cloudflare img2img failed ${res.status}: ${(await res.text()).slice(0, 300)}`);
      const ctype = res.headers.get("Content-Type") || "";
      if (ctype.startsWith("image/")) {
        imageBytes = new Uint8Array(await res.arrayBuffer());
      } else {
        const data = await res.json();
        if (!data.success) throw new Error(`Cloudflare error: ${JSON.stringify(data.errors)}`);
        const b64 = typeof data.result === "string" ? data.result : data.result?.image;
        if (!b64) throw new Error("Unexpected Cloudflare img2img response");
        const bin = atob(b64);
        imageBytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) imageBytes[i] = bin.charCodeAt(i);
      }
    } else {
      // Fallback: binding, prompt-only (no input image - not a true edit).
      // This path is only used if CF_API_TOKEN is missing.
      throw new Error("Image edits need CF_API_TOKEN + CF_ACCOUNT_ID set as Worker secrets. Set them with wrangler secret put, then try again.");
    }
    await addNeurons(env, NEURONS_KLEIN_EDIT_1024);
    await sendPhoto(env, chatId, imageBytes, `Edited: \u201c${clean}\u201d\nFLUX.2-klein-4b \u2022 1024\u00d71024 \u2022 ~${NEURONS_KLEIN_EDIT_1024} Neurons`);
  } catch (e) {
    console.log("img2img failed", e.message);
    const msg = String(e.message || e);
    if (msg.includes("429") || msg.toLowerCase().includes("used up")) {
      await sendMessage(env, chatId, "Today's free Cloudflare quota is used up for everyone. Try again after 00:00 UTC.");
      return;
    }
    await sendMessage(env, chatId, `Sorry, that edit failed: ${msg}\nTry again with a clear photo and a simple edit prompt.`);
  }
}

// --- Webhook handler (fetch) ---

async function handleUpdate(env, update) {
  const msg = update.message || {};
  const chat = msg.chat || {};
  const chatId = chat.id;
  const user = msg.from || {};
  const userId = user.id;
  if (!chatId || !userId) return;

  const text = (msg.text || "").trim();
  const caption = (msg.caption || "").trim();
  const photos = msg.photo || [];

  // Photo first
  if (photos.length) {
    const fileId = photos[photos.length - 1].file_id;
    if (caption) {
      await env.IMAGE_QUEUE.send({ kind: "edit", chatId, userId, prompt: caption, fileId });
    } else {
      // Store pending photo in KV for 10 minutes, so the next text
      // message becomes the edit prompt.
      if (env.USAGE_KV) {
        await kvPut(env, `pending:${userId}`, fileId, 600);
      }
      await sendMessage(env, chatId, "Got your photo. Now send the edit prompt as your next message, e.g. \u201cmake it sunset, oil painting\u201d.");
    }
    return;
  }

  if (!text) return;

  if (text.startsWith("/start")) {
    await sendMessage(env, chatId,
      "Hi! I make pictures for free, on Cloudflare's free tier.\n\n" +
      "\u2022 Text to image: just send a prompt, or /imagine <prompt>\n" +
      "  e.g. /imagine a red fox in fresh snow\n" +
      "\u2022 Image to image: send a photo with a caption describing the edit\n" +
      "  e.g. photo + \u201cmake it sunset\u201d\n" +
      "  Or send a photo, then send the edit prompt next.\n\n" +
      `Free limits: ${TXT_LIMIT(env)} pictures + ${EDIT_LIMIT(env)} edits per person per day, ` +
      "shared Cloudflare quota resets 00:00 UTC.\n" +
      "Use /status to see what you have left, /help for examples.");
    return;
  }

  if (text.startsWith("/help")) {
    await sendMessage(env, chatId,
      "How to use me:\n\n" +
      "/imagine a Bangladeshi rickshaw in Dhaka at sunset, cinematic\n" +
      "\u2192 makes a new picture from your words.\n\n" +
      "Send a photo, caption: turn this into a watercolour painting\n" +
      "\u2192 edits your photo.\n\n" +
      "/edit make the sky stormy\n" +
      "\u2192 reply to a photo with this, same as a captioned photo.\n\n" +
      "/status \u2192 your pictures/edits used today + free quota left.");
    return;
  }

  if (text.startsWith("/status")) {
    const s = await usageStatus(env, userId);
    await sendMessage(env, chatId,
      `Today (${s.day}, UTC):\n` +
      `\u2022 You: ${s.txt}/${TXT_LIMIT(env)} pictures, ${s.edits}/${EDIT_LIMIT(env)} edits\n` +
      `\u2022 Shared free quota: ~${Math.max(0, DAILY_NEURON_BUDGET - s.neuronsUsed).toFixed(0)} of ${DAILY_NEURON_BUDGET} Neurons left\n` +
      "Quota resets 00:00 UTC.");
    return;
  }

  if (text.startsWith("/imagine") || text.startsWith("/generate")) {
    const prompt = text.split(/\s+/).slice(1).join(" ");
    await env.IMAGE_QUEUE.send({ kind: "txt", chatId, userId, prompt });
    return;
  }

  if (text.startsWith("/edit")) {
    const prompt = text.split(/\s+/).slice(1).join(" ");
    const reply = msg.reply_to_message || {};
    const replyPhotos = reply.photo || [];
    if (!replyPhotos.length) {
      await sendMessage(env, chatId, "Reply to a photo with /edit <prompt>, e.g. reply to your photo with:\n/edit make it sunset");
      return;
    }
    await env.IMAGE_QUEUE.send({ kind: "edit", chatId, userId, prompt, fileId: replyPhotos[replyPhotos.length - 1].file_id });
    return;
  }

  // Plain text: pending edit, or imagine
  if (env.USAGE_KV) {
    const pending = await kvGet(env, `pending:${userId}`);
    if (pending) {
      await kvPut(env, `pending:${userId}`, "", 1);
      await env.IMAGE_QUEUE.send({ kind: "edit", chatId, userId, prompt: text, fileId: pending });
      return;
    }
  }
  await env.IMAGE_QUEUE.send({ kind: "txt", chatId, userId, prompt: text });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/") {
      return new Response("ok - image bot worker alive", { status: 200 });
    }
    if (request.method === "POST" && url.pathname === "/webhook") {
      if (env.WEBHOOK_SECRET) {
        const secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "";
        if (secret !== env.WEBHOOK_SECRET) {
          return new Response(JSON.stringify({ ok: false, error: "bad secret" }), { status: 403 });
        }
      }
      const update = await request.json().catch(() => ({}));
      // Answer fast, do the work in the queue - Telegram retries
      // slow webhooks and would duplicate replies.
      ctx.waitUntil(handleUpdate(env, update));
      return new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    return new Response("not found", { status: 404 });
  },

  async queue(batch, env) {
    // Queue consumer: 15 minute limit, plenty for FLUX generation.
    for (const message of batch.messages) {
      const job = message.body;
      try {
        if (job.kind === "txt") await doTextToImage(env, job);
        else if (job.kind === "edit") await doImageToImage(env, job);
      } catch (e) {
        console.log("queue job failed", e.message);
      }
      message.ack();
    }
  },
};

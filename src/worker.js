/**
 * Worker de doc2md.
 *
 * - Sirve el frontend estático (carpeta public/) mediante el binding ASSETS.
 * - /api/* hace de puerta de entrada al backend (Google Cloud Run):
 *   verifica Turnstile, aplica los límites de la demo pública y añade la
 *   API_KEY que el backend exige, para que nadie lo use saltándose esto.
 * - Cupos diarios (KV "LIMITS"): DAILY_LIMIT conversiones y DAILY_OCR_LIMIT
 *   conversiones con OCR al día para el público. Mantienen el uso de Cloud
 *   Run dentro de su capa gratuita.
 * - Con la cabecera X-Owner-Key = OWNER_KEY (secreto) no hay Turnstile ni
 *   límites: es el acceso del propietario.
 *
 * Variables (wrangler.jsonc): BACKEND_URL, TURNSTILE_SITE_KEY, MAX_FILE_MB,
 * MAX_OCR_PAGES, DAILY_LIMIT, DAILY_OCR_LIMIT. Secretos (wrangler secret
 * put): API_KEY, TURNSTILE_SECRET, OWNER_KEY.
 */

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });

const error = (detail, status) => json({ detail }, status);

async function sha256(text) {
  return crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
}

async function isOwner(request, env) {
  const given = request.headers.get("X-Owner-Key");
  if (!given || !env.OWNER_KEY) return false;
  // Se comparan los hashes para que ambos lados midan lo mismo.
  const [a, b] = await Promise.all([sha256(given), sha256(env.OWNER_KEY)]);
  return crypto.subtle.timingSafeEqual(a, b);
}

async function verifyTurnstile(token, request, env) {
  if (!token) return false;
  const body = new FormData();
  body.append("secret", env.TURNSTILE_SECRET);
  body.append("response", token);
  const ip = request.headers.get("CF-Connecting-IP");
  if (ip) body.append("remoteip", ip);
  const res = await fetch(
    "https://challenges.cloudflare.com/turnstile/v0/siteverify",
    { method: "POST", body },
  );
  const outcome = await res.json();
  return outcome.success === true;
}

// Cupos diarios en KV. KV es eventualmente consistente, así que con mucho
// tráfico simultáneo el cupo podría excederse por poco; aquí basta.
const quotaKey = (kind) => `${kind}:${new Date().toISOString().slice(0, 10)}`;

async function quotaUsed(env, kind) {
  return Number(await env.LIMITS.get(quotaKey(kind))) || 0;
}

async function quotaAdd(env, kind) {
  const used = await quotaUsed(env, kind);
  // Expira a los 2 días: los contadores viejos se borran solos.
  await env.LIMITS.put(quotaKey(kind), String(used + 1), { expirationTtl: 172800 });
}

async function handleConfig(request, env) {
  return json({
    turnstileSiteKey: env.TURNSTILE_SITE_KEY,
    maxFileMb: Number(env.MAX_FILE_MB),
    maxOcrPages: Number(env.MAX_OCR_PAGES),
    dailyOcrLimit: Number(env.DAILY_OCR_LIMIT),
    owner: await isOwner(request, env),
  });
}

async function handleHealth(env) {
  try {
    const res = await fetch(`${env.BACKEND_URL}/health`, {
      signal: AbortSignal.timeout(20000),
    });
    if (!res.ok) return json({ ready: false });
    const data = await res.json();
    return json({ ready: data.status === "ok", ocr: data.ocr === true });
  } catch {
    // Servicio apagado o arrancando: la propia petición lo despierta.
    return json({ ready: false });
  }
}

async function handleConvert(request, env) {
  const owner = await isOwner(request, env);
  const maxBytes = Number(env.MAX_FILE_MB) * 1024 * 1024;
  const length = Number(request.headers.get("content-length") || 0);
  // Margen de 1 MB para el resto del formulario multipart.
  if (!owner && length > maxBytes + 1024 * 1024) {
    return error(`El archivo supera el límite de ${env.MAX_FILE_MB} MB.`, 413);
  }

  let form;
  try {
    form = await request.formData();
  } catch {
    return error("Formulario inválido.", 400);
  }
  const file = form.get("file");
  if (!(file instanceof File)) return error("Falta el archivo.", 400);
  if (!owner && file.size > maxBytes) {
    return error(`El archivo supera el límite de ${env.MAX_FILE_MB} MB.`, 413);
  }

  if (!owner) {
    const ok = await verifyTurnstile(form.get("cf-turnstile-response"), request, env);
    if (!ok) return error("No se pudo verificar que eres humano. Recarga la página.", 403);
  }

  const wantsOcr = form.get("ocr") === "true" && file.name.toLowerCase().endsWith(".pdf");
  if (!owner) {
    if ((await quotaUsed(env, "convert")) >= Number(env.DAILY_LIMIT)) {
      return error("Se alcanzó el límite de conversiones de hoy. Vuelve mañana.", 429);
    }
    if (wantsOcr && (await quotaUsed(env, "ocr")) >= Number(env.DAILY_OCR_LIMIT)) {
      return error(
        "Se alcanzó el límite de conversiones con OCR de hoy. Desmarca el OCR o vuelve mañana.",
        429,
      );
    }
    await quotaAdd(env, "convert");
  }

  const upstream = new FormData();
  upstream.append("file", file, file.name);
  upstream.append("ocr", wantsOcr ? "true" : "false");
  upstream.append("merge_tables", form.get("merge_tables") === "true" ? "true" : "false");

  let res;
  try {
    res = await fetch(`${env.BACKEND_URL}/convert`, {
      method: "POST",
      body: upstream,
      headers: { "X-API-Key": env.API_KEY, "X-Unlimited": owner ? "1" : "0" },
    });
  } catch {
    return error("No se pudo contactar con el servidor de conversión.", 502);
  }

  const type = res.headers.get("content-type") || "";
  if (!type.includes("application/json")) {
    return error("El servidor no está disponible. Intenta de nuevo en un minuto.", 503);
  }
  if (owner || !wantsOcr || !res.ok) {
    return new Response(res.body, {
      status: res.status,
      headers: { "content-type": "application/json; charset=utf-8" },
    });
  }
  // Solo se descuenta del cupo de OCR si de verdad se reconoció alguna página.
  const data = await res.json();
  if (data.ocr_pages > 0) await quotaAdd(env, "ocr");
  return json(data);
}

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (!pathname.startsWith("/api/")) return env.ASSETS.fetch(request);

    if (pathname === "/api/config" && request.method === "GET") {
      return handleConfig(request, env);
    }
    if (pathname === "/api/health" && request.method === "GET") {
      return handleHealth(env);
    }
    if (pathname === "/api/convert" && request.method === "POST") {
      return handleConvert(request, env);
    }
    return error("Ruta no encontrada.", 404);
  },
};

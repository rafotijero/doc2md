// Frontend de doc2md: sube el archivo al Worker (/api/convert) y muestra el Markdown.

const $ = (id) => document.getElementById(id);
const OWNER_STORAGE = "doc2md-owner-key";
const THEME_STORAGE = "doc2md-theme";
const HEALTH_INTERVAL_MS = 5000;
const HEALTH_MAX_TRIES = 48; // ~4 minutos

const state = {
  config: null,
  file: null,
  turnstileId: null,
  turnstileToken: "",
  serverReady: false,
  busy: false,
  result: null,
};

// ---------------------------------------------------------------- utilidades

function storageGet(key) {
  try { return localStorage.getItem(key) || ""; } catch { return ""; }
}
function storageSet(key, value) {
  try { value ? localStorage.setItem(key, value) : localStorage.removeItem(key); } catch {}
}
function ownerHeaders() {
  const key = storageGet(OWNER_STORAGE);
  return key ? { "X-Owner-Key": key } : {};
}
function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
function fileKind(file) {
  const name = file.name.toLowerCase();
  if (name.endsWith(".pdf")) return "pdf";
  if (name.endsWith(".docx")) return "docx";
  return null;
}
function showMessage(text) {
  $("message").textContent = text;
  $("message").hidden = !text;
}

// ---------------------------------------------------------------------- tema

$("theme-toggle").addEventListener("click", () => {
  const root = document.documentElement;
  const isDark = root.dataset.theme
    ? root.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = isDark ? "light" : "dark";
  storageSet(THEME_STORAGE, root.dataset.theme);
});

// ------------------------------------------------------- estado del servidor

function setStatus(stateName, text) {
  $("status").dataset.state = stateName;
  $("status-text").textContent = text;
}

async function waitForServer() {
  for (let i = 0; i < HEALTH_MAX_TRIES; i++) {
    try {
      const res = await fetch("/api/health", { cache: "no-store" });
      const data = await res.json();
      if (data.ready) {
        state.serverReady = true;
        setStatus("ready", "Servidor listo");
        updateButton();
        return;
      }
    } catch {}
    setStatus("waking", "Despertando servidor…");
    await new Promise((r) => setTimeout(r, HEALTH_INTERVAL_MS));
  }
  setStatus("down", "Servidor no disponible");
}

// ------------------------------------------------------------------ Turnstile

function loadTurnstile() {
  if (!state.config?.turnstileSiteKey || state.config.owner) return;
  window.onTurnstileLoad = () => {
    state.turnstileId = window.turnstile.render("#turnstile", {
      sitekey: state.config.turnstileSiteKey,
      theme: "auto",
      language: "es",
      callback: (token) => { state.turnstileToken = token; updateButton(); },
      "expired-callback": () => { state.turnstileToken = ""; updateButton(); },
      "error-callback": () => { state.turnstileToken = ""; updateButton(); },
    });
  };
  const script = document.createElement("script");
  script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=onTurnstileLoad";
  script.async = true;
  document.head.appendChild(script);
}

function resetTurnstile() {
  if (state.turnstileId !== null && window.turnstile) {
    state.turnstileToken = "";
    window.turnstile.reset(state.turnstileId);
  }
}

// -------------------------------------------------------------- configuración

async function loadConfig() {
  try {
    const res = await fetch("/api/config", { headers: ownerHeaders(), cache: "no-store" });
    state.config = await res.json();
  } catch {
    state.config = { maxFileMb: 10, maxOcrPages: 3, owner: false };
  }
  const { maxFileMb, maxOcrPages, owner } = state.config;
  if (owner) {
    $("limits-hint").textContent = "PDF o DOCX · sin límites (propietario)";
    $("ocr-hint").textContent = "Más lento: puede tardar un minuto por página.";
    $("owner-status").textContent = "Clave válida: sin límites ni verificación.";
  } else {
    $("limits-hint").textContent = `PDF o DOCX · hasta ${maxFileMb} MB`;
    $("ocr-hint").textContent = `Más lento. En la demo se reconocen hasta ${maxOcrPages} páginas escaneadas.`;
    $("owner-status").textContent = storageGet(OWNER_STORAGE) ? "La clave guardada no es válida." : "";
  }
  loadTurnstile();
  updateButton();
}

// ------------------------------------------------------------------- archivo

function setFile(file) {
  showMessage("");
  if (!file) {
    state.file = null;
    $("file-input").value = "";
    $("file-chip").hidden = true;
    updateOptions();
    updateButton();
    return;
  }
  const kind = fileKind(file);
  if (!kind) {
    showMessage("Formato no soportado: elige un archivo .pdf o .docx.");
    return;
  }
  const max = state.config?.owner ? Infinity : (state.config?.maxFileMb ?? 10) * 1024 * 1024;
  if (file.size > max) {
    showMessage(`El archivo pesa ${formatSize(file.size)} y el límite es de ${state.config.maxFileMb} MB.`);
    return;
  }
  state.file = file;
  $("file-badge").textContent = kind.toUpperCase();
  $("file-name").textContent = file.name;
  $("file-size").textContent = formatSize(file.size);
  $("file-chip").hidden = false;
  updateOptions();
  updateButton();
}

function updateOptions() {
  const isDocx = state.file && fileKind(state.file) === "docx";
  for (const id of ["opt-ocr", "opt-merge"]) $(id).disabled = isDocx || state.busy;
}

function updateButton() {
  const needsCaptcha = !state.config?.owner && state.config?.turnstileSiteKey;
  $("convert-btn").disabled =
    !state.file || state.busy || !state.serverReady || (needsCaptcha && !state.turnstileToken);
  $("convert-btn").title = !state.serverReady
    ? "Esperando a que el servidor esté listo"
    : needsCaptcha && !state.turnstileToken ? "Completa la verificación" : "";
}

$("file-input").addEventListener("change", (e) => setFile(e.target.files[0]));
$("file-clear").addEventListener("click", () => setFile(null));

const dropzone = $("dropzone");
for (const type of ["dragenter", "dragover"]) {
  dropzone.addEventListener(type, (e) => { e.preventDefault(); dropzone.classList.add("dragover"); });
}
for (const type of ["dragleave", "drop"]) {
  dropzone.addEventListener(type, (e) => { e.preventDefault(); dropzone.classList.remove("dragover"); });
}
dropzone.addEventListener("drop", (e) => setFile(e.dataTransfer.files[0]));

// ---------------------------------------------------------------- conversión

function setBusy(busy) {
  state.busy = busy;
  $("progress").hidden = !busy;
  updateOptions();
  updateButton();
}

async function convert() {
  if (!state.file) return;
  showMessage("");
  setBusy(true);
  const started = Date.now();
  const tick = setInterval(() => {
    const s = Math.round((Date.now() - started) / 1000);
    $("progress-text").textContent = `Convirtiendo… ${s} s`;
  }, 1000);
  $("progress-text").textContent = "Convirtiendo…";

  const form = new FormData();
  form.append("file", state.file);
  form.append("ocr", String($("opt-ocr").checked));
  form.append("merge_tables", String($("opt-merge").checked));
  if (state.turnstileToken) form.append("cf-turnstile-response", state.turnstileToken);

  try {
    const res = await fetch("/api/convert", { method: "POST", body: form, headers: ownerHeaders() });
    let data;
    try { data = await res.json(); } catch { data = {}; }
    if (!res.ok) {
      const detail = typeof data.detail === "string" ? data.detail : "";
      throw new Error(detail || `Error ${res.status} al convertir.`);
    }
    showResult(data);
  } catch (err) {
    showMessage(err.message || "No se pudo convertir el archivo.");
  } finally {
    clearInterval(tick);
    setBusy(false);
    resetTurnstile();
  }
}

$("convert-btn").addEventListener("click", convert);

// ----------------------------------------------------------------- resultado

const PDF_TYPES = { text_based: "texto embebido", scanned: "escaneado", mixed: "mixto" };

function showResult(data) {
  state.result = data;
  $("result-title").textContent = data.filename;

  const meta = [];
  if (data.kind === "pdf") {
    meta.push(`PDF ${PDF_TYPES[data.pdf_type] || data.pdf_type}`);
    meta.push(`${data.page_count} página${data.page_count === 1 ? "" : "s"}`);
    if (data.ocr_pages) meta.push(`OCR en ${data.ocr_pages}`);
  } else {
    meta.push("DOCX");
  }
  meta.push(`${(data.markdown.length / 1024).toFixed(1)} KB de Markdown`);
  meta.push(`${(data.elapsed_ms / 1000).toFixed(1)} s`);
  $("result-meta").replaceChildren(...meta.map((text) => {
    const li = document.createElement("li");
    li.textContent = text;
    return li;
  }));

  const notices = [];
  if (data.ocr_skipped) {
    notices.push(`Se omitieron ${data.ocr_skipped} página(s) escaneada(s) por el límite de OCR de la demo.`);
  }
  if (data.kind === "pdf" && data.pdf_type !== "text_based" && !data.ocr_pages && !$("opt-ocr").checked) {
    notices.push("Este PDF tiene páginas escaneadas que salieron vacías: vuelve a convertirlo con OCR.");
  }
  $("result-notice").textContent = notices.join(" ");
  $("result-notice").hidden = notices.length === 0;

  $("raw-code").textContent = data.markdown;
  $("panel-preview").innerHTML = window.DOMPurify.sanitize(window.marked.parse(data.markdown));
  selectTab("preview");
  $("result").hidden = false;
  $("result").scrollIntoView({ behavior: "smooth", block: "start" });
}

function selectTab(name) {
  for (const tab of ["preview", "raw"]) {
    const active = tab === name;
    $(`tab-${tab}`).setAttribute("aria-selected", String(active));
    $(`panel-${tab}`).hidden = !active;
  }
}
$("tab-preview").addEventListener("click", () => selectTab("preview"));
$("tab-raw").addEventListener("click", () => selectTab("raw"));

$("copy-btn").addEventListener("click", async () => {
  if (!state.result) return;
  try {
    await navigator.clipboard.writeText(state.result.markdown);
    $("copy-btn").textContent = "¡Copiado!";
  } catch {
    $("copy-btn").textContent = "No se pudo copiar";
  }
  setTimeout(() => { $("copy-btn").textContent = "Copiar"; }, 1500);
});

$("download-btn").addEventListener("click", () => {
  if (!state.result) return;
  const blob = new Blob([state.result.markdown], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement("a"), { href: url, download: state.result.filename });
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
});

// --------------------------------------------------------------- propietario

$("owner-form").addEventListener("submit", (e) => {
  e.preventDefault();
  storageSet(OWNER_STORAGE, $("owner-key").value.trim());
  location.reload();
});
$("owner-clear").addEventListener("click", () => {
  storageSet(OWNER_STORAGE, "");
  location.reload();
});

// ------------------------------------------------------------------- inicio

loadConfig();
waitForServer();

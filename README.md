# doc2md

Convierte **PDF** y **DOCX** a **Markdown** desde el navegador:

- Texto embebido de PDF con [`pdf-inspector`](https://github.com/firecrawl/pdf-inspector).
- **OCR** de páginas escaneadas con Tesseract y OpenCV. Si la página es una tabla con cuadrícula, se detecta y cada celda se reconoce por separado.
- **Unión de tablas** partidas entre páginas: quita los pies "Página N de M" y las cabeceras repetidas.
- DOCX con `python-docx`: títulos, listas, negritas y tablas.

Demo: <https://doc2md.rafotijero.dev>

## Arquitectura

```
Navegador ──► Cloudflare Worker (doc2md.rafotijero.dev) ──► Google Cloud Run (Docker)
              · sirve public/ (HTML/CSS/JS)                 · FastAPI + pdf-inspector
              · verifica Turnstile                          · Tesseract + OpenCV (OCR)
              · aplica límites y cupos diarios (KV)         · python-docx
              · añade la API_KEY
```

El backend necesita binarios nativos: Tesseract, pdfium y el módulo Rust de `pdf-inspector`. Esos binarios no pueden ejecutarse en un Worker, así que el backend corre en Google Cloud Run, que se apaga cuando nadie lo usa y cobra solo por el tiempo de conversión. El Worker es la única puerta de entrada: el backend rechaza cualquier petición que no traiga la `API_KEY`.

| Ruta | Qué hay |
|---|---|
| `public/` | Frontend estático (sin framework). |
| `src/worker.js` | Worker: `/api/config`, `/api/health` y `/api/convert`. |
| `backend/app.py` | API FastAPI (`/health` y `/convert`). |
| `backend/doc2md/` | Conversores: `pdf.py`, `word.py`, `ocr.py` y la CLI por lotes `cli.py`. |
| `backend/Dockerfile` | Imagen para Cloud Run. |

### Límites de la demo pública

- Archivos de hasta 10 MB.
- OCR de hasta 3 páginas escaneadas por documento.
- Hasta 30 conversiones al día, de las que como máximo 10 pueden usar OCR.
- Verificación con Cloudflare Turnstile.

Los cupos diarios mantienen el uso de Cloud Run por debajo de su capa gratuita mensual (180 000 vCPU-segundos): una conversión con OCR de 3 páginas consume unos 480.

Con la **clave de propietario** (`OWNER_KEY`) no hay límites ni verificación. Se ingresa en "Acceso del propietario", al pie de la página, y se guarda en el navegador. Ojo: tus conversiones también consumen la capa gratuita.

## Desarrollo local

Requisitos: Node 20+, Python 3.11+ y [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki) (solo para el OCR).

```bash
# Backend
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt     # en Linux/macOS: .venv/bin/pip
API_KEY=devkey .venv/Scripts/python -m uvicorn app:app --port 8080
```

Si tu instalación de Tesseract no trae el español, define `TESSDATA_DIR` con una carpeta que contenga `spa.traineddata` y `osd.traineddata`. Se descargan de [tessdata](https://github.com/tesseract-ocr/tessdata).

```bash
# Frontend + Worker (en otra terminal, desde la raíz)
cp .dev.vars.example .dev.vars
npm install
npm run dev        # http://localhost:8787
```

### Conversión por lotes (CLI)

Para carpetas grandes conviene la CLI local, sin pasar por la web:

```bash
cd backend
.venv/Scripts/python -m doc2md.cli pdf  ruta/a/pdfs  --ocr --merge-tables -w 4
.venv/Scripts/python -m doc2md.cli docx ruta/a/docx
```

Genera un `.md` por archivo en `<carpeta>_md` y puede retomarse si se interrumpe. Los errores se guardan en `_fallidos.log`.

## Despliegue

### 1. Backend en Google Cloud Run

1. En <https://console.cloud.google.com> crea un proyecto `doc2md` y vincúlale una cuenta de facturación.
2. Abre *Cloud Run → Deploy container → Service* y elige **Continuously deploy from a repository**. Conecta GitHub con *Cloud Build*, elige `rafotijero/doc2md`, la rama `^main$`, el tipo **Dockerfile** y la ruta `/backend/Dockerfile`.
3. Configura el servicio:
   - **Service name:** `doc2md`. **Region:** `us-central1` (la más barata).
   - **Authentication:** *Allow public access*. El backend se protege con la `API_KEY`.
   - **Billing:** *Request-based*. **Minimum instances:** 0. **Maximum instances:** 1.
   - En *Container(s)*: puerto **8080**, **2 GiB** de memoria, **2 vCPU** y *request timeout* de **900** segundos. En *Requests*, *maximum concurrent requests* en **4**.
   - En *Variables & secrets*, agrega la variable `API_KEY` con una clave larga y aleatoria (`openssl rand -base64 32`).
4. Pulsa **Create**. El primer build tarda unos 5 minutos. Después, cada push a `main` vuelve a desplegar el backend.
5. Abre la URL del servicio (`https://doc2md-….run.app`) con `/health` al final. Debe responder `{"status":"ok","ocr":true}`.
6. Recomendado: en *Billing → Budgets & alerts* crea un presupuesto de 1 USD con alertas por correo. Las alertas solo avisan, no detienen el servicio; el techo real lo ponen los cupos diarios del Worker.

### 2. Turnstile

En el panel de Cloudflare, ve a *Turnstile → Add widget*, agrega el hostname `doc2md.rafotijero.dev` y elige el modo *Managed*. Copia la **site key** en `TURNSTILE_SITE_KEY` dentro de `wrangler.jsonc`.

### 3. Worker en Cloudflare

```bash
npx wrangler kv namespace create LIMITS   # pega el id en kv_namespaces de wrangler.jsonc
npx wrangler secret put API_KEY           # la misma clave de Cloud Run
npx wrangler secret put TURNSTILE_SECRET  # secret key de Turnstile
npx wrangler secret put OWNER_KEY         # tu clave de propietario
npm run deploy
```

Antes de desplegar, pon la URL de Cloud Run en `BACKEND_URL` dentro de `wrangler.jsonc`. También puedes conectar el repo en *Workers & Pages → doc2md → Settings → Builds* para desplegar en cada push, igual que tus otros Workers. `wrangler.jsonc` ya declara el dominio personalizado `doc2md.rafotijero.dev`.

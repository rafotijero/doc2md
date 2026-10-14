# doc2md

Convierte **PDF** y **DOCX** a **Markdown** desde el navegador:

- Texto embebido de PDF con [`pdf-inspector`](https://github.com/firecrawl/pdf-inspector).
- **OCR** de páginas escaneadas con Tesseract y OpenCV. Si la página es una tabla con cuadrícula, se detecta y cada celda se reconoce por separado.
- **Unión de tablas** partidas entre páginas: quita los pies "Página N de M" y las cabeceras repetidas.
- DOCX con `python-docx`: títulos, listas, negritas y tablas.

Demo: <https://doc2md.rafotijero.dev>

## Arquitectura

```
Navegador ──► Cloudflare Worker (doc2md.rafotijero.dev) ──► Hugging Face Space (Docker)
              · sirve public/ (HTML/CSS/JS)                 · FastAPI + pdf-inspector
              · verifica Turnstile                          · Tesseract + OpenCV (OCR)
              · aplica límites de la demo                   · python-docx
              · añade la API_KEY
```

El backend necesita binarios nativos: Tesseract, pdfium y el módulo Rust de `pdf-inspector`. Esos binarios no pueden ejecutarse en un Worker, así que el backend corre en un Space gratuito de Hugging Face. El Worker es la única puerta de entrada: el backend rechaza cualquier petición que no traiga la `API_KEY`.

| Ruta | Qué hay |
|---|---|
| `public/` | Frontend estático (sin framework). |
| `src/worker.js` | Worker: `/api/config`, `/api/health` y `/api/convert`. |
| `backend/app.py` | API FastAPI (`/health` y `/convert`). |
| `backend/doc2md/` | Conversores: `pdf.py`, `word.py`, `ocr.py` y la CLI por lotes `cli.py`. |
| `backend/Dockerfile` | Imagen para Hugging Face Spaces. |
| `.github/workflows/deploy-backend.yml` | Publica `backend/` en el Space en cada push. |

### Límites de la demo pública

- Archivos de hasta 10 MB.
- OCR de hasta 3 páginas escaneadas por documento.
- Verificación con Cloudflare Turnstile.

Con la **clave de propietario** (`OWNER_KEY`) no hay límites ni verificación. Se ingresa en "Acceso del propietario", al pie de la página, y se guarda en el navegador.

## Desarrollo local

Requisitos: Node 20+, Python 3.11+ y [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki) (solo para el OCR).

```bash
# Backend
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt     # en Linux/macOS: .venv/bin/pip
API_KEY=devkey .venv/Scripts/python -m uvicorn app:app --port 7860
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

### 1. Backend en Hugging Face Spaces (gratis)

1. En <https://huggingface.co/new-space> crea un Space con **SDK: Docker** (plantilla *Blank*), hardware **CPU basic (free)** y visibilidad **Public**. Usa el nombre `doc2md`.
2. En *Settings → Variables and secrets* del Space, agrega el **secreto** `API_KEY` con una clave larga y aleatoria.
3. Sube el contenido de `backend/` al Space. Puedes hacerlo de dos formas:
   - **Automático:** en el repo de GitHub, agrega el secreto `HF_TOKEN` (un token de *escritura* de <https://huggingface.co/settings/tokens>) y la variable `HF_SPACE=rafotijero/doc2md`. Después ejecuta el workflow *Deploy backend a Hugging Face*.
   - **Manual:** `git subtree split --prefix backend -b hf-space` y luego `git push --force https://huggingface.co/spaces/rafotijero/doc2md hf-space:main`.
4. Cuando el Space esté *Running*, `https://rafotijero-doc2md.hf.space/health` debe responder `{"status":"ok","ocr":true}`.

> El Space gratuito se duerme tras 48 h sin uso. La primera visita lo despierta en alrededor de un minuto, y mientras tanto la página muestra "Despertando servidor…".

### 2. Turnstile

En el panel de Cloudflare, ve a *Turnstile → Add widget*, agrega el hostname `doc2md.rafotijero.dev` y elige el modo *Managed*. Copia la **site key** en `TURNSTILE_SITE_KEY` dentro de `wrangler.jsonc`.

### 3. Worker en Cloudflare

```bash
npx wrangler secret put API_KEY           # la misma clave del Space
npx wrangler secret put TURNSTILE_SECRET  # secret key de Turnstile
npx wrangler secret put OWNER_KEY         # tu clave de propietario
npm run deploy
```

También puedes conectar el repo en *Workers & Pages → doc2md → Settings → Builds* para desplegar en cada push, igual que tus otros Workers. `wrangler.jsonc` ya declara el dominio personalizado `doc2md.rafotijero.dev`.

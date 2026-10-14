---
title: doc2md
emoji: 📄
colorFrom: indigo
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: API que convierte PDF y DOCX a Markdown (con OCR)
---

# doc2md · backend

API en FastAPI que convierte PDF y DOCX a Markdown. Es el backend de
[doc2md](https://github.com/rafotijero/doc2md) y está pensada para usarse
detrás de su Worker de Cloudflare, no directamente.

- `GET /health`: estado del servicio y si el OCR está disponible.
- `POST /convert` (multipart): `file`, `ocr` y `merge_tables`. Requiere la
  cabecera `X-API-Key`.

Este README lleva la cabecera YAML que Hugging Face Spaces necesita para
construir el contenedor con el `Dockerfile` de esta carpeta.

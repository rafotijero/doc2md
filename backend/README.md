# doc2md · backend

API en FastAPI que convierte PDF y DOCX a Markdown. Es el backend de
[doc2md](https://github.com/rafotijero/doc2md) y está pensada para usarse
detrás de su Worker de Cloudflare, no directamente. Se despliega en Google
Cloud Run con el `Dockerfile` de esta carpeta.

- `GET /health`: estado del servicio y si el OCR está disponible.
- `POST /convert` (multipart): `file`, `ocr` y `merge_tables`. Requiere la
  cabecera `X-API-Key`.

Variables de entorno: ver la cabecera de `app.py`.

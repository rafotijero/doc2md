"""API de doc2md: convierte un PDF o DOCX subido a Markdown.

Variables de entorno:
    API_KEY          Secreto compartido con el Worker de Cloudflare. Si está
                     definido, toda petición a /convert debe traer la cabecera
                     X-API-Key con ese valor (así nadie se salta el Worker).
    MAX_FILE_MB      Tamaño máximo del archivo en la demo pública (def. 10).
    MAX_OCR_PAGES    Páginas escaneadas que se reconocen con OCR en la demo
                     pública (def. 3); el resto se omite.
    MAX_CONCURRENT   Conversiones simultáneas (def. 1); el OCR consume mucha
                     CPU y RAM, y una a la vez acota también el costo.

El Worker añade "X-Unlimited: 1" cuando la petición viene del propietario;
solo se respeta si además llega la API_KEY correcta.
"""

import hmac
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile

from doc2md.pdf import pdf_to_markdown
from doc2md.word import docx_to_markdown

API_KEY = os.environ.get("API_KEY", "")
MAX_FILE_MB = float(os.environ.get("MAX_FILE_MB", "10"))
MAX_OCR_PAGES = int(os.environ.get("MAX_OCR_PAGES", "3"))
_slots = threading.BoundedSemaphore(int(os.environ.get("MAX_CONCURRENT", "1")))
_QUEUE_TIMEOUT = 120  # segundos esperando turno antes de responder 503

app = FastAPI(title="doc2md", docs_url=None, redoc_url=None)


def _ocr_available() -> bool:
    try:
        from doc2md import ocr  # noqa: F401  (falla si no hay Tesseract)
    except Exception:
        return False
    return True


@app.get("/")
def root():
    return {"service": "doc2md", "endpoints": ["/health", "/convert"]}


@app.get("/health")
def health():
    return {"status": "ok", "ocr": _ocr_available()}


def _detect_kind(filename: str, head: bytes) -> str:
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK") and filename.lower().endswith(".docx"):
        return "docx"
    raise HTTPException(400, "Formato no soportado: sube un PDF o un DOCX.")


@app.post("/convert")
def convert(
    file: UploadFile = File(...),
    ocr: bool = Form(False),
    merge_tables: bool = Form(False),
    x_api_key: str = Header(""),
    x_unlimited: str = Header(""),
):
    if API_KEY and not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(401, "No autorizado.")
    unlimited = x_unlimited == "1"
    max_bytes = None if unlimited else int(MAX_FILE_MB * 1024 * 1024)

    filename = Path(file.filename or "documento").name
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "input"
        with src.open("wb") as out:
            size = 0
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    raise HTTPException(
                        413, f"El archivo supera el límite de {MAX_FILE_MB:g} MB."
                    )
                out.write(chunk)
        if size == 0:
            raise HTTPException(400, "El archivo está vacío.")
        with src.open("rb") as f:
            kind = _detect_kind(filename, f.read(8))
        shutil.move(src, src.with_suffix(f".{kind}"))
        src = src.with_suffix(f".{kind}")

        if not _slots.acquire(timeout=_QUEUE_TIMEOUT):
            raise HTTPException(503, "El servidor está ocupado, intenta en un momento.")
        start = time.time()
        try:
            if kind == "pdf":
                result = pdf_to_markdown(
                    src,
                    ocr=ocr,
                    merge=merge_tables,
                    max_ocr_pages=None if unlimited else MAX_OCR_PAGES,
                )
                info = {
                    "markdown": result.markdown,
                    "pdf_type": result.pdf_type,
                    "page_count": result.page_count,
                    "ocr_pages": result.ocr_pages,
                    "ocr_skipped": result.ocr_skipped,
                }
            else:
                info = {"markdown": docx_to_markdown(src)}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(422, f"No se pudo convertir el archivo: {e}") from e
        finally:
            _slots.release()

    return {
        "filename": f"{Path(filename).stem}.md",
        "kind": kind,
        "elapsed_ms": round((time.time() - start) * 1000),
        **info,
    }

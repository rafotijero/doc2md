# Backend de doc2md para Google Cloud Run.
FROM python:3.13-slim

# Tesseract con español y detección de orientación (OSD).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-spa tesseract-ocr-osd \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /home/user/app
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt
COPY --chown=user . .

# Cloud Run indica el puerto en la variable PORT (8080 por defecto).
ENV PORT=8080
EXPOSE 8080
CMD exec uvicorn app:app --host 0.0.0.0 --port "$PORT"

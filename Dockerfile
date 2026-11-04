FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
# Job-description recovery renders pages it cannot read over plain HTTP in headless Chromium. Build with
# --build-arg INSTALL_BROWSER=false for a smaller image without it: recovery then uses HTTP only
# (set PLAYWRIGHT_ENABLED=false so Monitoring says so instead of reporting Chromium as missing).
ARG INSTALL_BROWSER=true
# Resume extraction V2 reads a PDF's pages with Tesseract when their text layer is broken (local; nothing leaves the
# server). Build with --build-arg INSTALL_OCR=false to leave it out: corrections then use consistent rules only.
ARG INSTALL_OCR=true
COPY requirements.txt ./
# tini reaps orphaned child processes (Chromium spawns several) when uvicorn runs as PID 1.
RUN apt-get update && apt-get install -y --no-install-recommends tini \
 && if [ "$INSTALL_OCR" = "true" ]; then apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng; fi \
 && pip install --no-cache-dir -r requirements.txt \
 && if [ "$INSTALL_BROWSER" = "true" ]; then \
      python -m playwright install --with-deps --only-shell chromium; \
    fi \
 && rm -rf /var/lib/apt/lists/*
COPY app ./app
EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]

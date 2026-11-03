# FinSight as one container: the API serves the dashboard from the same origin.
#
#   docker build -t finsight .
#   docker run -p 7860:7860 -v finsight-data:/app/data \
#              -e FINSIGHT_ACCESS_PASSWORD=change-me finsight
#
# then open http://localhost:7860. See docs/DEPLOY.md.
#
# deploy/huggingface/Dockerfile repeats these steps from a fresh clone of the
# repository; a change here belongs there too.

# ---- 1. build the dashboard -------------------------------------------------
FROM node:22-slim AS dashboard
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- 2. the application -----------------------------------------------------
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# OpenCV and ONNX Runtime link against these; the slim image has neither.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces runs containers as uid 1000; any other host is fine with it.
RUN useradd --create-home --uid 1000 finsight
WORKDIR /app

# Dependencies first, so a code change does not reinstall them.
COPY backend/requirements.txt backend/requirements.txt
# rapidocr-onnxruntime declares the desktop OpenCV build, which installs the
# same cv2 module as the headless one and needs X11 libraries a server image
# does not have: with it, every job failed at "import cv2". Keep only the
# headless build, at the version requirements.txt pins, and prove the native
# libraries load before the image is accepted.
RUN pip install -r backend/requirements.txt \
 && pip uninstall -y opencv-python opencv-python-headless \
 && pip install --no-deps "$(grep -i '^opencv-python-headless==' backend/requirements.txt)" \
 && python -c "import cv2, onnxruntime, fitz; print('native libraries load:', cv2.__version__)"

COPY --chown=finsight:finsight backend/ backend/
COPY --chown=finsight:finsight --from=dashboard /web/dist frontend/dist
RUN mkdir -p data && chown finsight:finsight data

USER finsight
WORKDIR /app/backend

# data/ holds the database, uploads, page renders and the OCR cache. Mount a
# volume there or every upload is lost when the container is replaced.
ENV FINSIGHT_STATIC_DIR=/app/frontend/dist \
    FINSIGHT_ENVIRONMENT=prod \
    FINSIGHT_LOG_JSON=true \
    PORT=7860
VOLUME ["/app/data"]
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/v1/health' % os.environ.get('PORT','7860'), timeout=4)"

# One worker process: jobs are queued in-process, and a second process would
# not see the first one's queue.
CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]

# FinSight on a Hugging Face Space (Docker SDK).
#
# A Space is its own small git repository. This file and the README.md beside
# it are all it needs: the build clones the application from GitHub, so the
# Space never holds a copy of the code, and nothing has to be pushed to the
# Space beyond these two files. The GitHub repository must be public.
#
# The steps after the clone are the repository's own Dockerfile; a change
# there belongs here too.

# ---- 0. fetch the application ------------------------------------------------
FROM debian:bookworm-slim AS source
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
ARG REPO=tahalok2005-alt/Finsight
ARG REF=main
# Fetched on every build. When the branch moves this changes, which
# invalidates the cached clone below; without it a rebuild would reuse
# yesterday's code.
ADD https://api.github.com/repos/${REPO}/git/refs/heads/${REF} /tmp/ref.json
RUN git clone --depth 1 --branch "${REF}" "https://github.com/${REPO}.git" /src

# ---- 1. build the dashboard -------------------------------------------------
FROM node:22-slim AS dashboard
WORKDIR /web
COPY --from=source /src/frontend/package.json /src/frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY --from=source /src/frontend/ ./
RUN npm run build

# ---- 2. the application -----------------------------------------------------
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# Spaces run containers as uid 1000.
RUN useradd --create-home --uid 1000 finsight
WORKDIR /app

COPY --from=source /src/backend/requirements.txt backend/requirements.txt
# rapidocr-onnxruntime declares the desktop OpenCV build, which needs X11
# libraries this image does not have. Keep only the headless build.
RUN pip install -r backend/requirements.txt \
 && pip uninstall -y opencv-python opencv-python-headless \
 && pip install --no-deps "$(grep -i '^opencv-python-headless==' backend/requirements.txt)" \
 && python -c "import cv2, onnxruntime, fitz; print('native libraries load:', cv2.__version__)"

COPY --from=source --chown=finsight:finsight /src/backend/ backend/
COPY --from=dashboard --chown=finsight:finsight /web/dist frontend/dist
RUN mkdir -p data && chown finsight:finsight data

USER finsight
WORKDIR /app/backend

# Public mode: every browser gets a private workspace, and documents are
# deleted 24 hours after upload -- which is also the honest promise on a free
# Space, whose storage does not survive a restart anyway. The limits keep one
# visitor from occupying both CPU cores. Each can be overridden under the
# Space's Settings > Variables and secrets.
ENV FINSIGHT_STATIC_DIR=/app/frontend/dist \
    FINSIGHT_ENVIRONMENT=prod \
    FINSIGHT_LOG_JSON=true \
    FINSIGHT_WORKSPACES_ENABLED=true \
    FINSIGHT_RETENTION_HOURS=24 \
    FINSIGHT_MAX_UPLOAD_MB=25 \
    FINSIGHT_MAX_PAGES=60 \
    PORT=7860
EXPOSE 7860

CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]

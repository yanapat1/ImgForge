ARG BASE_IMAGE=python:3.12-slim

# front end: compile static/ts -> static/js
FROM node:20-slim AS frontend
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json build.mjs ./
COPY static/ts static/ts
RUN npm run typecheck && npm run build

# BASE_IMAGE (set in .env) can be an earlier build of this image, e.g. image-tools:torch-base made with
#   docker tag image-tools:latest image-tools:torch-base
# torch etc. are then already installed, so the uv pip install below only adds what requirements.txt changed
FROM ${BASE_IMAGE}

# opencv needs libgl1/libglib2.0-0; curl is for the healthcheck
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 curl \
    && rm -rf /var/lib/apt/lists/*

# dataviz/paths.py sets PROJECT_ROOT = APP_DIR.parents[1], so the app must sit two levels deep:
#   /workspace (project root) / scripts / image-tools (app)
WORKDIR /workspace/scripts/image-tools

# pinned: a moving :latest tag invalidates this layer (and the pip install below) on every new uv release
# (uv 0.13.1, the image already pulled here, pinned by digest so the existing layer cache still matches)
COPY --from=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 /uv /usr/local/bin/uv

COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

COPY . .
COPY --from=frontend /build/static/js static/js

ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/workspace/.hf_cache \
    YOLO_CONFIG_DIR=/workspace/.dataviz_cache

EXPOSE 19151

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fs http://localhost:19151/ >/dev/null || exit 1

CMD ["python", "app.py", "--host", "0.0.0.0", "--port", "19151"]

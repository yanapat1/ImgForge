ARG BASE_IMAGE=python:3.12-slim

# front end: compile static/ts -> static/js
FROM node:20-slim AS frontend
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY tsconfig.json build.mjs ./
COPY static/ts static/ts
COPY static/vendor-src static/vendor-src
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

# ZONES: which parts of the product this image holds (dataviz/zones.py). Empty = every zone. An analyst image:
#   docker compose build --build-arg ZONES=bucket,data      (or ZONES=... in .env)
# installs only core + those zones' requirements (no torch / GPU libraries) and leaves the other zones' files out.
ARG ZONES=
COPY requirements.txt .
COPY requirements/ requirements/
RUN if [ -z "$ZONES" ]; then uv pip install --system --no-cache -r requirements.txt; \
    else for z in core $(echo "$ZONES" | tr ',' ' '); do echo "-r $PWD/requirements/$z.txt"; done > /tmp/zones.txt \
      && uv pip install --system --no-cache -r /tmp/zones.txt; fi

COPY . .
COPY --from=frontend /build/static/js static/js
RUN if [ -n "$ZONES" ]; then DATAVIZ_ZONES="$ZONES" python -m dataviz.zones prune; fi
ENV DATAVIZ_ZONES=${ZONES}

ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/workspace/.hf_cache \
    YOLO_CONFIG_DIR=/workspace/.dataviz_cache

EXPOSE 19151

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fs http://localhost:19151/ >/dev/null || exit 1

CMD ["python", "app.py", "--host", "0.0.0.0", "--port", "19151"]

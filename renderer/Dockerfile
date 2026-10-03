# The official image already contains the exact Chromium revision. No downloads at runtime.
FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3 AS verified
WORKDIR /source
COPY . /source/
RUN python deployment/scene/verify_runtime.py verify --component renderer

FROM mcr.microsoft.com/playwright:v1.57.0-noble@sha256:3bed4b1a12f2338642f3d8cba28e291deef3c66bd4a964bbeb3e57bbff511dbd
WORKDIR /app
ENV DEBIAN_FRONTEND=noninteractive PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 PYTHONNOUSERSITE=1
COPY --from=verified /source/ /app/
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources \
    && rm -rf /var/lib/apt/lists/* \
    && cp deployment/scene/ubuntu.sources /etc/apt/sources.list.d/scene.sources \
    && apt-get update -o APT::Update::Error-Mode=any \
    && sh deployment/scene/verify-apt.sh deployment/scene/ubuntu.inrelease-sha256 \
    && apt-get install -y --no-install-recommends python3 python3-venv libreoffice-impress fontconfig fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
# The render scripts resolve Playwright from /app/frontend; install only its minimal lock.
RUN mkdir -p /app/frontend \
    && cp renderer/package.json renderer/package-lock.json /app/frontend/ \
    && cd /app/frontend && npm ci --ignore-scripts --no-audit --no-fund
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --disable-pip-version-check --no-cache-dir --require-hashes --only-binary=:all: -r renderer/requirements.lock.txt
RUN mkdir -p /jobs /opt/scene && chmod 0711 /jobs \
    && chmod -R a+rX /app/renderer /app/backend/fonts /ms-playwright
# Root coordinates only; each converter/Chromium job runs under a fresh unprivileged UID.
USER 0:0
ENV SCENE_RENDER_JOB_ROOT=/jobs SCENE_RENDER_PYTHON=/opt/venv/bin/python SCENE_RENDER_REQUIRE_UNIQUE_UID=true
RUN /opt/venv/bin/python deployment/scene/verify_runtime.py record --component renderer --output /opt/scene/runtime-manifest.json \
    && /opt/venv/bin/python -c "from renderer.daemon import smoke_runtime; checks = smoke_runtime(); print(checks); assert all(checks.values()), 'renderer self-test failed'"
CMD ["/opt/venv/bin/python", "/app/renderer/daemon.py"]

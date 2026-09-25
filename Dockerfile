# 9Router base image + AzBot sidecar in one container.
# Fits a single lowest-spec Sevalla instance: 9router owns $PORT (health
# probe), AzBot runs alongside as a supervised background process.
#
# Base stays decolua/9router (Alpine + Node 22 + standalone build). Its
# system python is 3.14, which pyrogram 2.x cannot import on
# (asyncio.get_event_loop removal) — so AzBot brings its own Python 3.12
# via uv-managed interpreter + venv. AzBot requirements adjusted to match.
FROM decolua/9router:0.5.86

USER root

# uv: manages AzBot's Python 3.12 without touching system python.
# Install dir must be world-readable: the venv symlinks into it and the bot
# runs as the `node` user, which cannot read /root (uv's default home).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python

# AzBot runtime (Alpine names) + C toolchain for the best-effort tgcrypto
# build (no cp312 musl wheels — purged again after pip install).
RUN apk add --no-cache ffmpeg aria2 p7zip zip su-exec \
        gcc musl-dev libffi-dev \
    && uv python install 3.12 \
    && chmod -R a+rX /opt/uv-python \
    && uv venv --python 3.12 /app/azbot-venv

COPY azbot/requirements.txt /tmp/azbot-requirements.txt
RUN uv pip install --python /app/azbot-venv/bin/python --no-cache -r /tmp/azbot-requirements.txt \
    && rm /tmp/azbot-requirements.txt \
    && (uv pip install --python /app/azbot-venv/bin/python --no-cache tgcrypto \
        || echo "tgcrypto unavailable — crypto_guard falls back to pure python") \
    && apk del gcc musl-dev libffi-dev
COPY check_imports.py /tmp/check_imports.py
RUN su-exec node /app/azbot-venv/bin/python /tmp/check_imports.py && rm /tmp/check_imports.py

COPY azbot/ /app/azbot/
COPY entrypoint.sh /entrypoint-combined.sh
RUN chmod +x /entrypoint-combined.sh \
    && rm -rf /app/azbot/__pycache__ /app/azbot/bot_pkg/__pycache__ \
    && chown -R node:node /app/azbot /app/azbot-venv

# Low-RAM defaults for a 0.5-1 GB box. Override in Sevalla dashboard.
ENV QUEUE_WORKERS=1 \
    UPLOAD_WORKERS=1 \
    DRIVE_UPLOAD_CONCURRENCY=1 \
    HANDLER_WORKERS=8 \
    LOG_LEVEL=INFO

ENTRYPOINT ["/entrypoint-combined.sh"]

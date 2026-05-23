FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NODE_MAJOR=20 \
    CLAUDE_CONFIG_DIR=/claude-config

WORKDIR /app

# System packages + Node 20 (needed for @anthropic-ai/claude-code) + sqlite CLI
# for debugging/backup verification.
RUN apt-get update && apt-get install -y --no-install-recommends \
        sqlite3 curl ca-certificates gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

# Pre-download the embedding model (~150 MB) at build time so the first
# note save isn't a multi-second silent stall while fastembed pulls weights.
# Cached under /root/.cache/fastembed; baked into the image layer.
ARG INFOGUANA_EMBED_MODEL=BAAI/bge-small-en-v1.5
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='${INFOGUANA_EMBED_MODEL}')"

COPY app ./app
COPY scripts ./scripts
# Strip CRLF -> LF on the entrypoint in case the host checked it out with
# Windows line endings (Git for Windows defaults to core.autocrlf=true).
# Belt and suspenders with .gitattributes.
RUN sed -i 's/\r$//' /app/scripts/docker-entrypoint.sh \
    && chmod +x /app/scripts/docker-entrypoint.sh

ENV INFOGUANA_DB_PATH=/data/infoguana.db \
    INFOGUANA_BACKUP_DIR=/backups
VOLUME ["/data", "/backups", "/claude-config"]
EXPOSE 8789

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:8789/healthz || exit 1

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["python", "-m", "app.main"]

# ──────────────────────────────────────────────────────────────
# agent-node v2.0 — Lightweight Linux deployment node
# Primary: Web health-check + monitoring service
# Secondary: Token discovery, command exec, WebSocket terminal
#
# Built from: Alpine 3.20 — minimal base, ~5MB
# ──────────────────────────────────────────────────────────────

FROM alpine:3.20

# ─── Core packages (Alpine 3.20 compatible) ─────────────────────
RUN apk add --no-cache --no-progress \
    python3 \
    py3-pip \
    bash \
    curl \
    wget \
    git \
    nano \
    tmux \
    procps \
    bind-tools \
    iproute2 \
    net-tools \
    openssh \
    && echo "SSH client: $(which ssh || echo 'NOT FOUND')" \
    && python3 -m pip install --no-cache-dir \
        aiohttp \
        pyyaml

# ─── App setup ─────────────────────────────────────────────────
WORKDIR /app
COPY app.py /app/app.py
COPY requirements.txt /app/requirements.txt
COPY config.yaml /app/config.yaml
COPY entrypoint.sh /app/entrypoint.sh
COPY scripts/ /app/scripts/

RUN chmod +x /app/entrypoint.sh /app/scripts/*.sh && \
    mkdir -p /var/log/agent-node

# ─── Health check ──────────────────────────────────────────────
# Uses curl against /health endpoint (curl is installed above)
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -sf http://127.0.0.1:${PORT:-8080}/health || exit 1

# ─── Expose ────────────────────────────────────────────────────
EXPOSE 8080

# ─── Env defaults ─────────────────────────────────────────────
ENV HOME=/root \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    TERM=xterm-256color \
    PYTHONUNBUFFERED=1 \
    BOOTSTRAP_ENABLED=true

# ─── Startup ─────────────────────────────────────────────────
# PORT and HOST=0.0.0.0 are injected by the hosting platform
ENTRYPOINT ["/app/entrypoint.sh"]

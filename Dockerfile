# ──────────────────────────────────────────────────────────────
# agent-node v2.1 — Lightweight Linux deployment node
# Primary: Web health-check + monitoring service
# Secondary: Token discovery, command exec, WebSocket terminal
#
# Built from: Python 3.12-slim — reliable, widely cached
# ──────────────────────────────────────────────────────────────

FROM python:3.12-slim

# ─── System packages ────────────────────────────────────────────
RUN apt-get update -qq 2>/dev/null && \
    apt-get install -y --no-install-recommends \
        bash curl wget git nano tmux \
        openssh-client iproute2 net-tools \
    2>&1 | tail -3 && \
    rm -rf /var/lib/apt/lists/* && \
    echo "SSH client: $(which ssh || echo 'NOT FOUND')"

# ─── Python dependencies ────────────────────────────────────────
# Use pip directly (python:3.12-slim has it built-in)
RUN pip install --no-cache-dir --quiet aiohttp pyyaml

# ─── App setup ─────────────────────────────────────────────────
WORKDIR /app
COPY app.py /app/app.py
COPY requirements.txt /app/requirements.txt
COPY config.yaml /app/config.yaml
COPY entrypoint.sh /app/entrypoint.sh
COPY scripts/ /app/scripts/

RUN chmod +x /app/entrypoint.sh /app/scripts/*.sh && \
    mkdir -p /var/log/agent-node

# ─── ModelScope Studio requires port 7860 ───────────────────────
ENV PORT=7860
ENV HOST=0.0.0.0
ENV PYTHONUNBUFFERED=1
ENV BOOTSTRAP_ENABLED=true

EXPOSE 7860

# ─── Startup ───────────────────────────────────────────────────
ENTRYPOINT ["/app/entrypoint.sh"]

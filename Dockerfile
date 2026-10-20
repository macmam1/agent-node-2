# ──────────────────────────────────────────────────────────────
# agent-node v2.1 — Lightweight Linux deployment node
# Primary: Web health-check + monitoring service
# Secondary: Token discovery, command exec, WebSocket terminal
#
# Uses python:3.12-slim (Debian-based) with CN pip mirror for
# ModelScope Studio compatibility
# ──────────────────────────────────────────────────────────────

FROM python:3.12-slim

# ─── Python dependencies (CN mirror for ModelScope) ─────────────
RUN pip install --no-cache-dir --quiet \
    --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    --extra-index-url https://pypi.org/simple \
    aiohttp pyyaml

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

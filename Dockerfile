#syntax=docker/dockerfile:1.4
FROM python:3.12-alpine

# Use Aliyun pip mirror for China
RUN pip install --no-cache-dir --quiet \
    --index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    aiohttp pyyaml

WORKDIR /app
COPY app.py config.yaml /app/
COPY requirements.txt /app/requirements.txt
COPY entrypoint.sh /app/entrypoint.sh
COPY scripts/ /app/scripts/

RUN chmod +x /app/entrypoint.sh /app/scripts/*.sh && \
    mkdir -p /var/log/agent-node && \
    apk add --no-cache curl iproute2 openssh bash

# ModelScope Studio requires port 7860
ENV PORT=7860
ENV HOST=0.0.0.0
ENV PYTHONUNBUFFERED=1
ENV BOOTSTRAP_ENABLED=true

EXPOSE 7860

CMD ["python3", "/app/app.py"]

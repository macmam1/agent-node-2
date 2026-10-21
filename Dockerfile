FROM python:3.12-slim

# Install system tools agents need
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl wget openssh-client git ca-certificates \
        iproute2 dnsutils net-tools procps vim-tiny \
    && rm -rf /var/lib/apt/lists/*

# Copy app
COPY app.py /app/app.py
WORKDIR /app

# Install Python deps
RUN pip install --no-cache-dir fastapi uvicorn[standard] python-multipart

# Run
ENV HOST=0.0.0.0
ENV PORT=8080
CMD ["python", "app.py"]

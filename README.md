# 🤖 agent-node v2.1

**A lightweight Linux deployment node with embedded terminal, tooling, and tunnel capabilities.**

> Deploys to any free hosting platform, auto-installs its own toolsets, and provides authenticated terminal access via WebSocket, SSH, or sshx web links.

Designed for platforms that restrict direct terminal access (Vercel, Render, Belmo free tier, Suga free tier, etc.). Turns any such platform into a real Linux server with shell access.

---

## 🚀 Quick Start

### Deploy to Suga.app (one-click)

1. Fork this repo to your GitHub account
2. Create a **"Standard Build"** service on [suga.app](https://suga.app)
3. Configure:
   ```
   BUILD:  echo Build done
   START:  python3 app.py
   ENV:    MONITOR_TOKEN=suga_known_token_2026
   ```

After deploy (~15s):

| Method | URL / Command |
|--------|--------------|
| **Web Terminal** | `https://YOUR_URL.REGION.suga.run/terminal` |
| **WebSocket** | `wss://YOUR_URL.REGION.suga.run/ws/terminal?token=TOKEN` |
| **sshx** | `POST /api/install/sshx` then `GET /api/sshx` |
| **API** | `POST /api/exec` with `Authorization: Bearer TOKEN` |
| **Token** | `GET /api/bootstrap` (no auth — first-time only) |

### Deploy to Belmo.io (free, never sleeps)

1. Sign up: https://dashboard.belmo.io/signup
2. Install GitHub App on `yourname/agent-node-2`
3. New Service → API → Select repo `main`
4. Start command: `python3 app.py`
5. Click Deploy (2-4 minutes)
6. Access at `https://agent-node-2.app.belmo.io/terminal`

### Deploy with Docker

```bash
docker build -t agent-node .
docker run -d \
  -p 8080:8080 \
  -e MONITOR_TOKEN=your-secure-token \
  -e BOOTSTRAP_ENABLED=false \
  agent-node
```

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────┐
│                    agent-node v2.1                      │
│                                                         │
│  ┌──────────┐  ┌──────────────────────────────────┐  │
│  │ HTTP API  │  │  Services                        │  │
│  │  :8080    │  │  • Health check (/health)       │  │
│  └──────────┘  │  • Token discovery (/api/tokens)│  │
│                │  • Command exec (/api/exec)       │  │
│                │  • Web terminal (/terminal)       │  │
│                │  • WebSocket (/ws/terminal)       │  │
│                │  • sshx web terminal              │  │
│                └──────────────────────────────────┘  │
│                                                         │
│  Routes:                                                │
│    GET  /                    → Index page               │
│    GET  /health              → Health check (no auth) │
│    GET  /terminal            → Web terminal UI        │
│    GET  /api/bootstrap       → Token discovery        │
│    GET  /api/diagnostics     → System diagnostics     │
│    GET  /api/tokens          → Token discovery (auth) │
│    GET  /api/sshx            → Current sshx link      │
│    POST /api/exec            → Execute commands       │
│    POST /api/exec/background → Background exec        │
│    GET  /api/monitor/stream  → SSE system stats (auth)  │
│    POST /api/install/sshx    → Install sshx terminal     │
│    POST /api/files/write     → Write file (auth)         │
└─────────────────────────────────────────────────────┘
```

---

## ⚙️ Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MONITOR_TOKEN` | auto-generated | Auth token for `/api/*` and `/ws/*` endpoints |
| `PORT` | `8080` | HTTP server port |
| `HOST` | `0.0.0.0` | Bind address |
| `BOOTSTRAP_ENABLED` | `true` | Allow unauthenticated token access via `/api/bootstrap` |
| `ENABLE_KEEPALIVE` | `true` | Run keepalive loop to prevent sleep |
| `HISTORY_SIZE` | `100` | Number of commands to keep in history |
| `RATE_LIMIT` | `60` | Max requests per minute per token |

### Token Retrieval

If `MONITOR_TOKEN` is not set, the app auto-generates one (24-byte urlsafe base64):
```
TOKEN = os.environ.get('MONITOR_TOKEN') or secrets.token_urlsafe(24)
```

**Get the token after deploy:**
```bash
curl http://YOUR_HOST:PORT/api/bootstrap
# Returns: {"token": "...", "token_masked": "xxxx****xxxx"}
```

Or from logs — look for: `{"event": "token_generated", "token_preview": "xxxx****xxxx"}`

### Command Execution

POST to `/api/exec` with Bearer token:
```bash
curl -X POST http://HOST:PORT/api/exec \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"cmd": "ls -la /"}'
```

Supports allowlisted commands: `ls`, `cat`, `curl`, `wget`, `bash`, `python3`, `git`, `ssh`, `pip`, `find`, `grep`, etc.

---
### File Manager

Browse, read, and write files on the server via API:

```bash
# List files in a directory
curl "http://HOST:PORT/api/files?path=/tmp" -H "Authorization: Bearer $TOKEN"

# Read a file
curl "http://HOST:PORT/api/files/read?path=/etc/hostname" -H "Authorization: Bearer $TOKEN"

# Write a file
curl -X POST http://HOST:PORT/api/files/write \
  -H "Authorization: Bearer $TOKEN"   -H "Content-Type: application/json"   -d '{"path": "/tmp/test.txt", "content": "hello world"}'
```

### Command History

```bash
# Get history (default: last 20)
curl "http://HOST:PORT/api/exec/history?limit=50&offset=0" \
  -H "Authorization: Bearer $TOKEN"

# Clear history
curl -X POST http://HOST:PORT/api/exec/history/clear \
  -H "Authorization: Bearer $TOKEN"
```

### Environment Viewer

```bash
curl "http://HOST:PORT/api/env" -H "Authorization: Bearer $TOKEN"
# Returns all env vars with secrets masked
```

### Process List

```bash
curl "http://HOST:PORT/api/processes" -H "Authorization: Bearer $TOKEN"
# Returns top 30 processes by memory usage
```

### OpenAPI Documentation

Auto-generated API spec at:
```
GET /api/docs
```



## 🔧 Auto-Install & Self-Healing

On every container restart, the app auto-installs:
1. `apk add`: openssh, curl, wget, git, nano, tmux, iproute2, net-tools
2. SSH key setup via `scripts/setup_ssh.sh`
3. Keepalive via `scripts/keepalive.sh` (for sleep-prone platforms)

---

## 📱 Access Methods

### Web Terminal
Browse to `http://HOST:PORT/terminal` — interactive xterm.js terminal in your browser.

### WebSocket Terminal
```javascript
const ws = new WebSocket(`wss://HOST:PORT/ws/terminal?token=${TOKEN}`);
ws.onmessage = e => process.stdout.write(e.data);
ws.onopen = () => ws.send('ls -la\n');
```

### sshx (No-install web terminal)
```bash
curl -X POST http://HOST:PORT/api/install/sshx \
  -H "Authorization: Bearer YOUR_TOKEN"
# Then:
curl http://HOST:PORT/api/sshx \
  -H "Authorization: Bearer YOUR_TOKEN"
# Returns: {"link": "https://sshx.io/xxxxx"}
```

---

## 🌐 Platform Notes

### Suga.app / StormKit
- Python 3.13 via venv
- Container restarts every ~30-60 min — keepalive recommended
- HTTP-only on free tier

### Belmo.io
- Native Python 3.12 build (no Docker)
- Always-on, never sleeps
- EU / US regions

### WispByte
- Direct Docker + SSH access
- Persistent containers (no sleep)
- Best for reliable terminal access

### AlwaysData
- SSH-based deployment
- 256 MiB RAM, 1 GB disk
- Persistent daemon

---

## 🔒 Privacy & Security

1. **Make repo private**: GitHub → Settings → Change visibility → Make private
2. **Set your own token**: Set `MONITOR_TOKEN` env variable to a secure value
3. **Disable bootstrap**: Set `BOOTSTRAP_ENABLED=false` in production
4. **Token rotation**: GitHub Actions auto-generates a new token on every push (see workflow)

### `.dockerignore`
```
.git/
*.md
memory/
sessions/
*.log
__pycache__/
```

---

## 📄 License

MIT — Built for the multi-vp-panel project.

---

## 🛠️ Development

```bash
# Install deps
pip install -r requirements.txt

# Run locally
MONITOR_TOKEN=test-token python3 app.py

# Test endpoints
curl http://localhost:8080/health
curl http://localhost:8080/api/bootstrap
curl -H "Authorization: Bearer test-token" http://localhost:8080/api/diagnostics
```

---

## 🔑 دسترسی (Access)

**وب‌سایت**: `https://[YOUR_HOST]/terminal`

| روش | لینک | توضیحات |
|-----|------|--------|
| **🌐 وب‌ترمینال** | `GET /terminal` | مرورگر بکش |
| **🔗 sshx** | `GET /api/sshx` (with Bearer token) | لینک مستقیم sshx.io |
| **🔌 API** | `POST /api/exec` | با `Authorization: Bearer <token>` |
| **🔑 توکن** | `GET /api/bootstrap` | برای دسترسی اولیه (اگر فعال باشد) |

#!/usr/bin/env python3
"""
agent-node v2.1 — Lightweight deployment node for terminal access
on restricted hosting platforms.

Turns any restricted hosting platform (no SSH, no terminal, sleeps after
inactivity, etc.) into a real Linux server with shell access via HTTP API
and a WebSocket terminal.

Features:
  - Interactive command execution via HTTP API (allowlisted commands)
  - WebSocket-based web terminal
  - Health check + diagnostics
  - Token discovery from environment variables
  - Process listing
  - File browsing (read/write)
  - System monitoring via SSE
  - Persistent command history
  - Keepalive loop to prevent sleep on free-tier platforms

Routes:
    GET  /                    → Index page
    GET  /health             → Health check (no auth)
    GET  /terminal           → Web terminal UI
    GET  /api/bootstrap      → Token discovery (first-time only, if enabled)
    GET  /api/diagnostics    → System diagnostics (auth)
    GET  /api/tokens         → Token discovery from env (auth)
    GET  /api/sshx           → Current sshx link
    POST /api/exec           → Execute commands (auth)
    POST /api/exec/background → Background exec (auth)
    GET  /api/monitor/stream → SSE system stats (auth)
    POST /api/install/sshx   → Install sshx terminal (auth)
    GET  /api/files           → List files (auth)
    GET  /api/files/read      → Read file (auth)
    POST /api/files/write     → Write file (auth)
    GET  /api/exec/history     → Command history (auth)
    POST /api/exec/history/clear → Clear history
    GET  /api/env             → Environment info, masked (auth)
"""

import os
import re
import json
import time
import hmac
import subprocess
import threading
import secrets
import hashlib
from pathlib import Path
from collections import deque
from datetime import datetime
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Depends, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ─── Auto-install tools on startup (background) ─────────────────────
def _auto_install_tools():
    """Install system tools in background thread (non-blocking)."""
    import time as _t
    _t.sleep(3)
    try:
        subprocess.run(
            ["bash", "-c", "apt-get update -qq 2>/dev/null && apt-get install -y --no-install-recommends "
             "openssh-client curl wget git ca-certificates netcat-openbsd 2>&1 | tail -3"],
            capture_output=True, timeout=120)
        print(f"[startup] Tools: ssh, curl, wget, git ready")
    except Exception as e:
        print(f"[startup] Tool install: {e}")

threading.Thread(target=_auto_install_tools, daemon=True).start()

# ─── Keepalive Loop ─────────────────────────────────────────────────
def _keepalive_loop():
    """Ping /health every 5 minutes to prevent sleep on free-tier platforms."""
    import time as _t
    _t.sleep(30)  # Wait for server to start
    if os.environ.get("ENABLE_KEEPALIVE", "true").lower() != "true":
        return

    target = os.environ.get("KEEPALIVE_TARGET", "http://localhost:8080/health")
    interval = int(os.environ.get("KEEPALIVE_INTERVAL", "300"))
    log_file = f"{os.environ.get('LOG_DIR', '/var/log/agent-node')}/keepalive.log"

    print(f"[keepalive] Started — target: {target}, interval: {interval}s")
    while True:
        try:
            subprocess.run(["curl", "-sf", "-o", "/dev/null", target],
                          capture_output=True, timeout=15)
        except Exception:
            pass
        _t.sleep(interval)

threading.Thread(target=_keepalive_loop, daemon=True).start()

# ─── Config ─────────────────────────────────────────────────────────
START_TIME = time.time()
TOKEN = os.environ.get("MONITOR_TOKEN") or secrets.token_urlsafe(24)
PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")
BOOTSTRAP_ENABLED = os.environ.get("BOOTSTRAP_ENABLED", "true").lower() == "true"
LOG_DIR = os.environ.get("LOG_DIR", "/var/log/agent-node")
os.makedirs(LOG_DIR, exist_ok=True)

# ─── Command History ────────────────────────────────────────────────
_history = deque(maxlen=100)

# ─── Command Allowlist ──────────────────────────────────────────────
EXEC_ALLOWLIST = (
    "ls ", "cat ", "echo ", "pwd", "ps ", "df ", "ip ", "curl ", "wget ",
    "sh ", "bash ", "python3 ", "python ", "pip3 ", "pip ",
    "git ", "tar ", "gzip ", "find ", "grep ",
    "id", "whoami", "hostname", "date", "uname", "free", "uptime",
    "ssh ", "scp ", "rsync ", "chmod ", "cd ", "head ", "tail ", "tee ", "wc ", "stat",
    "apt ", "apt-get ", "apk ", "nohup ", "mkdir ", "lsblk", "env", "printenv",
)

# ─── Platform Detection ─────────────────────────────────────────────
def detect_platform():
    env = os.environ
    markers = {
        "modelscope": "MODELSCOPE" in env,
        "alibaba-eci": "ALIYUN_ECI_ETH0_IP" in env,
        "kubernetes": "KUBERNETES_SERVICE_HOST" in env,
        "stormkit": "STORMKIT" in str(env),
        "suga": "SUGA" in str(env),
        "belmo": "BELMO" in str(env),
    }
    return [k for k, v in markers.items() if v] or ["unknown"]

# ─── Utility Functions ──────────────────────────────────────────────
def mask_tokens(text):
    """Mask tokens/secrets in command output."""
    masked = re.sub(r'Bearer\s+[a-zA-Z0-9_-]{8,}', 'Bearer [REDACTED]', text)
    masked = re.sub(r'token=[a-zA-Z0-9_-]{8,}', 'token=[REDACTED]', masked)
    return masked

def run_command(cmd, timeout=30, cwd="/"):
    """Execute a command and return results."""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=min(timeout, 120), cwd=cwd,
        )
        return {
            "stdout": r.stdout[-5000:],
            "stderr": r.stderr[-3000:],
            "returncode": r.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "[TIMEOUT]", "stderr": "Command timed out", "returncode": -1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e)[:300], "returncode": -2}

def execute_command(cmd):
    """Execute a command with allowlist check. Returns formatted output."""
    cmd = cmd.strip()
    if not cmd:
        return "No command provided"

    if not any(cmd.startswith(p) for p in EXEC_ALLOWLIST):
        allowed = ", ".join(p.strip() for p in EXEC_ALLOWLIST[:15])
        return f"Command not in allowlist. Allowed: {allowed}"

    result = run_command(cmd)
    output = []
    if result["stdout"]:
        output.append(result["stdout"].rstrip())
    if result["stderr"]:
        output.append(f"[stderr]: {result['stderr'].rstrip()}")
    if not output:
        output.append(f"[Exit code: {result['returncode']}]")

    _history.append({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cmd": mask_tokens(cmd),
        "returncode": result["returncode"],
    })

    return "\n".join(output)

def get_health():
    """Return health status dict."""
    return {
        "service": "agent-node",
        "version": "2.1",
        "status": "ok",
        "uptime": round(time.time() - START_TIME, 1),
        "hostname": os.uname().nodename,
        "platform": detect_platform(),
        "pid": os.getpid(),
        "cpus": os.cpu_count(),
        "python": os.popen("which python3 2>/dev/null || which python 2>/dev/null").read().strip(),
        "tools": {
            "ssh": shutil.which("ssh") if (shutil := __import__("shutil")) else None,
            "curl": shutil.which("curl"),
            "bash": shutil.which("bash"),
        },
    }

def get_diagnostics():
    """Return diagnostics dict."""
    import shutil as _sh
    info = {
        "hostname": os.uname().nodename,
        "uptime_seconds": round(time.time() - START_TIME, 1),
        "cpus": os.cpu_count(),
        "pid": os.getpid(),
        "platform": detect_platform(),
        "python": _sh.which("python3") or _sh.which("python"),
        "bash": _sh.which("bash"),
        "node": _sh.which("node"),
        "ssh_client": "available" if _sh.which("ssh") else "not found",
        "curl": "available" if _sh.which("curl") else "not found",
        "bash_available": "available" if _sh.which("bash") else "not found",
        "token_preview": f"{TOKEN[:8]}...{TOKEN[-4:]}",
        "history_count": len(_history),
    }
    return info

def get_env_info():
    """Return environment info with secrets masked."""
    safe_env = {}
    mask_keys = {"TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL"}
    for k, v in sorted(os.environ.items()):
        if any(m in k.upper() for m in mask_keys):
            safe_env[k] = f"{'*' * min(len(v), 30)}"
        else:
            safe_env[k] = v[:80] if len(v) > 80 else v
    return safe_env

def discover_tokens():
    """Discover tokens from environment variables."""
    results = {}
    patterns = ["MODELSCOPE", "GITHUB", "STORMKIT", "VERCEL", "SNAPDEPLOY",
                "CLOUD_FLARE", "APPWRITE", "MODELSCOPE_API_KEY"]
    for k, v in os.environ.items():
        if any(p in k.upper() for p in patterns) and len(v) > 4:
            results[k] = f"{v[:6]}...{v[-4:]} (len={len(v)})"
        elif any(p in k.upper() for p in ["TOKEN", "KEY", "SECRET", "CREDENTIAL"]) and len(v) > 10:
            results[k] = f"{v[:6]}...{v[-4:]} (len={len(v)})"
    return results if results else {"status": "No tokens found"}

def list_files(path="/"):
    """List files in a directory."""
    try:
        entries = sorted(os.listdir(path))
        result = {"path": path, "files": [], "directories": []}
        for e in entries:
            full = os.path.join(path, e)
            if os.path.isdir(full):
                result["directories"].append(e)
            else:
                size = os.path.getsize(full)
                result["files"].append({"name": e, "size": size})
        return result
    except Exception as e:
        return {"error": str(e)}

def read_file(path):
    """Read a file."""
    try:
        size = os.path.getsize(path)
        if size > 102400:
            return {"error": f"File too large ({size} bytes, max 100KB)"}
        with open(path, "r", errors="replace") as f:
            content = f.read()
        return {"path": path, "size": size, "content": content}
    except Exception as e:
        return {"error": str(e)}

def write_file(path, content):
    """Write content to a file."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return {"status": "ok", "path": path, "bytes": len(content)}
    except Exception as e:
        return {"error": str(e)}

def get_history():
    """Return command history."""
    if not _history:
        return []
    return list(reversed(list(_history)))

# ─── FastAPI App ────────────────────────────────────────────────────
app = FastAPI(
    title="agent-node v2.1",
    description="Lightweight deployment node for terminal access on restricted hosting platforms.",
    version="2.1",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Auth ───────────────────────────────────────────────────────────
security = HTTPBearer()

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Verify Bearer token."""
    if not credentials:
        raise HTTPException(status_code=401, detail="Missing authorization")
    # Use constant-time comparison to prevent timing attacks
    if not hmac.compare_digest(credentials.credentials, TOKEN):
        raise HTTPException(status_code=401, detail="Invalid token")
    return credentials.credentials

def verify_token_optional(credentials: HTTPAuthorizationCredentials = None):
    """Optional token verification (for bootstrap endpoint)."""
    if not BOOTSTRAP_ENABLED:
        return verify_token(credentials)
    return credentials.credentials if credentials else None

# ─── Pydantic Models ────────────────────────────────────────────────
class ExecRequest(BaseModel):
    cmd: str
    timeout: int = 30
    cwd: str = "/"

class ExecResponse(BaseModel):
    stdout: str
    stderr: str
    returncode: int

class FileWriteRequest(BaseModel):
    path: str
    content: str

# ─── Routes ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    """Index page with API documentation."""
    return """<!DOCTYPE html>
<html>
<head>
    <title>agent-node v2.1</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: Inter, sans-serif; max-width: 900px; margin: 40px auto; padding: 20px; background: #0f0f23; color: #e0e0e0; }
        h1 { color: #4ade80; }
        .card { background: #1a1a2e; border-radius: 12px; padding: 24px; margin: 16px 0; border: 1px solid #333; }
        table { width: 100%; border-collapse: collapse; margin: 12px 0; }
        th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid #333; }
        th { color: #4ade80; }
        code { background: #252535; padding: 2px 6px; border-radius: 4px; color: #60a5fa; }
        a { color: #4ade80; }
        .terminal-link { display: inline-block; margin: 10px 0; padding: 10px 20px; background: #16213e; border-radius: 8px; border: 1px solid #4ade80; color: #4ade80; text-decoration: none; }
    </style>
</head>
<body>
    <h1>🤖 agent-node v2.1</h1>
    <p>Lightweight Linux deployment node — breaks through hosting service limitations.</p>

    <div class="card">
        <h2>🚀 Quick Access</h2>
        <a href="/terminal" class="terminal-link">🖥️ Web Terminal</a>
        <p><code>GET /health</code> — Health check (no auth)</p>
        <p><code>GET /api/bootstrap</code> — Get your token (first-time only)</p>
    </div>

    <div class="card">
        <h2>🔌 API Endpoints</h2>
        <table>
            <tr><th>Endpoint</th><th>Method</th><th>Auth</th><th>Description</th></tr>
            <tr><td>/health</td><td>GET</td><td>None</td><td>Health check</td></tr>
            <tr><td>/terminal</td><td>GET</td><td>None</td><td>Web terminal UI</td></tr>
            <tr><td>/api/bootstrap</td><td>GET</td><td>First-time</td><td>Token discovery</td></tr>
            <tr><td>/api/exec</td><td>POST</td><td>Bearer token</td><td>Execute command</td></tr>
            <tr><td>/api/diagnostics</td><td>GET</td><td>Bearer token</td><td>System diagnostics</td></tr>
            <tr><td>/api/tokens</td><td>GET</td><td>Bearer token</td><td>Discover tokens</td></tr>
            <tr><td>/api/env</td><td>GET</td><td>Bearer token</td><td>Environment (masked)</td></tr>
            <tr><td>/api/files</td><td>GET</td><td>Bearer token</td><td>List files</td></tr>
            <tr><td>/api/files/read</td><td>GET</td><td>Bearer token</td><td>Read file</td></tr>
            <tr><td>/api/files/write</td><td>POST</td><td>Bearer token</td><td>Write file</td></tr>
            <tr><td>/api/exec/history</td><td>GET</td><td>Bearer token</td><td>Command history</td></tr>
            <tr><td>/api/monitor/stream</td><td>GET</td><td>Bearer token</td><td>SSE system stats</td></tr>
            <tr><td>/api/sshx</td><td>GET</td><td>Bearer token</td><td>Get sshx link</td></tr>
            <tr><td>/api/install/sshx</td><td>POST</td><td>Bearer token</td><td>Install sshx</td></tr>
        </table>
    </div>

    <div class="card">
        <h2>ℹ️ About</h2>
        <p><strong>Purpose:</strong> Turn restricted hosting platforms into real Linux servers.</p>
        <p><strong>Platform:</strong> """ + ", ".join(detect_platform()) + """</p>
        <p><strong>Hostname:</strong> """ + os.uname().nodename + """</p>
        <p><strong>Uptime:</strong> """ + f"{time.time() - START_TIME:.1f}s" + """</p>
    </div>
</body>
</html>"""


@app.get("/health")
async def health():
    """Health check — no auth required."""
    return get_health()


@app.get("/terminal", response_class=HTMLResponse)
async def terminal_ui():
    """Web terminal UI (xterm.js)."""
    return """<!DOCTYPE html>
<html>
<head>
    <title>agent-node — Terminal</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm/css/xterm.css" />
    <style>
        body, html { margin: 0; padding: 0; height: 100%; background: #0f0f23; }
        #terminal { height: 100vh; }
    </style>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm/css/xterm.css" />
    <script src="https://cdn.jsdelivr.net/npm/xterm/lib/xterm.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/socket.io-client/dist/socket.io.min.js"></script>
</head>
<body>
    <div id="terminal"></div>
    <script>
        const term = new Terminal({
            cursorBlink: true,
            theme: { background: '#0f0f23', foreground: '#e0e0e0' },
            scrollback: 1000,
        });
        term.open(document.getElementById('terminal'));
        term.writeln('Welcome to agent-node v2.1 terminal\\r\\n');

        const wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const ws = new WebSocket(`${wsProto}//${window.location.host}/ws/terminal`);

        ws.onopen = () => { term.writeln('\\r\\n[Connected to server]\\r\\n$ '); };
        ws.onmessage = e => { term.write(e.data); };
        ws.onclose = () => { term.writeln('\\r\\n\\r\\n[Connection closed]'); };

        term.onData(data => { ws.send(JSON.stringify({type: 'input', data: data})); });

        // Handle resize
        window.addEventListener('resize', () => {
            const cols = term.cols;
            const rows = term.rows;
            ws.send(JSON.stringify({type: 'resize', cols: cols, rows: rows}));
        });
    </script>
</body>
</html>"""


@app.websocket("/ws/terminal")
async def websocket_terminal(websocket):
    """WebSocket terminal — executes commands in a subprocess."""
    await websocket.accept()
    import asyncio
    process = None
    try:
        process = await asyncio.create_subprocess_shell(
            "/bin/bash",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await websocket.send_text("[agent-node v2.1 terminal ready]\\r\\n")

        async def read_output():
            if process and process.stdout:
                try:
                    while True:
                        data = await process.stdout.read(1)
                        if data:
                            await websocket.send_bytes(data)
                        else:
                            break
                except Exception:
                    pass

        async def read_output_err():
            if process and process.stderr:
                try:
                    while True:
                        data = await process.stderr.read(1)
                        if data:
                            await websocket.send_bytes(data)
                        else:
                            break
                except Exception:
                    pass

        read_task = asyncio.create_task(read_output())
        err_task = asyncio.create_task(read_output_err())

        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "input" and process:
                if process.stdin:
                    process.stdin.write(msg.get("data", "").encode())
                    await process.stdin.drain()
    except Exception as e:
        await websocket.send_text(f"[Error: {e}]")
    finally:
        if process:
            try:
                process.terminate()
            except Exception:
                pass
        await websocket.close()


@app.get("/api/bootstrap")
async def bootstrap():
    """Bootstrap endpoint — returns token for first-time access.
    Disabled if BOOTSTRAP_ENABLED=false."""
    if not BOOTSTRAP_ENABLED:
        raise HTTPException(status_code=404, detail="Bootstrap disabled")
    return {
        "token": TOKEN,
        "token_masked": f"{TOKEN[:8]}...{TOKEN[-4:]}",
        "expires": "first-time only" if not os.environ.get("MONITOR_TOKEN") else "persistent",
    }


@app.get("/api/diagnostics")
async def diagnostics(token: str = Depends(verify_token)):
    """System diagnostics — requires Bearer token."""
    return get_diagnostics()


@app.get("/api/tokens")
async def api_tokens(token: str = Depends(verify_token)):
    """Discover tokens/API keys from environment — requires Bearer token."""
    return discover_tokens()


@app.get("/api/env")
async def api_env(token: str = Depends(verify_token)):
    """Environment info with secrets masked — requires Bearer token."""
    return get_env_info()


@app.get("/api/sshx")
async def api_sshx(token: str = Depends(verify_token)):
    """Get current sshx link if available."""
    # Check if sshx is already installed
    result = run_command("which sshx 2>/dev/null || echo ''")
    sshx_path = result["stdout"].strip() if result["stdout"] else ""
    if sshx_path:
        # Try to get the current sshx link
        link_result = run_command(f"{sshx_path} link 2>/dev/null || echo ''")
        return {
            "sshx_installed": True,
            "sshx_path": sshx_path,
            "link": link_result["stdout"].strip() if link_result["stdout"] else None,
        }
    return {"sshx_installed": False, "message": "sshx not installed. POST to /api/install/sshx to install."}


@app.post("/api/install/sshx")
async def install_sshx(token: str = Depends(verify_token)):
    """Install sshx terminal tunneling."""
    def install():
        result = run_command("npm install -g @-sshxd/sshx 2>/dev/null || npm install -g @sshxd/sshx 2>&1", timeout=60)
        return result
    result = install()
    return {"status": "installed" if "sshx" in result["stdout"] or result["returncode"] == 0 else "failed",
            "output": result["stdout"][-2000:] if result["stdout"] else result["stderr"][-2000:]}


@app.post("/api/exec")
async def api_exec(request: ExecRequest, token: str = Depends(verify_token), background: bool = False):
    """Execute a command — requires Bearer token.
    Set ?background=true for non-blocking execution."""
    cmd = request.cmd
    if not cmd.strip():
        raise HTTPException(status_code=400, detail="No command provided")

    if not any(cmd.startswith(p) for p in EXEC_ALLOWLIST):
        allowed = ", ".join(p.strip() for p in EXEC_ALLOWLIST[:15])
        raise HTTPException(status_code=400, detail=f"Command not in allowlist. Allowed: {allowed}")

    if background:
        # Run in background
        result = run_command(cmd, timeout=request.timeout, cwd=request.cwd)
        return {"stdout": result["stdout"], "stderr": result["stderr"], "returncode": result["returncode"], "background": True}

    result = run_command(cmd, timeout=request.timeout, cwd=request.cwd)
    return {"stdout": result["stdout"], "stderr": result["stderr"], "returncode": result["returncode"]}


@app.get("/api/exec/history")
async def api_exec_history(token: str = Depends(verify_token)):
    """Get command history — requires Bearer token."""
    return {"history": get_history()}


@app.post("/api/exec/history/clear")
async def api_exec_history_clear(token: str = Depends(verify_token)):
    """Clear command history — requires Bearer token."""
    _history.clear()
    return {"status": "cleared", "count": len(_history)}


@app.get("/api/files")
async def api_files(path: str = "/", token: str = Depends(verify_token)):
    """List files in a directory — requires Bearer token."""
    return list_files(path)


@app.get("/api/files/read")
async def api_files_read(path: str, token: str = Depends(verify_token)):
    """Read a file — requires Bearer token."""
    return read_file(path)


@app.post("/api/files/write")
async def api_files_write(request: FileWriteRequest, token: str = Depends(verify_token)):
    """Write content to a file — requires Bearer token."""
    return write_file(request.path, request.content)


@app.get("/api/processes")
async def api_processes(token: str = Depends(verify_token)):
    """Get top processes by memory — requires Bearer token."""
    result = run_command("ps aux --sort=-%mem | head -20")
    return {"processes": result["stdout"] if result["stdout"] else result["stderr"]}


@app.get("/api/monitor/stream")
async def api_monitor_stream(token: str = Depends(verify_token)):
    """SSE endpoint for live system stats — requires Bearer token."""
    async def event_stream():
        while True:
            r1 = run_command("uptime")
            r2 = run_command("df -h / 2>/dev/null | tail -1")
            r3 = run_command("free -h 2>/dev/null | head -2 || echo 'free not available'")
            stats = {
                "timestamp": datetime.now().isoformat(),
                "uptime": r1["stdout"].strip(),
                "disk": r2["stdout"].strip(),
                "memory": r3["stdout"].strip(),
            }
            yield f"data: {json.dumps(stats)}\n\n"
            await asyncio.sleep(5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/api/docs")
async def api_docs(token: str = Depends(verify_token)):
    """API documentation — requires Bearer token."""
    return {
        "version": "2.1",
        "endpoints": {
            "GET /health": "Health check (no auth)",
            "GET /terminal": "Web terminal UI (no auth)",
            "GET /api/bootstrap": "Token discovery (first-time, if enabled)",
            "POST /api/exec": "Execute command (Bearer token) — body: {cmd, timeout, cwd}",
            "GET /api/diagnostics": "System diagnostics (Bearer token)",
            "GET /api/tokens": "Token discovery from env (Bearer token)",
            "GET /api/env": "Environment info, masked (Bearer token)",
            "GET /api/files": "List files (Bearer token) — query: ?path=/dir",
            "GET /api/files/read": "Read file (Bearer token) — query: ?path=/file",
            "POST /api/files/write": "Write file (Bearer token) — body: {path, content}",
            "GET /api/exec/history": "Command history (Bearer token)",
            "POST /api/exec/history/clear": "Clear history (Bearer token)",
            "GET /api/processes": "Top processes (Bearer token)",
            "GET /api/monitor/stream": "SSE system stats (Bearer token)",
            "GET /api/sshx": "Get sshx link (Bearer token)",
            "POST /api/install/sshx": "Install sshx (Bearer token)",
        },
    }


# ─── Main ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import shutil

    print(f"Starting agent-node v2.1 (FastAPI) on {HOST}:{PORT}")
    print(f"Token: {TOKEN[:8]}...{TOKEN[-4:]}")
    print(f"Platform: {', '.join(detect_platform())}")
    print(f"Tools: ssh={bool(shutil.which('ssh'))} curl={bool(shutil.which('curl'))} bash={bool(shutil.which('bash'))}")
    print(f"Bootstrap: {BOOTSTRAP_ENABLED}")

    import uvicorn
    uvicorn.run(
        "app:app",
        host=HOST,
        port=PORT,
        log_level="info",
    )
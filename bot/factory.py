"""Factory engine management: project creation, tmux sessions, log monitoring."""

import asyncio
import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Awaitable, Callable

from . import config, state

log = logging.getLogger(__name__)

# Engine definitions
ENGINES = {
    "claude": {
        "name": "Claude Code",
        "template": "CLAUDE.md",
        "start_cmd": 'claude --dangerously-skip-permissions -p "Read CLAUDE.md and run /factory"',
        "install": "npm install -g @anthropic-ai/claude-code",
        "check": "claude --version",
    },
    "gemini": {
        "name": "Gemini CLI",
        "template": "GEMINI.md",
        "start_cmd": 'gemini -p "Read GEMINI.md and run /factory"',
        "install": "npm install -g @anthropic-ai/gemini-cli",
        "check": "gemini --version",
    },
    "opencode": {
        "name": "OpenCode",
        "template": "OPENCODE.md",
        "start_cmd": "opencode",
        "install": "curl -fsSL https://opencode.ai/install | bash",
        "check": "opencode --version",
    },
    "aider": {
        "name": "Aider",
        "template": "aider.conf.yml",
        "start_cmd": "aider --yes-always",
        "install": "pip install aider-chat",
        "check": "aider --version",
    },
}


def _tmux_session_name(project: str, engine: str) -> str:
    return f"{project}-{engine}"


def _project_dir(project: str, engine: str) -> Path:
    return config.FACTORY_ROOT / f"{project}-{engine}"


def _log_file(project: str, engine: str) -> Path:
    return _project_dir(project, engine) / "artifacts" / "reports" / "factory-run.log"


# --- Project setup ---

def setup_project(project_name: str, engine: str, requirements: str,
                  deploy_config: dict | None = None) -> Path:
    """Create project directory, copy template, write requirements. Returns project dir.

    deploy_config may contain: project_type, deploy, deploy_server, subdomain.
    """
    proj_dir = _project_dir(project_name, engine)
    proj_dir.mkdir(parents=True, exist_ok=True)

    # Create artifacts structure
    for subdir in ("requirements", "reports", "architecture", "code",
                   "tests", "reviews", "docs", "release"):
        (proj_dir / "artifacts" / subdir).mkdir(parents=True, exist_ok=True)

    # Copy engine template
    eng = ENGINES[engine]
    template_src = config.TEMPLATES_DIR / eng["template"]
    if template_src.exists():
        template_dst = proj_dir / eng["template"]
        shutil.copy2(template_src, template_dst)

    # Write requirements
    raw_input = proj_dir / "artifacts" / "requirements" / "raw-input.md"
    raw_input.write_text(requirements)

    # Write deployment config if provided
    if deploy_config:
        _write_deploy_config(proj_dir, deploy_config)

    # Init git repo
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=proj_dir, capture_output=True,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=proj_dir, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial project setup"],
        cwd=proj_dir, capture_output=True,
    )

    return proj_dir


def _write_deploy_config(proj_dir: Path, dc: dict) -> None:
    """Write artifacts/requirements/deploy-config.md so the factory knows deployment targets."""
    ptype = dc.get("project_type", "standalone")
    deploy = dc.get("deploy", False)
    server = dc.get("deploy_server", "")
    subdomain = dc.get("subdomain", "")

    type_labels = {"bot": "Telegram Bot", "web": "Web Service / API", "standalone": "Standalone Software"}

    lines = [
        "# Deployment Configuration",
        "",
        f"**Project type:** {type_labels.get(ptype, ptype)}",
        f"**Deploy:** {'Yes' if deploy else 'No'}",
    ]

    if deploy:
        lines.append(f"**Deploy method:** Docker via SSH")
        if server:
            lines.append(f"**Deploy server:** `{server}`")
        if subdomain:
            lines.append(f"**URL:** `https://{subdomain}`")

        lines.extend([
            "",
            "## Deployment Requirements",
            "",
            "The factory MUST generate:",
            "- `Dockerfile` (multi-stage build)",
            "- `docker-compose.yml` (with all services: app, DB, reverse proxy if web)",
            "- `artifacts/release/deploy.sh` that:",
            f"  1. SSHs into `{server}`" if server else "  1. SSHs into the deploy server",
            "  2. Pulls/copies the Docker image",
            "  3. Runs docker-compose up -d",
        ])

        if subdomain:
            lines.extend([
                f"  4. Configures nginx reverse proxy for `{subdomain}`",
                "  5. Provisions SSL via certbot/Let's Encrypt",
            ])

        if ptype == "bot":
            lines.extend([
                "",
                "## Bot-specific",
                "- The bot should run as a long-lived Docker container with restart: always",
                "- No web port needed unless there's an admin panel",
            ])

        if ptype == "web":
            lines.extend([
                "",
                "## Web-specific",
                "- Expose via nginx reverse proxy on port 80/443",
                f"- Server name: `{subdomain}`" if subdomain else "",
                "- HTTPS with auto-renewing Let's Encrypt certificate",
            ])
    else:
        lines.extend([
            "",
            "## No Deployment",
            "Generate a DEPLOYMENT.md guide with manual setup instructions only.",
            "Include Dockerfile for optional containerized usage.",
        ])

    deploy_md = proj_dir / "artifacts" / "requirements" / "deploy-config.md"
    deploy_md.write_text("\n".join(line for line in lines if line is not None) + "\n")


# --- tmux session management ---

def start_engine(project_name: str, engine: str) -> str:
    """Start a factory engine in a tmux session. Returns session name."""
    session = _tmux_session_name(project_name, engine)
    proj_dir = _project_dir(project_name, engine)
    eng = ENGINES[engine]

    # Kill existing session if any
    subprocess.run(
        ["tmux", "kill-session", "-t", session],
        capture_output=True,
    )

    # Create new tmux session
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-c", str(proj_dir)],
        check=True, capture_output=True,
    )

    # Send the engine start command
    subprocess.run(
        ["tmux", "send-keys", "-t", session, eng["start_cmd"], "Enter"],
        check=True, capture_output=True,
    )

    # Track the run
    state.add_run(project_name, engine, session)
    log.info("Started %s for project %s (session: %s)", engine, project_name, session)
    return session


def stop_engine(project_name: str, engine: str) -> bool:
    """Stop a factory engine by killing its tmux session."""
    session = _tmux_session_name(project_name, engine)

    # Also touch .factory-stop signal
    stop_file = _project_dir(project_name, engine) / ".factory-stop"
    stop_file.touch()

    result = subprocess.run(
        ["tmux", "kill-session", "-t", session],
        capture_output=True,
    )
    state.update_run(project_name, engine, status="stopped", finished_at=time.time())
    return result.returncode == 0


def is_session_alive(session: str) -> bool:
    """Check if a tmux session exists."""
    result = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True,
    )
    return result.returncode == 0


def get_session_output(session: str, lines: int = 50) -> str:
    """Capture recent output from a tmux session."""
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", session, "-p", "-S", f"-{lines}"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def list_active_sessions() -> list[str]:
    """List all active tmux sessions."""
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    return [s.strip() for s in result.stdout.strip().split("\n") if s.strip()]


# --- Log monitoring ---

_MARKER_RE = re.compile(r"\[FACTORY:(\w+)(?::(.+?))?\]")


def parse_markers(text: str) -> list[dict]:
    """Parse [FACTORY:...] markers from log text."""
    markers = []
    for match in _MARKER_RE.finditer(text):
        marker_type = match.group(1)
        payload = match.group(2) or ""

        if marker_type == "PHASE":
            # [FACTORY:PHASE:N:START] or [FACTORY:PHASE:N:END:score]
            parts = payload.split(":")
            if len(parts) >= 2:
                markers.append({
                    "type": "phase",
                    "phase": int(parts[0]),
                    "action": parts[1].lower(),
                    "score": int(parts[2]) if len(parts) > 2 else None,
                })
        elif marker_type == "CLARIFY":
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                data = {"question": payload}
            markers.append({"type": "clarify", "data": data})
        elif marker_type == "ERROR":
            markers.append({"type": "error", "message": payload})
        elif marker_type == "COST":
            parts = payload.split(":")
            markers.append({
                "type": "cost",
                "amount": float(parts[0]) if parts else 0,
                "provider": parts[1] if len(parts) > 1 else "unknown",
            })
        elif marker_type == "COMPLETE":
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                data = {"summary": payload}
            markers.append({"type": "complete", "data": data})

    return markers


class LogMonitor:
    """Async monitor that tails a factory log file and fires callbacks on markers."""

    def __init__(self, project_name: str, engine: str,
                 on_event: Callable[[dict], Awaitable]):
        self.project_name = project_name
        self.engine = engine
        self.on_event = on_event
        self._task: asyncio.Task | None = None
        self._stop = False
        self._last_pos = 0

    def start(self):
        self._stop = False
        self._last_pos = 0
        self._task = asyncio.create_task(self._monitor_loop())

    def stop(self):
        self._stop = True
        if self._task:
            self._task.cancel()

    async def _monitor_loop(self):
        log_path = _log_file(self.project_name, self.engine)
        session = _tmux_session_name(self.project_name, self.engine)

        while not self._stop:
            await asyncio.sleep(3)

            # Check if tmux session is still alive
            if not is_session_alive(session):
                await self.on_event({
                    "type": "session_died",
                    "project": self.project_name,
                    "engine": self.engine,
                })
                state.update_run(
                    self.project_name, self.engine,
                    status="failed", finished_at=time.time(),
                )
                break

            # Read new log content
            if not log_path.exists():
                continue

            try:
                with open(log_path) as f:
                    f.seek(self._last_pos)
                    new_content = f.read()
                    self._last_pos = f.tell()
            except OSError:
                continue

            if not new_content:
                continue

            # Parse and emit markers
            markers = parse_markers(new_content)
            for marker in markers:
                marker["project"] = self.project_name
                marker["engine"] = self.engine
                try:
                    await self.on_event(marker)
                except Exception as e:
                    log.error("Error in event handler: %s", e)

                # If factory completed, stop monitoring
                if marker["type"] == "complete":
                    state.update_run(
                        self.project_name, self.engine,
                        status="completed", finished_at=time.time(),
                    )
                    self._stop = True
                    break


# --- Engine health check ---

def check_engine(engine: str) -> dict:
    """Check if an engine is installed and get its version."""
    eng = ENGINES.get(engine)
    if not eng:
        return {"installed": False, "error": f"Unknown engine: {engine}"}

    result = subprocess.run(
        eng["check"], shell=True, capture_output=True, text=True, timeout=10,
    )
    if result.returncode == 0:
        version = result.stdout.strip().split("\n")[0]
        return {"installed": True, "version": version}
    return {"installed": False, "error": result.stderr.strip()[:200]}


def check_all_engines() -> dict:
    """Check all engines. Returns dict of engine -> status."""
    return {name: check_engine(name) for name in ENGINES}


# --- System health ---

def system_health() -> dict:
    """Get basic system health info."""
    try:
        import psutil
        return {
            "cpu_percent": psutil.cpu_percent(interval=1),
            "memory": {
                "total_gb": round(psutil.virtual_memory().total / (1024**3), 1),
                "used_percent": psutil.virtual_memory().percent,
            },
            "disk": {
                "total_gb": round(psutil.disk_usage("/").total / (1024**3), 1),
                "used_percent": psutil.disk_usage("/").percent,
            },
        }
    except ImportError:
        return {"error": "psutil not installed"}

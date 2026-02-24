"""JSON-based state management. No database required."""

import json
import time
from pathlib import Path
from typing import Any

from . import config

_USERS_FILE = config.STATE_DIR / "users.json"
_PROJECTS_FILE = config.STATE_DIR / "projects.json"
_SETTINGS_FILE = config.STATE_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "stt_provider": "auto",       # "groq", "openai", "auto"
    "tts_provider": "edge",       # "edge", "openai"
    "tts_voice": "en-US-AriaNeural",  # edge-tts voice name (English — all responses are in English)
    "default_engines": ["claude"],
    "notification_events": ["phase_end", "error", "complete"],
    "quality_threshold": 97,
    "cost_alert": 50.0,
}


def _load_json(path: Path, default: Any = None) -> Any:
    if default is None:
        default = {}
    if not path.exists():
        return default
    return json.loads(path.read_text())


def _save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


# --- Users ---

def load_users() -> dict:
    return _load_json(_USERS_FILE)


def add_user(telegram_id: int, name: str, role: str = "user") -> None:
    users = load_users()
    users[str(telegram_id)] = {
        "name": name,
        "role": role,
        "active": True,
        "added_at": time.time(),
    }
    _save_json(_USERS_FILE, users)


def remove_user(telegram_id: int) -> bool:
    users = load_users()
    key = str(telegram_id)
    if key in users:
        del users[key]
        _save_json(_USERS_FILE, users)
        return True
    return False


# --- Projects ---

def load_projects() -> dict:
    return _load_json(_PROJECTS_FILE)


def save_projects(projects: dict) -> None:
    _save_json(_PROJECTS_FILE, projects)


def create_project(name: str, engines: list[str], description: str,
                   requirements: str, created_by: int,
                   project_type: str = "standalone",
                   deploy: bool = False,
                   deploy_server: str = "",
                   subdomain: str = "") -> dict:
    projects = load_projects()
    project = {
        "engines": engines,
        "description": description,
        "requirements": requirements,
        "project_type": project_type,
        "deploy": deploy,
        "deploy_server": deploy_server,
        "subdomain": subdomain,
        "status": "created",
        "created_by": created_by,
        "created_at": time.time(),
        "runs": [],
    }
    projects[name] = project
    save_projects(projects)
    return project


def add_run(project_name: str, engine: str, tmux_session: str) -> dict:
    projects = load_projects()
    run = {
        "engine": engine,
        "tmux_session": tmux_session,
        "status": "running",
        "phase": 0,
        "started_at": time.time(),
        "finished_at": None,
    }
    projects[project_name]["runs"].append(run)
    projects[project_name]["status"] = "running"
    save_projects(projects)
    return run


def update_run(project_name: str, engine: str, **kwargs) -> None:
    projects = load_projects()
    for run in projects[project_name]["runs"]:
        if run["engine"] == engine and run["status"] == "running":
            run.update(kwargs)
            break
    save_projects(projects)


# --- Settings ---

def load_settings() -> dict:
    settings = _load_json(_SETTINGS_FILE, DEFAULT_SETTINGS.copy())
    # Merge defaults for any missing keys
    for k, v in DEFAULT_SETTINGS.items():
        if k not in settings:
            settings[k] = v
    return settings


def save_settings(settings: dict) -> None:
    _save_json(_SETTINGS_FILE, settings)


def update_setting(key: str, value: Any) -> None:
    settings = load_settings()
    settings[key] = value
    save_settings(settings)

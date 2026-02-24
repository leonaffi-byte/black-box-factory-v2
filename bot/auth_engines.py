"""Engine OAuth / API-key authentication management.

Correct auth flows (learned from @mariozechner/pi-ai source):

  Claude / pi  -- Anthropic PKCE OAuth
      1. Bot builds URL: https://claude.ai/oauth/authorize?...state=<verifier>
      2. User opens URL, logs in at claude.ai
      3. claude.ai redirects to https://console.anthropic.com/oauth/code/callback?code=CODE&state=STATE
      4. User copies "code#state" from their browser address bar, pastes to bot
      5. Bot calls Anthropic token exchange API directly (no local server!)
      6. access_token stored as ANTHROPIC_API_KEY in .env

  Gemini CLI  -- Google PKCE OAuth with local callback server
      1. Bot starts HTTP server on port 8085
      2. Bot builds URL: https://accounts.google.com/o/oauth2/v2/auth?...
      3. User opens URL in browser, browser tries to redirect to http://localhost:8085/oauth2callback
      4. Redirect FAILS (localhost:8085 is on the VPS, not the user's machine)
      5. User copies the full redirect URL from address bar, pastes to bot
      6. Bot parses code+state from URL, does token exchange + project discovery

  OpenCode / Aider / pi  -- Direct API key input
      User pastes the API key, bot stores it in .env
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

log = logging.getLogger(__name__)

ENV_FILE = Path('/opt/factory-bot/.env')
CLAUDE_BIN = '/usr/bin/claude'
GEMINI_BIN = '/usr/bin/gemini'
OPENCODE_BIN = '/usr/local/bin/opencode'
AIDER_BIN = '/usr/local/bin/aider'
PI_BIN = '/usr/bin/pi'

# ── Active OAuth sessions ─────────────────────────────────────────────────────
# engine_key -> session dict
_active: dict[str, dict] = {}


# ── PKCE helpers ──────────────────────────────────────────────────────────────

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()

def _generate_pkce() -> tuple[str, str]:
    """Return (verifier, challenge)."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


# ── .env helpers ──────────────────────────────────────────────────────────────

def _read_env() -> str:
    try:
        return ENV_FILE.read_text()
    except Exception:
        return ""

def get_env_key(key: str) -> Optional[str]:
    content = _read_env()
    m = re.search(rf'^{re.escape(key)}=(.+)$', content, re.MULTILINE)
    return m.group(1).strip() if m else None

def set_env_key(key: str, value: str) -> bool:
    try:
        content = _read_env()
        if re.search(rf'^{re.escape(key)}=', content, re.MULTILINE):
            content = re.sub(rf'^{re.escape(key)}=.*$', f'{key}={value}',
                             content, flags=re.MULTILINE)
        else:
            content += f'\n{key}={value}\n'
        ENV_FILE.write_text(content)
        return True
    except Exception as e:
        log.error("set_env_key failed: %s", e)
        return False

def _mask(key: Optional[str]) -> str:
    return f"{key[:8]}..." if key and len(key) > 8 else "?"


# ══════════════════════════════════════════════════════════════════════════════
# ANTHROPIC OAUTH  (used by: claude, pi)
# No local server needed — redirect is to console.anthropic.com (HTTPS)
# ══════════════════════════════════════════════════════════════════════════════

_ANTHROPIC_CLIENT_ID  = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_ANTHROPIC_AUTH_URL   = "https://claude.ai/oauth/authorize"
_ANTHROPIC_TOKEN_URL  = "https://console.anthropic.com/v1/oauth/token"
_ANTHROPIC_REDIRECT   = "https://console.anthropic.com/oauth/code/callback"
_ANTHROPIC_SCOPES     = "org:create_api_key user:profile user:inference"


async def anthropic_start_oauth() -> tuple[bool, str]:
    """Build and return the Anthropic OAuth URL. No subprocess or server needed."""
    verifier, challenge = _generate_pkce()

    params = {
        "code": "true",
        "client_id": _ANTHROPIC_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": _ANTHROPIC_REDIRECT,
        "scope": _ANTHROPIC_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": verifier,   # state IS the verifier — user must paste both
    }
    url = f"{_ANTHROPIC_AUTH_URL}?{urlencode(params)}"
    _active["anthropic"] = {"verifier": verifier}
    return True, url


async def anthropic_deliver_code(code_and_state: str, target_engine: str) -> tuple[bool, str]:
    """
    Exchange code for access token.
    code_and_state format: "CODE#STATE"  (copied from browser URL bar after redirect)
    """
    import httpx

    info = _active.get("anthropic")
    if not info:
        return False, "No active Anthropic auth session. Use /auth to start a new one."

    # Parse code#state — the redirect URL from claude.ai includes state= param
    # Users may paste either just "code#state" or the full redirect URL
    text = code_and_state.strip()

    if text.startswith("http"):
        # Full URL pasted — extract query params
        parsed = urlparse(text)
        qs = parse_qs(parsed.query)
        code  = (qs.get("code")  or [""])[0]
        state = (qs.get("state") or [""])[0]
    elif "#" in text:
        parts = text.split("#", 1)
        code  = parts[0].strip()
        state = parts[1].strip()
    else:
        code  = text
        state = ""

    if not code:
        return False, "Could not extract authorization code. Paste the full redirect URL or `code#state`."

    verifier = info["verifier"]

    log.info("Anthropic token exchange: code=%s… state=%s…", code[:8], state[:8])

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(_ANTHROPIC_TOKEN_URL, json={
                "grant_type":    "authorization_code",
                "client_id":     _ANTHROPIC_CLIENT_ID,
                "code":          code,
                "state":         state,
                "redirect_uri":  _ANTHROPIC_REDIRECT,
                "code_verifier": verifier,
            })
    except Exception as e:
        return False, f"HTTP error during token exchange: {e}"

    if not resp.is_success:
        return False, f"Token exchange failed ({resp.status_code}): {resp.text[:300]}"

    data = resp.json()
    access_token  = data.get("access_token", "")
    refresh_token = data.get("refresh_token", "")
    expires_in    = data.get("expires_in", 3600)

    if not access_token:
        return False, f"No access_token in response: {resp.text[:200]}"

    _active.pop("anthropic", None)

    # Store as ANTHROPIC_API_KEY for all anthropic-based engines
    ok = set_env_key("ANTHROPIC_API_KEY", access_token)
    if not ok:
        return False, "Token received but failed to save to .env"

    # Also persist refresh token so we can refresh later
    if refresh_token:
        set_env_key("ANTHROPIC_REFRESH_TOKEN", refresh_token)

    # For claude engine: also write to claude's auth JSON so `claude` CLI knows it's logged in
    if target_engine == "claude":
        _write_claude_oauth(access_token, refresh_token, expires_in, data)

    return True, f"Anthropic OAuth OK — access token saved ({_mask(access_token)})"


def _write_claude_oauth(access_token: str, refresh_token: str, expires_in: int, raw: dict):
    """Write OAuth credentials to claude's config files."""
    try:
        claude_json_path = Path("/home/factory/.claude.json")
        try:
            d = json.loads(claude_json_path.read_text()) if claude_json_path.exists() else {}
        except Exception:
            d = {}
        d["loggedIn"] = True
        d["authMethod"] = "oauth"
        d["primaryApiKey"] = access_token
        account = raw.get("account") or {}
        d["oauthAccount"] = {
            "emailAddress": account.get("email", ""),
            "organizationUuid": account.get("organization_uuid", ""),
        }
        claude_json_path.write_text(json.dumps(d, indent=2))
        os.chmod(claude_json_path, 0o600)
        log.info("Wrote OAuth credentials to %s", claude_json_path)
    except Exception as e:
        log.warning("Could not write claude auth JSON: %s", e)


async def anthropic_cancel():
    _active.pop("anthropic", None)


async def claude_auth_status() -> tuple[bool, str]:
    """Check Claude Code login status."""
    try:
        r = subprocess.run(
            [CLAUDE_BIN, 'auth', 'status', '--json'],
            capture_output=True, text=True, timeout=10,
        )
        raw = (r.stdout + r.stderr).strip()
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith('{'):
                try:
                    d = json.loads(line)
                    if d.get('loggedIn'):
                        acc = d.get('oauthAccount') or {}
                        email = acc.get('emailAddress', '?')
                        return True, f"✅ Logged in as {email}"
                    return False, "❌ Not logged in"
                except json.JSONDecodeError:
                    pass
        # Fallback: check ANTHROPIC_API_KEY
        key = get_env_key("ANTHROPIC_API_KEY")
        if key and key.startswith("sk-ant-"):
            return True, f"✅ API key set ({_mask(key)})"
        if key:
            return True, f"✅ OAuth token set ({_mask(key)})"
        return False, "❌ Not logged in"
    except Exception as e:
        return False, f"❌ Error: {e}"


# ══════════════════════════════════════════════════════════════════════════════
# GEMINI CLI OAUTH  (Google Cloud Code Assist)
# Starts local server on port 8085; user pastes the full redirect URL
# ══════════════════════════════════════════════════════════════════════════════

# Gemini OAuth client credentials — loaded from .env (never hardcoded in source)
def _get_gemini_client_id() -> str:
    from dotenv import load_dotenv
    load_dotenv('/opt/factory-bot/.env')
    return os.environ.get("GEMINI_OAUTH_CLIENT_ID", "")

def _get_gemini_client_secret() -> str:
    from dotenv import load_dotenv
    load_dotenv('/opt/factory-bot/.env')
    return os.environ.get("GEMINI_OAUTH_CLIENT_SECRET", "")


_GEMINI_REDIRECT      = "http://localhost:8085/oauth2callback"
_GEMINI_SCOPES        = " ".join([
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
])
_GEMINI_AUTH_URL      = "https://accounts.google.com/o/oauth2/v2/auth"
_GEMINI_TOKEN_URL     = "https://oauth2.googleapis.com/token"
_CODE_ASSIST_ENDPOINT = "https://cloudcode-pa.googleapis.com"


class _GeminiCallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler that captures the OAuth callback code."""

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/oauth2callback":
            qs = parse_qs(parsed.query)
            code  = (qs.get("code")  or [""])[0]
            state = (qs.get("state") or [""])[0]
            if code:
                self.server._oauth_result = {"code": code, "state": state}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<html><body><h1>Auth OK</h1><p>Close this tab.</p></body></html>")
            else:
                self.server._oauth_result = None
                self.send_response(400)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass  # suppress server log noise


def _start_gemini_callback_server() -> Optional[HTTPServer]:
    """Start an HTTP server on :8085 to catch the OAuth redirect."""
    try:
        server = HTTPServer(("127.0.0.1", 8085), _GeminiCallbackHandler)
        server._oauth_result = None
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        return server
    except Exception as e:
        log.error("Could not start Gemini callback server: %s", e)
        return None


async def gemini_start_oauth() -> tuple[bool, str]:
    """Start Google OAuth flow for Gemini CLI."""
    await gemini_cancel()

    verifier, challenge = _generate_pkce()

    server = _start_gemini_callback_server()

    params = {
        "client_id":             _get_gemini_client_id(),
        "response_type":         "code",
        "redirect_uri":          _GEMINI_REDIRECT,
        "scope":                 _GEMINI_SCOPES,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
        "state":                 verifier,
        "access_type":           "offline",
        "prompt":                "consent",
    }
    url = f"{_GEMINI_AUTH_URL}?{urlencode(params)}"

    _active["gemini"] = {"verifier": verifier, "server": server}
    return True, url


async def gemini_deliver_code(redirect_url_or_code: str) -> tuple[bool, str]:
    """
    Finish Gemini OAuth.
    Accepts either the full redirect URL (http://localhost:8085/oauth2callback?code=...&state=...)
    or just the code alone.
    """
    import httpx

    info = _active.get("gemini")
    if not info:
        return False, "No active Gemini auth session. Use /auth to start a new one."

    verifier = info["verifier"]
    server: Optional[HTTPServer] = info.get("server")

    text = redirect_url_or_code.strip()

    if text.startswith("http"):
        parsed = urlparse(text)
        qs = parse_qs(parsed.query)
        code  = (qs.get("code")  or [""])[0]
        state = (qs.get("state") or [""])[0]
    else:
        code  = text
        state = ""

    if not code:
        return False, "Could not extract authorization code. Paste the full redirect URL."

    # Close callback server
    if server:
        try:
            server.shutdown()
        except Exception:
            pass

    log.info("Gemini token exchange: code=%s…", code[:8])

    # Exchange code for tokens
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(_GEMINI_TOKEN_URL, data={
                "client_id":     _get_gemini_client_id(),
                "client_secret": _get_gemini_client_secret(),
                "code":          code,
                "grant_type":    "authorization_code",
                "redirect_uri":  _GEMINI_REDIRECT,
                "code_verifier": verifier,
            })
    except Exception as e:
        return False, f"HTTP error during token exchange: {e}"

    if not resp.is_success:
        return False, f"Token exchange failed ({resp.status_code}): {resp.text[:300]}"

    token_data = resp.json()
    access_token  = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    expires_in    = token_data.get("expires_in", 3600)

    if not access_token:
        return False, f"No access_token in response: {resp.text[:200]}"

    # Discover Google Cloud project
    project_id = await _gemini_discover_project(access_token)
    if not project_id:
        _active.pop("gemini", None)
        return False, "Authenticated but could not discover Google Cloud project. Set GOOGLE_CLOUD_PROJECT env var."

    _active.pop("gemini", None)

    # Store credentials as JSON (matching pi-ai's getApiKey format)
    creds = json.dumps({"token": access_token, "projectId": project_id})
    set_env_key("GEMINI_OAUTH_CREDS", creds)
    set_env_key("GEMINI_REFRESH_TOKEN", refresh_token)
    set_env_key("GEMINI_PROJECT_ID", project_id)

    # Also set GOOGLE_API_KEY if it looks like an access token (for gemini CLI direct use)
    # Note: gemini CLI uses GEMINI_API_KEY or GOOGLE_API_KEY for simple API key mode
    # For OAuth, credentials go via the credentials file
    _write_gemini_credentials(access_token, refresh_token, expires_in)

    return True, f"✅ Google OAuth OK — project: {project_id}"


async def _gemini_discover_project(access_token: str) -> Optional[str]:
    """Try to discover or provision a Google Cloud project for Gemini CLI."""
    import httpx
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "User-Agent": "google-api-nodejs-client/9.15.1",
        "X-Goog-Api-Client": "gl-node/22.17.0",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{_CODE_ASSIST_ENDPOINT}/v1internal:loadCodeAssist",
                headers=headers,
                json={
                    "metadata": {
                        "ideType": "IDE_UNSPECIFIED",
                        "platform": "PLATFORM_UNSPECIFIED",
                        "pluginType": "GEMINI",
                    }
                }
            )
        if resp.is_success:
            d = resp.json()
            if d.get("cloudaicompanionProject"):
                return d["cloudaicompanionProject"]
            # Need to onboard
            log.info("Gemini: onboarding user...")
            async with httpx.AsyncClient(timeout=60.0) as client:
                ob = await client.post(
                    f"{_CODE_ASSIST_ENDPOINT}/v1internal:onboardUser",
                    headers=headers,
                    json={
                        "tierId": "free-tier",
                        "metadata": {
                            "ideType": "IDE_UNSPECIFIED",
                            "platform": "PLATFORM_UNSPECIFIED",
                            "pluginType": "GEMINI",
                        }
                    }
                )
            if ob.is_success:
                obd = ob.json()
                proj = (obd.get("response") or {}).get("cloudaicompanionProject", {}).get("id")
                if proj:
                    return proj
    except Exception as e:
        log.error("Gemini project discovery failed: %s", e)
    return None


def _write_gemini_credentials(access_token: str, refresh_token: str, expires_in: int):
    """Write credentials to gemini CLI's application_default_credentials.json."""
    import time as _time
    creds_dir = Path("/home/factory/.config/gcloud")
    creds_dir.mkdir(parents=True, exist_ok=True)
    creds_file = creds_dir / "application_default_credentials.json"
    creds = {
        "client_id": _get_gemini_client_id(),
        "client_secret": _get_gemini_client_secret(),
        "refresh_token": refresh_token,
        "type": "authorized_user",
    }
    try:
        creds_file.write_text(json.dumps(creds, indent=2))
        os.chmod(creds_file, 0o600)
        log.info("Wrote Gemini credentials to %s", creds_file)
    except Exception as e:
        log.warning("Could not write Gemini credentials file: %s", e)


async def gemini_cancel():
    info = _active.pop("gemini", None)
    if info:
        server = info.get("server")
        if server:
            try:
                server.shutdown()
            except Exception:
                pass


async def gemini_auth_status() -> tuple[bool, str]:
    creds_raw = get_env_key("GEMINI_OAUTH_CREDS")
    if creds_raw:
        try:
            d = json.loads(creds_raw)
            pid = d.get("projectId", "?")
            return True, f"✅ OAuth credentials set (project: {pid})"
        except Exception:
            pass
    key = get_env_key("GOOGLE_API_KEY") or get_env_key("GEMINI_API_KEY")
    if key and len(key) > 10:
        return True, f"✅ API key set ({_mask(key)})"
    # Check gcloud credentials file
    creds_file = Path("/home/factory/.config/gcloud/application_default_credentials.json")
    if creds_file.exists():
        return True, "✅ gcloud credentials file present"
    return False, "❌ No key / not authenticated"


async def gemini_set_api_key(api_key: str) -> tuple[bool, str]:
    ok = set_env_key("GOOGLE_API_KEY", api_key)
    if not ok:
        return False, "❌ Failed to save key"
    return True, f"✅ GOOGLE_API_KEY saved ({_mask(api_key)})"


# ══════════════════════════════════════════════════════════════════════════════
# OpenCode
# ══════════════════════════════════════════════════════════════════════════════

async def opencode_auth_status() -> tuple[bool, str]:
    for k in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        v = get_env_key(k)
        if v and len(v) > 10:
            return True, f"✅ {k} set ({_mask(v)})"
    return False, "❌ No API key configured"


async def opencode_set_key(provider: str, api_key: str) -> tuple[bool, str]:
    mapping = {
        "openrouter": "OPENROUTER_API_KEY",
        "anthropic":  "ANTHROPIC_API_KEY",
        "openai":     "OPENAI_API_KEY",
    }
    env_key = mapping.get(provider.lower())
    if not env_key:
        return False, f"Unknown provider '{provider}'. Use: openrouter / anthropic / openai"
    ok = set_env_key(env_key, api_key)
    return (True, f"✅ {env_key} saved ({_mask(api_key)})") if ok else (False, "❌ Failed to save")


# ══════════════════════════════════════════════════════════════════════════════
# Aider
# ══════════════════════════════════════════════════════════════════════════════

async def aider_auth_status() -> tuple[bool, str]:
    for k in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        v = get_env_key(k)
        if v and len(v) > 10:
            return True, f"✅ {k} set ({_mask(v)})"
    return False, "❌ No API key configured"


async def aider_set_key(provider: str, api_key: str) -> tuple[bool, str]:
    mapping = {
        "groq":        "GROQ_API_KEY",
        "openrouter":  "OPENROUTER_API_KEY",
        "anthropic":   "ANTHROPIC_API_KEY",
        "openai":      "OPENAI_API_KEY",
    }
    env_key = mapping.get(provider.lower())
    if not env_key:
        return False, f"Unknown provider '{provider}'. Use: groq / openrouter / anthropic / openai"
    ok = set_env_key(env_key, api_key)
    if not ok:
        return False, "❌ Failed to save"
    # Update aider model config
    model_map = {
        "groq":       "groq/llama-3.3-70b-versatile",
        "openrouter": "openrouter/anthropic/claude-3.5-sonnet",
        "anthropic":  "anthropic/claude-3-5-sonnet-20241022",
        "openai":     "gpt-4o",
    }
    conf = Path("/home/factory/.aider.conf.yml")
    try:
        txt = conf.read_text() if conf.exists() else ""
        model_line = f"model: {model_map[provider.lower()]}"
        if "model:" in txt:
            txt = re.sub(r"^model:.*$", model_line, txt, flags=re.MULTILINE)
        else:
            txt += f"\n{model_line}\nyes-always: true\n"
        conf.write_text(txt)
    except Exception:
        pass
    return True, f"✅ {env_key} saved ({_mask(api_key)})"


# ══════════════════════════════════════════════════════════════════════════════
# Pi (uses ANTHROPIC_API_KEY)
# ══════════════════════════════════════════════════════════════════════════════

async def pi_auth_status() -> tuple[bool, str]:
    key = get_env_key("ANTHROPIC_API_KEY")
    if key and len(key) > 10:
        return True, f"✅ ANTHROPIC_API_KEY set ({_mask(key)})"
    r = subprocess.run(['which', PI_BIN], capture_output=True, text=True)
    if r.returncode != 0:
        return False, "❌ pi not installed"
    return False, "⚠️ pi installed, no ANTHROPIC_API_KEY"


async def pi_set_key(api_key: str) -> tuple[bool, str]:
    ok = set_env_key("ANTHROPIC_API_KEY", api_key)
    return (True, f"✅ ANTHROPIC_API_KEY saved ({_mask(api_key)})") if ok else (False, "❌ Failed to save")


# ══════════════════════════════════════════════════════════════════════════════
# OpenAI OAuth  (just shows API keys page — no actual OAuth)
# ══════════════════════════════════════════════════════════════════════════════

async def openai_start_oauth() -> tuple[bool, str]:
    return True, "https://platform.openai.com/api-keys"


async def openai_set_key(api_key: str, target_engine: str = "") -> tuple[bool, str]:
    if not (api_key.startswith("sk-") and len(api_key) > 20):
        return False, "Invalid OpenAI key (must start with sk-...)"
    ok = set_env_key("OPENAI_API_KEY", api_key)
    if not ok:
        return False, "❌ Failed to save"
    notes = [f"✅ OPENAI_API_KEY saved ({_mask(api_key)})"]
    if target_engine in ("aider", ""):
        conf = Path("/home/factory/.aider.conf.yml")
        try:
            txt = conf.read_text() if conf.exists() else ""
            if "model:" in txt:
                txt = re.sub(r"^model:.*$", "model: gpt-4o", txt, flags=re.MULTILINE)
            else:
                txt += "\nmodel: gpt-4o\nyes-always: true\n"
            conf.write_text(txt)
            notes.append("Aider model → gpt-4o")
        except Exception:
            pass
    return True, "\n".join(notes)


async def openai_auth_status() -> tuple[bool, str]:
    key = get_env_key("OPENAI_API_KEY")
    if key and key.startswith("sk-") and len(key) > 20:
        return True, f"✅ OPENAI_API_KEY: {_mask(key)}"
    return False, "❌ No OpenAI key"


# ══════════════════════════════════════════════════════════════════════════════
# Claude API key (direct sk-ant- key, no OAuth)
# ══════════════════════════════════════════════════════════════════════════════

async def claude_set_api_key(api_key: str) -> tuple[bool, str]:
    if not api_key.startswith("sk-ant-"):
        return False, "Must be an Anthropic API key (starts with sk-ant-...)"
    ok = set_env_key("ANTHROPIC_API_KEY", api_key)
    return (True, f"✅ ANTHROPIC_API_KEY saved ({_mask(api_key)})") if ok else (False, "❌ Failed to save")


async def gemini_set_api_key(api_key: str) -> tuple[bool, str]:
    ok = set_env_key("GOOGLE_API_KEY", api_key)
    return (True, f"✅ GOOGLE_API_KEY saved ({_mask(api_key)})") if ok else (False, "❌ Failed to save")


# ══════════════════════════════════════════════════════════════════════════════
# Propagation helpers (called after OAuth completes)
# ══════════════════════════════════════════════════════════════════════════════

async def after_anthropic_oauth(target_engine: str) -> str:
    if target_engine == "claude":
        return ""
    key = get_env_key("ANTHROPIC_API_KEY")
    if key:
        return f"ℹ️ ANTHROPIC_API_KEY also usable by pi / aider / opencode"
    return ""


async def after_google_oauth(target_engine: str) -> str:
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# Aggregate status
# ══════════════════════════════════════════════════════════════════════════════

async def all_status() -> dict[str, tuple[bool, str]]:
    results = await asyncio.gather(
        claude_auth_status(),
        gemini_auth_status(),
        opencode_auth_status(),
        aider_auth_status(),
        pi_auth_status(),
    )
    return dict(zip(["claude", "gemini", "opencode", "aider", "pi"], results))


async def all_status_with_providers() -> dict:
    statuses = await all_status()
    return {k: {"ok": v[0], "msg": v[1]} for k, v in statuses.items()}

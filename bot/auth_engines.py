"""Engine OAuth / API-key authentication management.

Supports:
  • Claude Code — OAuth PKCE  (claude auth login, local HTTP callback)
  • Gemini CLI  — OAuth / GEMINI_API_KEY
  • OpenCode    — OPENROUTER_API_KEY or ANTHROPIC_API_KEY
  • Aider       — GROQ_API_KEY / OPENROUTER_API_KEY / ANTHROPIC_API_KEY
  • pi          — ANTHROPIC_API_KEY

Since the factory-bot process itself runs as the `factory` user (via systemd),
all subprocess calls run directly as that user — no sudo needed.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

ENV_FILE = Path('/opt/factory-bot/.env')
CLAUDE_BIN = '/usr/bin/claude'
GEMINI_BIN = '/usr/bin/gemini'
OPENCODE_BIN = '/usr/local/bin/opencode'
AIDER_BIN = '/usr/local/bin/aider'

# ─── Per-engine active OAuth session (one at a time per engine) ───────────────
# engine_key -> {"proc": Process, "port": int|None, "state": str|None, "url": str}
_active: dict[str, dict] = {}


# ─── .env helpers ─────────────────────────────────────────────────────────────

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
    """Return first 8 chars + '...' or '—'."""
    return f"{key[:8]}..." if key and len(key) > 8 else "—"


# ─── Port scanner (finds the callback HTTP server's ephemeral port) ───────────

def _get_listening_ports() -> set[int]:
    """Return the set of TCP ports currently in LISTEN state."""
    try:
        r = subprocess.run(
            ['ss', '-tlnp', '--no-header'],
            capture_output=True, text=True, timeout=5,
        )
        ports = set()
        for line in r.stdout.splitlines():
            m = re.search(r':(\d{4,5})\s', line)
            if m:
                ports.add(int(m.group(1)))
        return ports
    except Exception:
        return set()


async def _find_new_port(existing: set[int], timeout: float = 8.0) -> Optional[int]:
    """Wait up to *timeout* seconds for a new high port to appear."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.4)
        current = _get_listening_ports()
        new = {p for p in (current - existing) if 1024 < p < 65535}
        if new:
            return min(new)  # take the lowest new port
    return None


# ─── Claude Code ──────────────────────────────────────────────────────────────

async def claude_start_oauth() -> tuple[bool, str]:
    """
    Start 'claude auth login' in the background.
    Returns (ok, message_for_user).
    If ok, message contains the OAuth URL to open.
    Also stores proc/port/state in _active['claude'].
    """
    # Kill any leftover session
    await claude_cancel()

    existing_ports = _get_listening_ports()

    try:
        proc = await asyncio.create_subprocess_exec(
            CLAUDE_BIN, 'auth', 'login',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return False, f"claude not found at {CLAUDE_BIN}"

    # Read stdout to find the OAuth URL (timeout 20 s)
    auth_url: Optional[str] = None
    try:
        async with asyncio.timeout(20):
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode('utf-8', errors='replace').strip()
                log.debug("claude-auth stdout: %s", line)
                m = re.search(r'(https://claude\.ai/oauth/authorize[^\s]+)', line)
                if not m:
                    m = re.search(r'(https://[^\s]*claude\.ai[^\s]+state=[^\s]+)', line)
                if m:
                    auth_url = m.group(1)
                    break
    except (asyncio.TimeoutError, Exception) as e:
        log.error("claude auth: error reading URL: %s", e)

    if not auth_url:
        proc.kill()
        return False, "Failed to capture OAuth URL from claude auth login output."

    state_m = re.search(r'state=([^&\s]+)', auth_url)
    state = state_m.group(1) if state_m else None

    # Discover the callback port
    port = await _find_new_port(existing_ports, timeout=6.0)

    _active['claude'] = {
        'proc': proc,
        'port': port,
        'state': state,
        'url': auth_url,
    }

    port_hint = f" (callback port: {port})" if port else " (port not detected — will try common ports)"
    return True, auth_url


async def claude_deliver_code(code: str) -> tuple[bool, str]:
    """Deliver the auth code to the running claude auth HTTP server."""
    import httpx

    info = _active.get('claude')
    if not info:
        return False, "No active Claude auth session. Use /auth to start a new one."

    port = info.get('port')
    state = info.get('state', '')

    # Try the known port, then scan for any new port
    if not port:
        existing = _get_listening_ports()
        for p in range(40000, 50000):
            if p in existing:
                port = p
                info['port'] = port
                break

    if not port:
        return False, "Could not find Claude auth callback port. Please start over."

    url = f"http://127.0.0.1:{port}/callback?code={code}&state={state}"
    log.info("Delivering Claude code to %s", url)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, follow_redirects=False)
        log.info("Delivery response: %s", resp.status_code)
    except Exception as e:
        return False, f"HTTP delivery failed: {e}"

    # Wait for the process to complete token exchange
    try:
        proc = info['proc']
        await asyncio.wait_for(proc.wait(), timeout=15.0)
    except asyncio.TimeoutError:
        pass  # process might linger

    # Check result
    await asyncio.sleep(1)
    ok, msg = await claude_auth_status()
    _active.pop('claude', None)
    return ok, msg


async def claude_cancel():
    """Kill any running claude auth session."""
    info = _active.pop('claude', None)
    if info:
        proc = info.get('proc')
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass


async def claude_auth_status() -> tuple[bool, str]:
    """Return (logged_in, description) for factory user."""
    try:
        r = subprocess.run(
            [CLAUDE_BIN, 'auth', 'status', '--json'],
            capture_output=True, text=True, timeout=10,
        )
        raw = (r.stdout + r.stderr).strip()
        # Find JSON in output
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith('{'):
                try:
                    d = json.loads(line)
                    logged_in = d.get('loggedIn', False)
                    if logged_in:
                        acc = d.get('oauthAccount', {})
                        email = acc.get('emailAddress', '?')
                        return True, f"✅ Logged in as {email}"
                    return False, "❌ Not logged in"
                except json.JSONDecodeError:
                    pass
        # Fallback: text scan
        if '"loggedIn":true' in raw or 'loggedIn.*true' in raw:
            return True, "✅ Logged in"
        return False, "❌ Not logged in"
    except Exception as e:
        return False, f"❌ Error: {e}"


# ─── Gemini CLI ───────────────────────────────────────────────────────────────

async def gemini_start_oauth() -> tuple[bool, str]:
    """Start 'gemini auth' to get a Google OAuth URL."""
    await gemini_cancel()

    # Try API key first check
    key = get_env_key('GOOGLE_API_KEY') or get_env_key('GEMINI_API_KEY')
    if key and len(key) > 10:
        # Key already set — offer as alternative info but proceed with OAuth
        pass

    existing_ports = _get_listening_ports()

    try:
        proc = await asyncio.create_subprocess_exec(
            GEMINI_BIN, 'auth', 'login',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return False, f"gemini not found at {GEMINI_BIN}"

    auth_url: Optional[str] = None
    try:
        async with asyncio.timeout(20):
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode('utf-8', errors='replace').strip()
                log.debug("gemini-auth stdout: %s", line)
                # Google OAuth URL
                m = re.search(r'(https://accounts\.google\.com[^\s]+)', line)
                if not m:
                    m = re.search(r'(https://[^\s]*google[^\s]+)', line)
                if m:
                    auth_url = m.group(1)
                    break
    except (asyncio.TimeoutError, Exception) as e:
        log.error("gemini auth: %s", e)

    if not auth_url:
        proc.kill()
        return False, (
            "Could not capture Gemini OAuth URL.\n\n"
            "You can also authenticate via API key:\n"
            "Use the 'Set API Key' option."
        )

    state_m = re.search(r'state=([^&\s]+)', auth_url)
    state = state_m.group(1) if state_m else None
    port = await _find_new_port(existing_ports, timeout=6.0)

    _active['gemini'] = {'proc': proc, 'port': port, 'state': state, 'url': auth_url}
    return True, auth_url


async def gemini_deliver_code(code: str) -> tuple[bool, str]:
    """Deliver Google auth code to gemini's callback server."""
    import httpx

    info = _active.get('gemini')
    if not info:
        return False, "No active Gemini auth session."

    port = info.get('port')
    state = info.get('state', '')

    if not port:
        # gemini might accept code via stdin too
        proc = info['proc']
        if proc.returncode is None:
            try:
                proc.stdin.write((code + '\n').encode())
                await proc.stdin.drain()
                await asyncio.wait_for(proc.wait(), timeout=15.0)
                ok, msg = await gemini_auth_status()
                _active.pop('gemini', None)
                return ok, msg
            except Exception as e:
                return False, f"Stdin delivery failed: {e}"
        return False, "No port found and process already exited."

    cb_url = f"http://127.0.0.1:{port}/callback?code={code}"
    if state:
        cb_url += f"&state={state}"

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(cb_url, follow_redirects=False)
    except Exception as e:
        return False, f"HTTP delivery failed: {e}"

    try:
        proc = info['proc']
        await asyncio.wait_for(proc.wait(), timeout=15.0)
    except asyncio.TimeoutError:
        pass

    await asyncio.sleep(1)
    ok, msg = await gemini_auth_status()
    _active.pop('gemini', None)
    return ok, msg


async def gemini_cancel():
    info = _active.pop('gemini', None)
    if info:
        proc = info.get('proc')
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass


async def gemini_auth_status() -> tuple[bool, str]:
    key = get_env_key('GOOGLE_API_KEY') or get_env_key('GEMINI_API_KEY')
    if key and len(key) > 10:
        return True, f"✅ API key: {_mask(key)}"

    try:
        r = subprocess.run(
            [GEMINI_BIN, 'auth', 'status'],
            capture_output=True, text=True, timeout=10,
        )
        out = (r.stdout + r.stderr).lower()
        if 'logged in' in out or 'authenticated' in out:
            return True, "✅ OAuth authenticated"
        if 'not logged' in out or 'not authenticated' in out:
            return False, "❌ Not authenticated"
    except Exception:
        pass

    return False, "❌ No key / not authenticated"


# ─── OpenCode ─────────────────────────────────────────────────────────────────

async def opencode_auth_status() -> tuple[bool, str]:
    # OpenCode uses OPENROUTER_API_KEY or ANTHROPIC_API_KEY
    for k in ('OPENROUTER_API_KEY', 'ANTHROPIC_API_KEY'):
        v = get_env_key(k)
        if v and len(v) > 10:
            return True, f"✅ {k}: {_mask(v)}"
    return False, "❌ No API key configured"


async def opencode_set_key(provider: str, api_key: str) -> tuple[bool, str]:
    key_map = {
        'openrouter': 'OPENROUTER_API_KEY',
        'anthropic': 'ANTHROPIC_API_KEY',
        'openai': 'OPENAI_API_KEY',
    }
    env_key = key_map.get(provider.lower())
    if not env_key:
        return False, f"Unknown provider '{provider}'. Use: openrouter / anthropic / openai"
    ok = set_env_key(env_key, api_key)
    return ok, (f"✅ OpenCode: {env_key} updated" if ok else "❌ Failed to update .env")


# ─── Aider ────────────────────────────────────────────────────────────────────

async def aider_auth_status() -> tuple[bool, str]:
    for k in ('GROQ_API_KEY', 'OPENROUTER_API_KEY', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY'):
        v = get_env_key(k)
        if v and len(v) > 10:
            return True, f"✅ {k}: {_mask(v)}"
    return False, "❌ No API key configured"


async def aider_set_key(provider: str, api_key: str) -> tuple[bool, str]:
    key_map = {
        'groq': 'GROQ_API_KEY',
        'openrouter': 'OPENROUTER_API_KEY',
        'anthropic': 'ANTHROPIC_API_KEY',
        'openai': 'OPENAI_API_KEY',
    }
    env_key = key_map.get(provider.lower())
    if not env_key:
        return False, f"Unknown provider '{provider}'. Use: groq / openrouter / anthropic / openai"

    ok = set_env_key(env_key, api_key)
    if not ok:
        return False, "❌ Failed to update .env"

    # Write ~/.aider.conf.yml for factory user
    model_map = {
        'groq': 'groq/llama-3.3-70b-versatile',
        'openrouter': 'openrouter/anthropic/claude-3.5-sonnet',
        'anthropic': 'anthropic/claude-3-5-sonnet-20241022',
        'openai': 'gpt-4o',
    }
    model = model_map.get(provider.lower(), '')
    conf_path = Path('/home/factory/.aider.conf.yml')
    try:
        conf_path.write_text(f"model: {model}\nyes-always: true\nauto-commits: false\n")
    except Exception as e:
        log.warning("Could not write aider config: %s", e)

    return True, f"✅ Aider: {env_key} updated, model set to {model}"


# ─── pi ───────────────────────────────────────────────────────────────────────

async def pi_auth_status() -> tuple[bool, str]:
    # pi uses ANTHROPIC_API_KEY
    key = get_env_key('ANTHROPIC_API_KEY')
    if key and key.startswith('sk-ant-'):
        return True, f"✅ ANTHROPIC_API_KEY: {_mask(key)}"

    # Check if pi is installed for factory user
    r = subprocess.run(['which', 'pi'], capture_output=True, text=True)
    installed = r.returncode == 0

    if not installed:
        return False, "❌ pi not installed"
    return False, "⚠️ pi installed, no ANTHROPIC_API_KEY"


async def pi_set_key(api_key: str) -> tuple[bool, str]:
    if not api_key.startswith('sk-ant-'):
        return False, "❌ Must be an Anthropic API key (starts with sk-ant-)"

    ok = set_env_key('ANTHROPIC_API_KEY', api_key)
    if not ok:
        return False, "❌ Failed to update .env"

    # Also export in factory user's .bashrc and .profile
    for rc in (Path('/home/factory/.bashrc'), Path('/home/factory/.profile')):
        try:
            txt = rc.read_text() if rc.exists() else ''
            # Remove old ANTHROPIC_API_KEY lines
            txt = re.sub(r'^export ANTHROPIC_API_KEY=.*\n?', '', txt, flags=re.MULTILINE)
            txt += f'\nexport ANTHROPIC_API_KEY={api_key}\n'
            rc.write_text(txt)
        except Exception as e:
            log.warning("Could not update %s: %s", rc, e)

    return True, f"✅ pi: ANTHROPIC_API_KEY updated ({_mask(api_key)})"


# ─── Aggregate status ─────────────────────────────────────────────────────────

async def all_status() -> dict[str, tuple[bool, str]]:
    """Return status for all engines concurrently."""
    results = await asyncio.gather(
        claude_auth_status(),
        gemini_auth_status(),
        opencode_auth_status(),
        aider_auth_status(),
        pi_auth_status(),
        return_exceptions=True,
    )
    keys = ['claude', 'gemini', 'opencode', 'aider', 'pi']
    out = {}
    for k, r in zip(keys, results):
        if isinstance(r, Exception):
            out[k] = (False, f"❌ Error: {r}")
        else:
            out[k] = r
    return out


# ─── Additional API-key setters (called from main.py) ─────────────────────────

async def claude_set_api_key(api_key: str) -> tuple[bool, str]:
    """Set ANTHROPIC_API_KEY for Claude Code (allows headless use without OAuth)."""
    if not api_key.startswith('sk-ant-'):
        return False, "Must be an Anthropic API key (starts with sk-ant-...)"
    ok = set_env_key('ANTHROPIC_API_KEY', api_key)
    if ok:
        # Also write to factory user .claude.json so claude CLI sees it
        import json
        claude_json = Path('/home/factory/.claude.json')
        try:
            data = json.loads(claude_json.read_text()) if claude_json.exists() else {}
            data['apiKey'] = api_key
            claude_json.write_text(json.dumps(data, indent=2))
        except Exception as e:
            log.warning("Could not update .claude.json: %s", e)
    return ok, (f"✅ ANTHROPIC_API_KEY set ({_mask(api_key)})" if ok else "❌ Failed")


async def gemini_set_api_key(api_key: str) -> tuple[bool, str]:
    """Set GOOGLE_API_KEY or GEMINI_API_KEY for Gemini CLI."""
    if api_key.startswith('AIza'):
        ok = set_env_key('GOOGLE_API_KEY', api_key)
        ok2 = set_env_key('GEMINI_API_KEY', api_key)
        return ok and ok2, (f"✅ GOOGLE_API_KEY + GEMINI_API_KEY set ({_mask(api_key)})" if ok else "❌ Failed")
    # Might be a Gemini API key in another format
    ok = set_env_key('GEMINI_API_KEY', api_key)
    return ok, (f"✅ GEMINI_API_KEY set ({_mask(api_key)})" if ok else "❌ Failed")


# ─── OpenAI ──────────────────────────────────────────────────────────────────
# OpenAI doesn't expose a public PKCE endpoint (needs registered client_id),
# so we do a "guided key" flow: send user to platform.openai.com, accept the key.

async def openai_start_oauth() -> tuple[bool, str]:
    """Return the OpenAI API-keys page for the user to open."""
    return True, "https://platform.openai.com/api-keys"


async def openai_set_key(api_key: str, target_engine: str = "") -> tuple[bool, str]:
    """Store an OpenAI API key and configure relevant engines."""
    if not (api_key.startswith("sk-") and len(api_key) > 20):
        return False, "Invalid OpenAI key (must start with sk-...)"
    ok = set_env_key("OPENAI_API_KEY", api_key)
    if not ok:
        return False, "❌ Failed to update .env"
    notes: list[str] = [f"✅ OPENAI_API_KEY saved ({_mask(api_key)})"]
    # Aider: update conf to use openai if requested engine is aider
    if target_engine in ("aider", ""):
        conf = Path("/home/factory/.aider.conf.yml")
        try:
            txt = conf.read_text() if conf.exists() else ""
            txt = re.sub(r"^model:.*$", "model: gpt-4o", txt, flags=re.MULTILINE) \
                  if "model:" in txt else txt + "\nmodel: gpt-4o\nyes-always: true\n"
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


# ─── Propagation helpers ──────────────────────────────────────────────────────

async def after_anthropic_oauth(target_engine: str) -> str:
    """Called after successful Anthropic OAuth.
    Claude Code is now authenticated via OAuth token.
    For other engines, they need a separate sk-ant-* key from console.anthropic.com.
    Returns a status note for the user.
    """
    if target_engine == "claude":
        return ""  # already handled
    key = get_env_key("ANTHROPIC_API_KEY")
    if key and key.startswith("sk-ant-"):
        return f"Other engines can use existing ANTHROPIC_API_KEY ({_mask(key)})."
    return (
        "Claude Code is now authenticated.\n"
        "For Aider / OpenCode / pi to also use Anthropic, set ANTHROPIC_API_KEY "
        "from https://console.anthropic.com/settings/keys"
    )


async def after_google_oauth(target_engine: str) -> str:
    """Called after successful Google OAuth.
    Gemini CLI is authenticated via OAuth credentials.
    Other engines need GOOGLE_API_KEY / GEMINI_API_KEY.
    """
    if target_engine == "gemini":
        return ""
    key = get_env_key("GOOGLE_API_KEY") or get_env_key("GEMINI_API_KEY")
    if key and len(key) > 10:
        return f"Other engines can use existing GOOGLE_API_KEY ({_mask(key)})."
    return (
        "Gemini CLI is now authenticated.\n"
        "For Aider / OpenCode to also use Google, set GOOGLE_API_KEY "
        "from https://aistudio.google.com/app/apikey"
    )


# ─── Extended all_status ──────────────────────────────────────────────────────

async def all_status_with_providers() -> dict:
    """
    Returns:
      {
        "claude": (ok, desc),
        "gemini": (ok, desc),
        "opencode": (ok, desc),
        "aider": (ok, desc),
        "pi": (ok, desc),
        "providers": {
            "anthropic": (ok, desc),
            "google": (ok, desc),
            "openai": (ok, desc),
        }
      }
    """
    base = await all_status()

    ant_ok = (
        (await claude_auth_status())[0]
        or bool(get_env_key("ANTHROPIC_API_KEY"))
    )
    ant_key = get_env_key("ANTHROPIC_API_KEY") or ""
    ant_desc = (
        f"✅ OAuth + API key ({_mask(ant_key)})" if ant_ok and ant_key else
        "✅ OAuth (Claude Code)" if ant_ok else
        "❌ Not configured"
    )

    goo_key = get_env_key("GOOGLE_API_KEY") or get_env_key("GEMINI_API_KEY") or ""
    goo_ok_key = bool(goo_key and len(goo_key) > 10)
    goo_ok_oauth = (await gemini_auth_status())[0]
    goo_ok = goo_ok_key or goo_ok_oauth
    goo_desc = (
        f"✅ API key: {_mask(goo_key)}" if goo_ok_key else
        "✅ OAuth (Gemini CLI)" if goo_ok_oauth else
        "❌ Not configured"
    )

    oai_ok, oai_desc = await openai_auth_status()

    base["providers"] = {
        "anthropic": (ant_ok, ant_desc),
        "google":    (goo_ok, goo_desc),
        "openai":    (oai_ok, oai_desc),
    }
    return base

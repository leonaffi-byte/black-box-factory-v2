#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# Factory Bot — Full Deployment & Health-Check Script
# Checks everything, fixes what it can, reports what it can't.
# Usage: sudo bash deploy.sh
# ============================================================================

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
INSTALL_DIR="/opt/factory-bot"
SERVICE_NAME="factory-bot"
FACTORY_USER="factory"
FACTORY_HOME="/home/factory"
VENV="$INSTALL_DIR/.venv"
ERRORS=0
FIXES=0

ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; ((ERRORS++)); }
fix()  { echo -e "  ${BLUE}→ Fixed:${NC} $1"; ((FIXES++)); }
header() { echo ""; echo -e "${BLUE}[$1]${NC} $2"; }

# ── Root check ──────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo bash deploy.sh"
    exit 1
fi

echo "╔══════════════════════════════════════════════╗"
echo "║   Factory Bot — Deploy & Health Check        ║"
echo "╚══════════════════════════════════════════════╝"

# ── 1. System Dependencies ──────────────────────────────────────────────────
header "1/9" "System dependencies"

PKGS=(python3 python3-venv python3-pip ffmpeg tmux git curl)
MISSING=()
for pkg in "${PKGS[@]}"; do
    if ! dpkg -s "$pkg" &>/dev/null; then
        MISSING+=("$pkg")
    fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    warn "Missing packages: ${MISSING[*]}"
    apt-get update -qq
    apt-get install -y -qq "${MISSING[@]}"
    fix "Installed: ${MISSING[*]}"
else
    ok "All system packages present (${PKGS[*]})"
fi

# Check Python version (need 3.10+)
PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [[ $PY_MAJOR -ge 3 && $PY_MINOR -ge 10 ]]; then
    ok "Python $PY_VERSION"
else
    fail "Python $PY_VERSION is too old (need 3.10+)"
fi

# ── 2. Factory user ────────────────────────────────────────────────────────
header "2/9" "Factory user"

if ! id "$FACTORY_USER" &>/dev/null; then
    warn "User '$FACTORY_USER' missing"
    useradd -m -s /bin/bash "$FACTORY_USER"
    fix "Created user '$FACTORY_USER'"
else
    ok "User '$FACTORY_USER' exists"
fi

# ── 3. Bot files ────────────────────────────────────────────────────────────
header "3/9" "Bot installation"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Check if we have source files to install from
if [[ -d "$SCRIPT_DIR/bot" ]]; then
    # Copy bot files
    mkdir -p "$INSTALL_DIR"
    cp -r "$SCRIPT_DIR"/bot "$INSTALL_DIR/"
    cp -r "$SCRIPT_DIR"/templates "$INSTALL_DIR/" 2>/dev/null || true
    cp "$SCRIPT_DIR"/requirements.txt "$INSTALL_DIR/"
    cp "$SCRIPT_DIR"/factory-bot.service "$INSTALL_DIR/" 2>/dev/null || true
    cp "$SCRIPT_DIR"/.env.example "$INSTALL_DIR/" 2>/dev/null || true
    fix "Copied latest bot files to $INSTALL_DIR"
else
    if [[ -d "$INSTALL_DIR/bot" ]]; then
        ok "Bot files exist at $INSTALL_DIR"
    else
        fail "No bot files found at $INSTALL_DIR and no source to copy from"
    fi
fi

# Verify critical files exist
for f in bot/main.py bot/config.py bot/factory.py bot/auth.py bot/state.py bot/voice.py requirements.txt; do
    if [[ ! -f "$INSTALL_DIR/$f" ]]; then
        fail "Missing: $INSTALL_DIR/$f"
    fi
done
ok "All critical bot files present"

# ── 4. Python virtual environment ──────────────────────────────────────────
header "4/9" "Python environment"

if [[ ! -d "$VENV" ]]; then
    warn "Virtual environment missing"
    python3 -m venv "$VENV"
    fix "Created venv at $VENV"
else
    ok "Virtual environment exists"
fi

# Always upgrade pip and install/update deps
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
ok "Python dependencies installed/updated"

# Verify imports work
IMPORT_CHECK=$("$VENV/bin/python" -c "
import sys
errors = []
try:
    import telegram
except ImportError:
    errors.append('python-telegram-bot')
try:
    import edge_tts
except ImportError:
    errors.append('edge-tts')
try:
    import httpx
except ImportError:
    errors.append('httpx')
try:
    import psutil
except ImportError:
    errors.append('psutil')
try:
    import dotenv
except ImportError:
    errors.append('python-dotenv')
if errors:
    print('MISSING:' + ','.join(errors))
else:
    print('OK')
" 2>&1)

if [[ "$IMPORT_CHECK" == "OK" ]]; then
    ok "All Python imports verified"
else
    fail "Import errors: $IMPORT_CHECK"
fi

# Syntax check all bot modules
SYNTAX_OK=true
for pyfile in "$INSTALL_DIR"/bot/*.py; do
    if ! "$VENV/bin/python" -c "import py_compile; py_compile.compile('$pyfile', doraise=True)" 2>/dev/null; then
        fail "Syntax error in $(basename "$pyfile")"
        SYNTAX_OK=false
    fi
done
if $SYNTAX_OK; then
    ok "All Python files pass syntax check"
fi

# ── 5. Environment file (.env) ─────────────────────────────────────────────
header "5/9" "Environment configuration (.env)"

if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    if [[ -f "$INSTALL_DIR/.env.example" ]]; then
        cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
        chmod 600 "$INSTALL_DIR/.env"
        fix "Created .env from template"
    else
        fail ".env file missing and no template found"
    fi
fi

if [[ -f "$INSTALL_DIR/.env" ]]; then
    # Source .env safely to check values
    ENV_ISSUES=()

    get_env_val() {
        grep -E "^$1=" "$INSTALL_DIR/.env" 2>/dev/null | head -1 | cut -d= -f2-
    }

    TOKEN=$(get_env_val "TELEGRAM_BOT_TOKEN")
    ADMIN_ID=$(get_env_val "ADMIN_TELEGRAM_ID")
    GROQ_KEY=$(get_env_val "GROQ_API_KEY")

    if [[ -z "$TOKEN" ]]; then
        ENV_ISSUES+=("TELEGRAM_BOT_TOKEN is empty")
    else
        ok "TELEGRAM_BOT_TOKEN is set"
    fi

    if [[ -z "$ADMIN_ID" ]]; then
        ENV_ISSUES+=("ADMIN_TELEGRAM_ID is empty")
    elif ! [[ "$ADMIN_ID" =~ ^[0-9]+$ ]]; then
        ENV_ISSUES+=("ADMIN_TELEGRAM_ID must be a number, got: $ADMIN_ID")
    else
        ok "ADMIN_TELEGRAM_ID is set ($ADMIN_ID)"
    fi

    if [[ -z "$GROQ_KEY" ]]; then
        warn "GROQ_API_KEY is empty (voice STT won't work)"
    else
        ok "GROQ_API_KEY is set"
    fi

    # Optional keys — just inform
    for key in OPENAI_API_KEY GOOGLE_API_KEY OPENROUTER_API_KEY GITHUB_TOKEN; do
        val=$(get_env_val "$key")
        if [[ -n "$val" ]]; then
            ok "$key is set"
        else
            warn "$key is empty (optional)"
        fi
    done

    if [[ ${#ENV_ISSUES[@]} -gt 0 ]]; then
        for issue in "${ENV_ISSUES[@]}"; do
            fail "$issue"
        done
        echo ""
        echo -e "  ${YELLOW}Edit .env:${NC} nano $INSTALL_DIR/.env"
    fi

    # Permissions
    PERMS=$(stat -c "%a" "$INSTALL_DIR/.env")
    if [[ "$PERMS" != "600" ]]; then
        chmod 600 "$INSTALL_DIR/.env"
        fix ".env permissions set to 600"
    else
        ok ".env permissions correct (600)"
    fi
fi

# ── 6. Directories & permissions ───────────────────────────────────────────
header "6/9" "Directories & permissions"

for dir in "$FACTORY_HOME/projects" "$FACTORY_HOME/.factory-bot"; do
    if [[ ! -d "$dir" ]]; then
        mkdir -p "$dir"
        fix "Created $dir"
    else
        ok "$dir exists"
    fi
done

chown -R "$FACTORY_USER:$FACTORY_USER" "$FACTORY_HOME"
chown -R "$FACTORY_USER:$FACTORY_USER" "$INSTALL_DIR"
ok "Ownership set to $FACTORY_USER"

# ── 7. Systemd service ─────────────────────────────────────────────────────
header "7/9" "Systemd service"

SERVICE_FILE="/etc/systemd/system/factory-bot.service"

if [[ -f "$INSTALL_DIR/factory-bot.service" ]]; then
    # Always refresh the service file
    cp "$INSTALL_DIR/factory-bot.service" "$SERVICE_FILE"
    systemctl daemon-reload
    fix "Service file installed & daemon reloaded"
elif [[ ! -f "$SERVICE_FILE" ]]; then
    fail "No service file found"
fi

if systemctl is-enabled "$SERVICE_NAME" &>/dev/null; then
    ok "Service is enabled (starts on boot)"
else
    systemctl enable "$SERVICE_NAME"
    fix "Enabled service for auto-start"
fi

# ── 8. Telegram bot token validation ────────────────────────────────────────
header "8/9" "Telegram bot connectivity"

if [[ -n "${TOKEN:-}" ]]; then
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "https://api.telegram.org/bot${TOKEN}/getMe" 2>/dev/null || echo "000")
    if [[ "$HTTP_CODE" == "200" ]]; then
        BOT_NAME=$(curl -s "https://api.telegram.org/bot${TOKEN}/getMe" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['result']['username'])" 2>/dev/null || echo "unknown")
        ok "Bot token valid — @$BOT_NAME"
    elif [[ "$HTTP_CODE" == "401" ]]; then
        fail "Bot token INVALID (401 Unauthorized)"
    elif [[ "$HTTP_CODE" == "000" ]]; then
        warn "Could not reach Telegram API (network issue?)"
    else
        fail "Telegram API returned HTTP $HTTP_CODE"
    fi
else
    warn "Skipping bot validation (token not set)"
fi

# ── 9. AI engine availability ───────────────────────────────────────────────
header "9/9" "AI engine checks"

check_cmd() {
    local name="$1" cmd="$2"
    if command -v "$cmd" &>/dev/null; then
        ok "$name ($cmd) is installed"
        return 0
    else
        warn "$name ($cmd) not found — install it to use this engine"
        return 1
    fi
}

check_cmd "Claude Code" "claude" || true
check_cmd "Gemini CLI" "gemini" || true
check_cmd "OpenCode" "opencode" || true
check_cmd "Aider" "aider" || true

# ── Summary ─────────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════"

if [[ $ERRORS -eq 0 ]]; then
    echo -e "${GREEN}All checks passed!${NC} ($FIXES fixes applied)"
    echo ""

    # Stop if already running, then start fresh
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    sleep 1
    systemctl start "$SERVICE_NAME"
    sleep 2

    if systemctl is-active --quiet "$SERVICE_NAME"; then
        echo -e "${GREEN}✓ factory-bot is running!${NC}"
        echo ""
        echo "  Status:  systemctl status factory-bot"
        echo "  Logs:    journalctl -u factory-bot -f"
        echo "  Stop:    systemctl stop factory-bot"
    else
        echo -e "${RED}✗ Service failed to start. Check logs:${NC}"
        echo "  journalctl -u factory-bot -n 20 --no-pager"
    fi
else
    echo -e "${RED}$ERRORS error(s) found${NC} ($FIXES fixes applied)"
    echo ""
    echo "Fix the errors above, then run this script again."
    echo ""
    if [[ -n "$(get_env_val "TELEGRAM_BOT_TOKEN" 2>/dev/null)" ]] || true; then
        echo -e "  Edit config: ${YELLOW}nano $INSTALL_DIR/.env${NC}"
    fi
fi
echo ""

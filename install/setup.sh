#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════
# BLACK BOX FACTORY v2 — COMPLETE INSTALLER
# ═══════════════════════════════════════════════════════════════════════
#
# Run as ROOT on a fresh Ubuntu 22/24 VPS:
#   bash /tmp/factory-install/install/setup.sh
#
# Prerequisites:
#   1. Clone repo to /tmp/factory-install
#   2. Edit install/.factory-env with your API keys
#
# What this does (in order):
#   [1]  System packages (git, tmux, python3, node22, etc.)
#   [2]  Tailscale (secure SSH from anywhere)
#   [3]  Security hardening (fail2ban, ufw, unattended-upgrades)
#   [4]  Factory user (non-root, needed for autonomous mode)
#   [5]  SSH keys + GitHub connectivity
#   [6]  API keys (validate all of them)
#   [7]  Shell environment (.bashrc, .tmux.conf, aliases)
#   [8]  Git configuration
#   [9]  uv/uvx (for zen MCP server)
#   [10] npm global config
#   [11] Claude Code + Gemini CLI
#   [12] MCP servers (6 for Claude, 5 for Gemini)
#   [13] Factory template (CLAUDE.md, GEMINI.md, slash commands)
#   [14] Helper scripts (new-project, factory-start, backup, health)
#   [15] Directories (projects, knowledge-base, backups)
#   [16] Cron jobs (daily backup, knowledge-base auto-commit)
#   [17] Final validation
#
# Safe to re-run. Takes ~5 minutes on a fresh VPS.
# ═══════════════════════════════════════════════════════════════════════

set -e

# ── Configuration ──
FACTORY_USER="factory"
FACTORY_HOME="/home/$FACTORY_USER"
INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$INSTALL_DIR")"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
pass() { echo -e "  ${GREEN}✅ $1${NC}"; }
fail() { echo -e "  ${RED}❌ $1${NC}"; ERRORS=$((ERRORS+1)); }
warn() { echo -e "  ${YELLOW}⚠️  $1${NC}"; }
ERRORS=0

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  🏭 BLACK BOX FACTORY v2 — INSTALLER"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ── Check prerequisites ──
if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Run as root"
    exit 1
fi

if [ ! -f "$INSTALL_DIR/.factory-env" ]; then
    echo "ERROR: API keys not configured"
    echo "  1. cp $INSTALL_DIR/.factory-env.example $INSTALL_DIR/.factory-env"
    echo "  2. nano $INSTALL_DIR/.factory-env"
    echo "  3. Re-run this script"
    exit 1
fi

source "$INSTALL_DIR/.factory-env"

if [ "$GOOGLE_API_KEY" = "PASTE-YOUR-KEY-HERE" ] || [ "$OPENROUTER_API_KEY" = "PASTE-YOUR-KEY-HERE" ]; then
    echo "ERROR: You haven't filled in your API keys yet"
    echo "  nano $INSTALL_DIR/.factory-env"
    exit 1
fi

# ═══════════════════════════════════════════════════════════
# [1] SYSTEM PACKAGES
# ═══════════════════════════════════════════════════════════
echo "━━━ [1/17] System Packages ━━━"
apt update -qq 2>/dev/null
for pkg in git tmux curl wget unzip build-essential python3 python3-pip python3-venv jq rsync fail2ban ufw unattended-upgrades; do
    dpkg -l | grep -q "ii  $pkg " || apt install -y -qq $pkg 2>/dev/null
done
pass "Base packages installed"

# Node.js 22
if ! node --version 2>/dev/null | grep -q "v22"; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - 2>/dev/null
    apt install -y -qq nodejs 2>/dev/null
fi
pass "Node.js $(node --version)"

# ═══════════════════════════════════════════════════════════
# [2] TAILSCALE
# ═══════════════════════════════════════════════════════════
echo ""
echo "━━━ [2/17] Tailscale ━━━"
if command -v tailscale &>/dev/null; then
    pass "Tailscale already installed"
else
    curl -fsSL https://tailscale.com/install.sh | sh 2>/dev/null
    pass "Tailscale installed"
fi

if tailscale status &>/dev/null; then
    tailscale set --ssh 2>/dev/null
    TS_IP=$(tailscale ip -4 2>/dev/null)
    pass "Tailscale connected: $TS_IP"
    pass "Tailscale SSH enabled"
else
    warn "Tailscale not connected — run: tailscale up"
    warn "After connecting, run: tailscale set --ssh"
fi

# ═══════════════════════════════════════════════════════════
# [3] SECURITY HARDENING
# ═══════════════════════════════════════════════════════════
echo ""
echo "━━━ [3/17] Security ━━━"
systemctl enable fail2ban --now 2>/dev/null
pass "Fail2ban enabled"

ufw allow from 100.64.0.0/10 2>/dev/null  # Tailscale
ufw --force enable 2>/dev/null
pass "UFW enabled (Tailscale only)"

# Enable unattended security updates
dpkg-reconfigure -plow unattended-upgrades 2>/dev/null || true
pass "Unattended upgrades configured"

# ═══════════════════════════════════════════════════════════
# [4] FACTORY USER
# ═══════════════════════════════════════════════════════════
echo ""
echo "━━━ [4/17] Factory User ━━━"
id $FACTORY_USER &>/dev/null || useradd -m -s /bin/bash $FACTORY_USER
echo "factory ALL=(ALL) NOPASSWD: /usr/bin/docker, /usr/bin/docker-compose, /usr/bin/systemctl" > /etc/sudoers.d/factory
chmod 440 /etc/sudoers.d/factory
pass "User '$FACTORY_USER' ready"

# ═══════════════════════════════════════════════════════════
# [5] SSH KEYS + GITHUB
# ═══════════════════════════════════════════════════════════
echo ""
echo "━━━ [5/17] SSH Keys ━━━"
mkdir -p $FACTORY_HOME/.ssh

if [ ! -f $FACTORY_HOME/.ssh/id_ed25519 ]; then
    if [ -f /root/.ssh/id_ed25519 ]; then
        cp /root/.ssh/id_ed25519 $FACTORY_HOME/.ssh/
        cp /root/.ssh/id_ed25519.pub $FACTORY_HOME/.ssh/
        pass "Copied SSH key from root"
    else
        su - $FACTORY_USER -c 'ssh-keygen -t ed25519 -C "factory-vps" -f ~/.ssh/id_ed25519 -N ""' 2>/dev/null
        pass "Generated new SSH key"
        echo ""
        warn "ADD THIS KEY TO GITHUB (github.com/settings/keys):"
        cat $FACTORY_HOME/.ssh/id_ed25519.pub
        echo ""
    fi
fi

cp /root/.ssh/authorized_keys $FACTORY_HOME/.ssh/ 2>/dev/null || true
ssh-keyscan -t ed25519 github.com >> $FACTORY_HOME/.ssh/known_hosts 2>/dev/null
chmod 700 $FACTORY_HOME/.ssh
chmod 600 $FACTORY_HOME/.ssh/* 2>/dev/null || true
chown -R $FACTORY_USER:$FACTORY_USER $FACTORY_HOME/.ssh
pass "SSH configured"

# ═══════════════════════════════════════════════════════════
# [6] API KEYS
# ═══════════════════════════════════════════════════════════
echo ""
echo "━━━ [6/17] API Keys ━━━"
cp "$INSTALL_DIR/.factory-env" $FACTORY_HOME/.factory-env
chown $FACTORY_USER:$FACTORY_USER $FACTORY_HOME/.factory-env
chmod 600 $FACTORY_HOME/.factory-env

# Validate each key
GH_CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: token $GITHUB_TOKEN" https://api.github.com/user)
[ "$GH_CODE" = "200" ] && pass "GitHub token valid" || fail "GitHub token invalid (HTTP $GH_CODE)"

GH_REPO=$(curl -s -H "Authorization: token $GITHUB_TOKEN" -H "Accept: application/vnd.github.v3+json" \
    https://api.github.com/user/repos -d '{"name":"__factory-test__","private":true}' 2>/dev/null)
if echo "$GH_REPO" | grep -q '"full_name"'; then
    pass "GitHub token can create repos"
    curl -s -X DELETE -H "Authorization: token $GITHUB_TOKEN" https://api.github.com/repos/${GITHUB_USER:-leonaffi-byte}/__factory-test__ 2>/dev/null
else
    fail "GitHub token cannot create repos — add Administration: Read+Write"
fi

GOOGLE_CODE=$(curl -s -o /dev/null -w "%{http_code}" "https://generativelanguage.googleapis.com/v1beta/models?key=$GOOGLE_API_KEY")
[ "$GOOGLE_CODE" = "200" ] && pass "Google AI Studio key valid" || fail "Google AI Studio key invalid (HTTP $GOOGLE_CODE)"

OR_CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $OPENROUTER_API_KEY" https://openrouter.ai/api/v1/models)
[ "$OR_CODE" = "200" ] && pass "OpenRouter key valid" || fail "OpenRouter key invalid (HTTP $OR_CODE)"

PPLX_CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $PERPLEXITY_API_KEY" -H "Content-Type: application/json" \
    -d '{"model":"sonar","messages":[{"role":"user","content":"test"}],"max_tokens":1}' https://api.perplexity.ai/chat/completions)
[ "$PPLX_CODE" = "200" ] && pass "Perplexity key valid" || fail "Perplexity key invalid (HTTP $PPLX_CODE)"

# ═══════════════════════════════════════════════════════════
# [7] SHELL ENVIRONMENT
# ═══════════════════════════════════════════════════════════
echo ""
echo "━━━ [7/17] Shell Environment ━━━"
cat > $FACTORY_HOME/.bashrc << 'BASHEOF'
source ~/.factory-env 2>/dev/null
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$HOME/.npm-global/bin:/usr/local/bin:$PATH"

alias p='cd ~/projects'
alias ll='ls -la'
alias factory-health='bash ~/factory-health-check.sh'
alias fsg='~/factory-start-gemini.sh'
alias fsc='~/factory-start-claude.sh'

sync-push() { cd "${1:-.}"; git add -A; git commit -m "${2:-sync: $(date +%Y-%m-%d_%H:%M)}"; git push; }
sync-pull() { cd "${1:-.}"; git pull --rebase; }

proj() {
    [ -z "$1" ] && echo "Usage: proj <name>" && ls ~/projects/ && return
    tmux has-session -t "$1" 2>/dev/null || tmux new-session -d -s "$1" -c ~/projects/"$1"
    tmux attach-session -t "$1"
}

factory-claude() { [ -z "$1" ] && echo "Usage: factory-claude <project>" && return; cd ~/projects/"$1"; claude --dangerously-skip-permissions; }
factory-gemini() { [ -z "$1" ] && echo "Usage: factory-gemini <project>" && return; cd ~/projects/"$1"; gemini --yolo; }
BASHEOF

cat > $FACTORY_HOME/.tmux.conf << 'TMUXEOF'
set -g mouse on
set -g default-terminal "screen-256color"
set -g history-limit 50000
set -g status-style 'bg=#333333 fg=#5eacd3'
set -g status-left '#[fg=green]#S '
set -g status-right '#[fg=yellow]%H:%M'
TMUXEOF

pass "Shell + tmux configured"

# ═══════════════════════════════════════════════════════════
# [8] GIT
# ═══════════════════════════════════════════════════════════
echo ""
echo "━━━ [8/17] Git ━━━"
su - $FACTORY_USER -c 'git config --global user.name "Leo Factory"' 2>/dev/null
su - $FACTORY_USER -c 'git config --global user.email "leonaffi@gmail.com"' 2>/dev/null
su - $FACTORY_USER -c 'git config --global init.defaultBranch main' 2>/dev/null
su - $FACTORY_USER -c 'git config --global push.autoSetupRemote true' 2>/dev/null
su - $FACTORY_USER -c 'git config --global pull.rebase true' 2>/dev/null
pass "Git configured"

# ═══════════════════════════════════════════════════════════
# [9] UV/UVX
# ═══════════════════════════════════════════════════════════
echo ""
echo "━━━ [9/17] UV/UVX ━━━"
if ! su - $FACTORY_USER -c 'which uvx' &>/dev/null; then
    su - $FACTORY_USER -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' 2>&1 | tail -1
fi
pass "uvx installed"

# ═══════════════════════════════════════════════════════════
# [10] NPM
# ═══════════════════════════════════════════════════════════
echo ""
echo "━━━ [10/17] NPM ━━━"
su - $FACTORY_USER -c 'mkdir -p ~/.npm-global && npm config set prefix "~/.npm-global"' 2>/dev/null
pass "npm global prefix set"

# ═══════════════════════════════════════════════════════════
# [11] CLAUDE CODE + GEMINI CLI
# ═══════════════════════════════════════════════════════════
echo ""
echo "━━━ [11/17] AI CLIs ━━━"
command -v claude &>/dev/null || npm install -g @anthropic-ai/claude-code 2>/dev/null
pass "Claude Code $(claude --version 2>/dev/null | head -1 || echo 'installed')"

command -v gemini &>/dev/null || npm install -g @google/gemini-cli 2>/dev/null
pass "Gemini CLI installed"

# ═══════════════════════════════════════════════════════════
# [12] MCP SERVERS
# ═══════════════════════════════════════════════════════════
echo ""
echo "━━━ [12/17] MCP Servers ━━━"

# Claude MCP servers (user scope)
su - $FACTORY_USER << MCPEOF
source ~/.bashrc
source ~/.factory-env
for mcp in zen perplexity context7 github-mcp memory sequential-thinking; do
    claude mcp remove --scope user \$mcp 2>/dev/null || true
done
claude mcp add --scope user zen -e GOOGLE_API_KEY="\$GOOGLE_API_KEY" -e OPENROUTER_API_KEY="\$OPENROUTER_API_KEY" -- sh -c "\$HOME/.local/bin/uvx --from git+https://github.com/BeehiveInnovations/zen-mcp-server.git pal-mcp-server"
claude mcp add --scope user perplexity -e PERPLEXITY_API_KEY="\$PERPLEXITY_API_KEY" -- npx -y @perplexity-ai/mcp-server
claude mcp add --scope user context7 -- npx -y @upstash/context7-mcp
claude mcp add --scope user github-mcp -e GITHUB_PERSONAL_ACCESS_TOKEN="\$GITHUB_TOKEN" -- npx -y @modelcontextprotocol/server-github
claude mcp add --scope user memory -- npx -y @modelcontextprotocol/server-memory
claude mcp add --scope user sequential-thinking -- npx -y @modelcontextprotocol/server-sequential-thinking
MCPEOF
pass "Claude MCP servers (6)"

# Gemini MCP servers (settings.json)
mkdir -p $FACTORY_HOME/.gemini
cat > $FACTORY_HOME/.gemini/settings.json << GEMJSON
{
  "mcpServers": {
    "zen": {
      "command": "sh",
      "args": ["-c", "$FACTORY_HOME/.local/bin/uvx --from git+https://github.com/BeehiveInnovations/zen-mcp-server.git pal-mcp-server"],
      "env": {
        "GOOGLE_API_KEY": "$GOOGLE_API_KEY",
        "OPENROUTER_API_KEY": "$OPENROUTER_API_KEY"
      }
    },
    "perplexity": {
      "command": "npx",
      "args": ["-y", "@perplexity-ai/mcp-server"],
      "env": { "PERPLEXITY_API_KEY": "$PERPLEXITY_API_KEY" }
    },
    "context7": {
      "command": "npx",
      "args": ["-y", "@upstash/context7-mcp"]
    },
    "github-mcp": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "$GITHUB_TOKEN" }
    },
    "memory": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-memory"]
    }
  }
}
GEMJSON
pass "Gemini MCP servers (5)"

# ═══════════════════════════════════════════════════════════
# [13] FACTORY TEMPLATE
# ═══════════════════════════════════════════════════════════
echo ""
echo "━━━ [13/17] Factory Template ━━━"
rm -rf $FACTORY_HOME/factory-template
cp -r "$REPO_DIR/project-template" $FACTORY_HOME/factory-template

# Init as git repo
cd $FACTORY_HOME/factory-template
git init 2>/dev/null
git add -A 2>/dev/null
git commit -m "Factory template v3" 2>/dev/null || true
pass "Factory template installed"

# ═══════════════════════════════════════════════════════════
# [14] HELPER SCRIPTS
# ═══════════════════════════════════════════════════════════
echo ""
echo "━━━ [14/17] Helper Scripts ━━━"

# ── new-project.sh ──
cat > $FACTORY_HOME/new-project.sh << 'NPEOF'
#!/bin/bash
set -e
PROJECT_NAME="$1"
DESCRIPTION="$2"
source ~/.factory-env

if [ -z "$PROJECT_NAME" ]; then
    echo "Usage: ~/new-project.sh <project-name> \"Short description\""
    exit 1
fi
if [ -d "$HOME/projects/$PROJECT_NAME" ]; then
    echo "ERROR: Project $PROJECT_NAME already exists"
    exit 1
fi

echo "=== Creating: $PROJECT_NAME ==="
cp -r ~/factory-template ~/projects/$PROJECT_NAME
cd ~/projects/$PROJECT_NAME
rm -rf .git
git init && git add -A && git commit -m "Initial factory template"

echo "=== Creating GitHub repo ==="
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "Authorization: token $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github.v3+json" https://api.github.com/user/repos \
    -d "{\"name\":\"$PROJECT_NAME\",\"private\":true,\"description\":\"$DESCRIPTION\"}")

if [ "$HTTP_CODE" != "201" ]; then
    echo "WARNING: GitHub returned HTTP $HTTP_CODE (repo may already exist)"
fi
sleep 5

git remote add origin git@github.com:$GITHUB_USER/$PROJECT_NAME.git 2>/dev/null || \
    git remote set-url origin git@github.com:$GITHUB_USER/$PROJECT_NAME.git
git push -u origin main
git checkout -b dev
git push -u origin dev

mkdir -p artifacts/requirements artifacts/reports
[ -n "$DESCRIPTION" ] && echo "$DESCRIPTION" > artifacts/requirements/raw-input.md || \
    echo "Describe your project here, then run /factory" > artifacts/requirements/raw-input.md

cat > artifacts/reports/audit-log.md << AEOF
# Factory Audit Log
## Project Created
- Date: $(date +%Y-%m-%d)
- Project: $PROJECT_NAME
AEOF

git add -A && git commit -m "Add requirements and audit log" && git push

echo ""
echo "=== Done! ==="
echo "  Edit: nano ~/projects/$PROJECT_NAME/artifacts/requirements/raw-input.md"
echo "  Run:  fsg $PROJECT_NAME  (Gemini) or fsc $PROJECT_NAME  (Claude)"
NPEOF

# ── factory-start-gemini.sh ──
cat > $FACTORY_HOME/factory-start-gemini.sh << 'FSGEOF'
#!/bin/bash
PROJECT="$1"
[ -z "$PROJECT" ] && echo "Usage: fsg <project-name>" && ls ~/projects/ && exit 1
[ ! -d "$HOME/projects/$PROJECT" ] && echo "ERROR: Project not found" && exit 1
[ -f "$HOME/projects/$PROJECT/GEMINI.md" ] || cp ~/factory-template/GEMINI.md "$HOME/projects/$PROJECT/GEMINI.md"

echo "Starting Gemini factory: $PROJECT"
tmux kill-session -t "gemini-$PROJECT" 2>/dev/null
tmux new-session -d -s "gemini-$PROJECT" -c "$HOME/projects/$PROJECT" \
    "gemini --yolo -p 'Read GEMINI.md then run the full factory pipeline phases 0-7. Maintain audit log. Use zen MCP for external models. Be fully autonomous.' 2>&1 | tee ~/projects/$PROJECT/artifacts/reports/factory-run.log"

echo "✅ Running in background"
echo "  Monitor: tmux attach -t gemini-$PROJECT"
echo "  Logs:    tail -f ~/projects/$PROJECT/artifacts/reports/factory-run.log"
echo "  Stop:    tmux kill-session -t gemini-$PROJECT"
FSGEOF

# ── factory-start-claude.sh ──
cat > $FACTORY_HOME/factory-start-claude.sh << 'FSCEOF'
#!/bin/bash
PROJECT="$1"
[ -z "$PROJECT" ] && echo "Usage: fsc <project-name>" && ls ~/projects/ && exit 1
[ ! -d "$HOME/projects/$PROJECT" ] && echo "ERROR: Project not found" && exit 1
[ -f "$HOME/projects/$PROJECT/CLAUDE.md" ] || cp ~/factory-template/CLAUDE.md "$HOME/projects/$PROJECT/CLAUDE.md"

echo "Starting Claude factory: $PROJECT"
tmux kill-session -t "claude-$PROJECT" 2>/dev/null
tmux new-session -d -s "claude-$PROJECT" -c "$HOME/projects/$PROJECT" \
    "claude --dangerously-skip-permissions -p 'Read CLAUDE.md then run /factory. Follow all phases 0-7. Maintain audit log. Use zen MCP for external models. Be fully autonomous.' 2>&1 | tee ~/projects/$PROJECT/artifacts/reports/factory-run.log"

echo "✅ Running in background"
echo "  Monitor: tmux attach -t claude-$PROJECT"
echo "  Logs:    tail -f ~/projects/$PROJECT/artifacts/reports/factory-run.log"
echo "  Stop:    tmux kill-session -t claude-$PROJECT"
FSCEOF

# ── factory-health-check.sh ──
cat > $FACTORY_HOME/factory-health-check.sh << 'FHEOF'
#!/bin/bash
echo "=== Factory Health Check ==="
source ~/.factory-env 2>/dev/null
echo ""
echo "API Keys:"
[ -n "$GOOGLE_API_KEY" ] && echo "  ✅ GOOGLE_API_KEY" || echo "  ❌ GOOGLE_API_KEY"
[ -n "$OPENROUTER_API_KEY" ] && echo "  ✅ OPENROUTER_API_KEY" || echo "  ❌ OPENROUTER_API_KEY"
[ -n "$PERPLEXITY_API_KEY" ] && echo "  ✅ PERPLEXITY_API_KEY" || echo "  ❌ PERPLEXITY_API_KEY"
[ -n "$GITHUB_TOKEN" ] && echo "  ✅ GITHUB_TOKEN" || echo "  ❌ GITHUB_TOKEN"
echo ""
echo "Tools:"
claude --version 2>/dev/null | head -1 && echo "  ✅ Claude Code" || echo "  ❌ Claude Code"
gemini --version 2>/dev/null | head -1 && echo "  ✅ Gemini CLI" || echo "  ❌ Gemini CLI"
which uvx &>/dev/null && echo "  ✅ uvx" || echo "  ❌ uvx"
echo ""
echo "GitHub:"
ssh -T git@github.com 2>&1 | head -1
echo ""
echo "Active Factory Runs:"
tmux ls 2>/dev/null || echo "  None"
echo ""
echo "Projects:"
ls ~/projects/ 2>/dev/null || echo "  None"
echo ""
echo "Disk: $(df -h / | tail -1 | awk '{print $4 " free of " $2}')"
FHEOF

# ── backup.sh ──
cat > $FACTORY_HOME/backup.sh << 'BKEOF'
#!/bin/bash
BACKUP_DIR=~/backups/$(date +%Y-%m-%d)
mkdir -p $BACKUP_DIR
cp ~/.factory-env $BACKUP_DIR/
cp -r ~/knowledge-base $BACKUP_DIR/ 2>/dev/null
for proj in ~/projects/*/; do
    name=$(basename "$proj")
    mkdir -p $BACKUP_DIR/projects/$name
    cp "$proj/CLAUDE.md" $BACKUP_DIR/projects/$name/ 2>/dev/null
    cp "$proj/GEMINI.md" $BACKUP_DIR/projects/$name/ 2>/dev/null
    cp -r "$proj/artifacts/requirements" $BACKUP_DIR/projects/$name/ 2>/dev/null
    cp -r "$proj/artifacts/architecture" $BACKUP_DIR/projects/$name/ 2>/dev/null
    cp -r "$proj/artifacts/reports" $BACKUP_DIR/projects/$name/ 2>/dev/null
done
find ~/backups -maxdepth 1 -mtime +7 -exec rm -rf {} \;
echo "Backup: $BACKUP_DIR"
BKEOF

chmod +x $FACTORY_HOME/{new-project,factory-start-gemini,factory-start-claude,factory-health-check,backup}.sh
pass "All helper scripts installed"

# ═══════════════════════════════════════════════════════════
# [15] DIRECTORIES
# ═══════════════════════════════════════════════════════════
echo ""
echo "━━━ [15/17] Directories ━━━"
mkdir -p $FACTORY_HOME/{projects,backups}
mkdir -p $FACTORY_HOME/knowledge-base/{research,patterns,decisions}

[ -f $FACTORY_HOME/knowledge-base/project-index.md ] || cat > $FACTORY_HOME/knowledge-base/project-index.md << 'KBEOF'
# Project Index
| Project | Status | Created | Tech Stack |
|---------|--------|---------|------------|
KBEOF

cd $FACTORY_HOME/knowledge-base
[ -d .git ] || (git init && git add -A && git commit -m "Init knowledge base") 2>/dev/null

pass "Directories created"

# ═══════════════════════════════════════════════════════════
# [16] CRON JOBS
# ═══════════════════════════════════════════════════════════
echo ""
echo "━━━ [16/17] Cron Jobs ━━━"
echo "0 2 * * * $FACTORY_HOME/backup.sh >> $FACTORY_HOME/backups/cron.log 2>&1" > /tmp/factory-cron
echo "30 2 * * * cd $FACTORY_HOME/knowledge-base && git add -A && git commit -m 'auto-update' 2>/dev/null || true" >> /tmp/factory-cron
su - $FACTORY_USER -c 'crontab /tmp/factory-cron'
rm /tmp/factory-cron
pass "Daily backup + knowledge-base cron"

# ═══════════════════════════════════════════════════════════
# [17] FINAL OWNERSHIP + VALIDATION
# ═══════════════════════════════════════════════════════════
echo ""
echo "━━━ [17/17] Final Validation ━━━"
chown -R $FACTORY_USER:$FACTORY_USER $FACTORY_HOME
chmod 600 $FACTORY_HOME/.factory-env

# Test GitHub SSH
GH_SSH=$(su - $FACTORY_USER -c 'ssh -T git@github.com 2>&1 || true')
echo "$GH_SSH" | grep -q "successfully authenticated" && pass "GitHub SSH works" || warn "GitHub SSH — may need to add key"

# Summary
TS_IP=$(tailscale ip -4 2>/dev/null || echo "not-connected")

echo ""
echo "═══════════════════════════════════════════════════════════"
if [ $ERRORS -eq 0 ]; then
    echo -e "  ${GREEN}✅ INSTALLATION COMPLETE — 0 errors${NC}"
else
    echo -e "  ${RED}⚠️  INSTALLATION COMPLETE — $ERRORS issues (see above)${NC}"
fi
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "  Connect:  ssh factory@$TS_IP"
echo ""
echo "  Create project:"
echo "    ~/new-project.sh \"my-app\" \"description\""
echo "    nano ~/projects/my-app/artifacts/requirements/raw-input.md"
echo ""
echo "  Start factory:"
echo "    fsg my-app    (Gemini — cheaper)"
echo "    fsc my-app    (Claude — stronger)"
echo ""
echo "  Health check:  factory-health"
echo ""
echo "  ⚠️  IMPORTANT: Log in to Claude Code and Gemini CLI first:"
echo "    su - factory"
echo "    claude        (authenticate, then quit)"
echo "    gemini        (authenticate, then quit)"
echo ""

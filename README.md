# 🏭 Black Box Software Factory v2

A multi-model AI software factory that builds complete projects autonomously using Claude Code + Gemini CLI as dual orchestrators, with cross-provider verification via 7+ AI providers.

## What It Does

Give it a project description → walk away → come back to a production-ready app with:
- Structured requirements (spec.md)
- Multi-model architecture brainstorm
- Full backend + frontend implementation
- Black-box test suite (written by a different AI that never sees the code)
- Cross-provider code review + security audit
- Complete deployment guide + deploy script
- Full audit log of every model used, every decision, every cost

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  YOU: "Build me an expense tracker"                         │
│                                                             │
│  ┌──────────────────┐     ┌──────────────────┐              │
│  │ CLAUDE FACTORY   │ OR  │ GEMINI FACTORY   │              │
│  │ claude --skip-p   │     │ gemini --yolo    │              │
│  │ Opus 4.6 builds  │     │ Gemini 3 Pro     │              │
│  │ $0 (Max sub)     │     │ $0 (free/Pro)    │              │
│  └────────┬─────────┘     └────────┬─────────┘              │
│           │                        │                         │
│           └────────┬───────────────┘                         │
│                    ▼                                         │
│  ┌─────────────────────────────────┐                        │
│  │ ZEN MCP → OpenRouter/Google API │                        │
│  │ Cross-Provider Review:          │                        │
│  │  • GPT-5.2 (testing)            │                        │
│  │  • O3 (code review)             │                        │
│  │  • Claude/Gemini (security)     │                        │
│  └─────────────────────────────────┘                        │
│                                                             │
│  Output: Full-stack app + tests + docs + deploy script      │
└─────────────────────────────────────────────────────────────┘
```

## Prerequisites

- A VPS (Ubuntu 22/24, 2GB+ RAM, 20GB+ disk) — e.g., Hostinger, Hetzner, DigitalOcean
- Tailscale account (free) — for secure SSH from anywhere
- GitHub account with SSH key
- API keys (see below)

### Required API Keys

| Key | Where to Get | Used For | Cost |
|-----|-------------|----------|------|
| Anthropic (Max subscription) | claude.ai | Claude Code orchestrator | $200/mo (or $20 Pro) |
| Google AI Studio | aistudio.google.com | Gemini orchestrator + zen MCP | Free (Pro: $20/mo) |
| OpenRouter | openrouter.ai/settings/keys | GPT, O3, Qwen, GLM via zen MCP | Pay-per-use (~$5-50/project) |
| Perplexity | perplexity.ai/settings/api | Sourced research | Pay-per-use (~$0.50/project) |
| GitHub (fine-grained PAT) | github.com/settings/tokens | Repo creation + management | Free |
| Telegram Bot (optional) | @BotFather on Telegram | Phone control | Free |

### GitHub Token Permissions
When creating your fine-grained token, enable:
- **Administration**: Read and Write (repo creation)
- **Contents**: Read and Write
- **Issues**: Read and Write
- **Pull requests**: Read and Write
- **Metadata**: Read

## Installation

```bash
# 1. SSH into your fresh VPS as root
ssh root@your-vps-ip

# 2. Clone this repo
git clone https://github.com/leonaffi-byte/black-box-factory-v2.git /tmp/factory-install

# 3. Edit your API keys
cp /tmp/factory-install/install/.factory-env.example /tmp/factory-install/install/.factory-env
nano /tmp/factory-install/install/.factory-env

# 4. Run the installer (takes ~5 minutes)
bash /tmp/factory-install/install/setup.sh

# 5. Done! Connect as factory user from now on
ssh factory@your-vps-tailscale-ip
```

## Usage

### Create a new project
```bash
ssh factory@100.107.37.108
~/new-project.sh "my-app" "Build an expense tracker with charts"
nano ~/projects/my-app/artifacts/requirements/raw-input.md
```

### Start the factory (autonomous, go to sleep)
```bash
# Using Claude (best for complex projects)
fsc my-app

# Using Gemini (best for simple/medium, cheaper)
fsg my-app
```

### Monitor progress
```bash
tmux attach -t claude-my-app    # or gemini-my-app
# Detach: Ctrl+B then D

tail -f ~/projects/my-app/artifacts/reports/factory-run.log
```

### From Windows
```
newp.bat "my-app" "description" C:\path\to\requirements.txt
```

### Health check
```bash
factory-health
```

## Project Structure (what gets created)

```
my-app/
├── CLAUDE.md                    # Claude orchestrator instructions
├── GEMINI.md                    # Gemini orchestrator instructions
├── .claude/commands/            # Claude slash commands
├── .gemini/commands/            # Gemini slash commands (.toml)
├── artifacts/
│   ├── requirements/
│   │   ├── raw-input.md         # Your initial description
│   │   └── spec.md              # Structured spec (Phase 1)
│   ├── architecture/
│   │   ├── brainstorm.md        # Multi-model brainstorm (Phase 2)
│   │   ├── design.md            # System architecture (Phase 3)
│   │   └── interfaces.md        # API contract
│   ├── code/
│   │   ├── backend/             # Backend code (Phase 4)
│   │   └── frontend/            # Frontend code (Phase 4)
│   ├── tests/                   # Black-box tests (Phase 4)
│   ├── reviews/
│   │   ├── code-review.md       # Cross-provider review (Phase 5)
│   │   └── security-audit.md    # Security audit (Phase 5)
│   ├── reports/
│   │   ├── audit-log.md         # Full audit trail
│   │   └── factory-run.log      # Console output log
│   ├── docs/
│   │   ├── README.md            # Generated docs (Phase 7)
│   │   ├── CHANGELOG.md
│   │   └── DEPLOYMENT.md        # Step-by-step deploy guide
│   └── release/
│       └── deploy.sh            # One-command deployment
└── config/
    ├── models.yaml
    └── cost-limits.yaml
```

## Cost Estimates

| Factory | Simple Project | Medium Project | Complex Project |
|---------|---------------|----------------|-----------------|
| Claude  | $5-15         | $20-50         | $40-80          |
| Gemini  | $3-8          | $10-25         | $20-45          |

Claude Code (Max sub) and Gemini CLI (free/Pro) costs are not included — those are flat subscriptions.

## File Reference

| File | Purpose |
|------|---------|
| `install/setup.sh` | One-command VPS installer |
| `install/.factory-env.example` | API key template |
| `install/newp.bat` | Windows project creation script |
| `project-template/` | Clean template copied for each new project |
| `project-template/CLAUDE.md` | Claude orchestrator instructions |
| `project-template/GEMINI.md` | Gemini orchestrator instructions |

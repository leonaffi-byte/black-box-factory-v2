"""Environment configuration loader."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)

# Required
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_TELEGRAM_ID = int(os.environ["ADMIN_TELEGRAM_ID"])
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

# Optional
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

# Deployment
DEPLOY_SERVER = os.environ.get("DEPLOY_SERVER", "")        # e.g. "root@100.64.0.5"
DEPLOY_DOMAIN = os.environ.get("DEPLOY_DOMAIN", "")        # e.g. "example.com"

# Paths
FACTORY_ROOT = Path(os.environ.get("FACTORY_ROOT", "/home/factory/projects"))
STATE_DIR = Path(os.environ.get("STATE_DIR", str(Path.home() / ".factory-bot")))
TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

# Ensure dirs exist
FACTORY_ROOT.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)

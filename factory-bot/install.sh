#!/usr/bin/env bash
set -euo pipefail

# Factory Control Bot — One-command installer
# Usage: bash install.sh

INSTALL_DIR="/opt/factory-bot"
SERVICE_NAME="factory-bot"
FACTORY_USER="factory"

echo "=== Factory Control Bot Installer ==="
echo ""

# Check if running as root
if [[ $EUID -ne 0 ]]; then
    echo "This script must be run as root (sudo bash install.sh)"
    exit 1
fi

# 1. System dependencies
echo "[1/7] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ffmpeg tmux git curl

# 2. Create factory user if needed
if ! id "$FACTORY_USER" &>/dev/null; then
    echo "[2/7] Creating factory user..."
    useradd -m -s /bin/bash "$FACTORY_USER"
else
    echo "[2/7] Factory user already exists."
fi

# 3. Copy bot files
echo "[3/7] Installing bot to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp -r "$(dirname "$0")"/* "$INSTALL_DIR/"
cp -r "$(dirname "$0")"/.env.example "$INSTALL_DIR/" 2>/dev/null || true

# 4. Create Python venv and install deps
echo "[4/7] Setting up Python environment..."
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# 5. Setup .env file
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    echo "[5/7] Creating .env file from template..."
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    chmod 600 "$INSTALL_DIR/.env"
    echo ""
    echo "IMPORTANT: Edit $INSTALL_DIR/.env with your API keys before starting!"
    echo ""
else
    echo "[5/7] .env file already exists, skipping."
fi

# 6. Create project directories
echo "[6/7] Creating directories..."
mkdir -p /home/factory/projects
mkdir -p /home/factory/.factory-bot
chown -R "$FACTORY_USER:$FACTORY_USER" /home/factory
chown -R "$FACTORY_USER:$FACTORY_USER" "$INSTALL_DIR"

# 7. Install systemd service
echo "[7/7] Installing systemd service..."
cp "$INSTALL_DIR/factory-bot.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo ""
echo "=== Installation complete! ==="
echo ""
echo "Next steps:"
echo "  1. Edit API keys:  sudo nano $INSTALL_DIR/.env"
echo "  2. Start the bot:  sudo systemctl start $SERVICE_NAME"
echo "  3. Check status:   sudo systemctl status $SERVICE_NAME"
echo "  4. View logs:      sudo journalctl -u $SERVICE_NAME -f"
echo ""
echo "To run manually (for testing):"
echo "  cd $INSTALL_DIR && .venv/bin/python -m bot.main"

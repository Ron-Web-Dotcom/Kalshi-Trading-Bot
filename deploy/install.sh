#!/usr/bin/env bash
# ============================================================
# Kalshi AI Trading Bot — VPS Install Script
# Run as root or with sudo on Ubuntu 22.04 / Debian 12
# Usage: bash deploy/install.sh
# ============================================================
set -euo pipefail

BOT_USER="kalshi"
BOT_DIR="/opt/kalshi-bot"
PYTHON="python3"
PIP="pip3"

echo "======================================================"
echo " Kalshi AI Trading Bot — VPS Installer"
echo "======================================================"

# 1. System packages
echo "[1/7] Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl

# 2. Create dedicated system user (no login shell)
echo "[2/7] Creating bot user '$BOT_USER'..."
id "$BOT_USER" &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin "$BOT_USER"

# 3. Clone / update repository
echo "[3/7] Deploying code to $BOT_DIR..."
if [ -d "$BOT_DIR/.git" ]; then
    git -C "$BOT_DIR" pull --ff-only
else
    mkdir -p "$BOT_DIR"
    cp -r . "$BOT_DIR/"
fi
chown -R "$BOT_USER:$BOT_USER" "$BOT_DIR"

# 4. Python virtual environment
echo "[4/7] Creating Python virtual environment..."
sudo -u "$BOT_USER" $PYTHON -m venv "$BOT_DIR/.venv"
sudo -u "$BOT_USER" "$BOT_DIR/.venv/bin/pip" install --upgrade pip -q
sudo -u "$BOT_USER" "$BOT_DIR/.venv/bin/pip" install -r "$BOT_DIR/requirements.txt" -q

# 5. Environment file
echo "[5/7] Checking .env file..."
if [ ! -f "$BOT_DIR/.env" ]; then
    cp "$BOT_DIR/env.template" "$BOT_DIR/.env"
    chown "$BOT_USER:$BOT_USER" "$BOT_DIR/.env"
    chmod 600 "$BOT_DIR/.env"
    echo ""
    echo "  ⚠️  Created $BOT_DIR/.env from template."
    echo "  ⚠️  Edit it before starting the bot:"
    echo "      nano $BOT_DIR/.env"
    echo ""
fi

# 6. Log and data directories
echo "[6/7] Creating logs and data directories..."
mkdir -p "$BOT_DIR/logs"
chown -R "$BOT_USER:$BOT_USER" "$BOT_DIR/logs"

# 7. Systemd service
echo "[7/7] Installing systemd service..."
cp "$BOT_DIR/deploy/kalshi-bot.service" /etc/systemd/system/kalshi-bot.service
systemctl daemon-reload
systemctl enable kalshi-bot

echo ""
echo "======================================================"
echo " Installation complete!"
echo "======================================================"
echo ""
echo " Next steps:"
echo "   1. Edit your config:  nano $BOT_DIR/.env"
echo "   2. Start the bot:     systemctl start kalshi-bot"
echo "   3. Check status:      systemctl status kalshi-bot"
echo "   4. View live logs:    journalctl -u kalshi-bot -f"
echo ""
echo " To run manually (for testing):"
echo "   sudo -u $BOT_USER $BOT_DIR/.venv/bin/python $BOT_DIR/bot.py"
echo ""

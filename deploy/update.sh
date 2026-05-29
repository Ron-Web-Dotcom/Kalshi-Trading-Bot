#!/usr/bin/env bash
# ============================================================
# Kalshi AI Trading Bot — Zero-downtime update script
# Run from the VPS as root: bash deploy/update.sh
# ============================================================
set -euo pipefail

BOT_DIR="/opt/kalshi-bot"
BOT_USER="kalshi"

echo "Stopping bot..."
systemctl stop kalshi-bot

echo "Pulling latest code..."
git -C "$BOT_DIR" pull --ff-only

echo "Updating dependencies..."
sudo -u "$BOT_USER" "$BOT_DIR/.venv/bin/pip" install -r "$BOT_DIR/requirements.txt" -q

echo "Reloading systemd and starting bot..."
systemctl daemon-reload
systemctl start kalshi-bot

echo ""
echo "Update complete. Status:"
systemctl status kalshi-bot --no-pager -l

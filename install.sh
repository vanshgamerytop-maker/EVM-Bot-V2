#!/usr/bin/env bash
set -euo pipefail

# One-run dependency installer for Ubuntu 22.04/24.04 and Debian-based VPS hosts.
if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash install.sh"
  exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip ca-certificates curl git sqlite3

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip wheel
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo
  echo "Created $APP_DIR/.env. Edit it and add DISCORD_TOKEN and OWNER_ID before starting the bot."
else
  echo "Existing .env preserved."
fi

python3 -m py_compile "$APP_DIR/bot.py"
echo
echo "Dependencies installed and bot.py syntax check passed."
echo "Next: nano '$APP_DIR/.env'"
echo "Then: '$APP_DIR/.venv/bin/python' '$APP_DIR/bot.py'"
echo
echo "LXD image default: ubuntu:24.04"
echo "LXD provisioning stays disabled until you set ENABLE_LXD_PROVISIONING=true after checking networking, quotas, and firewall."

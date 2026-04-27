#!/usr/bin/env bash
# MoneyGlitch installer for Ubuntu 24.x
# Run from the repo root: sudo bash deploy/install.sh
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "Run as root: sudo bash deploy/install.sh" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR=/opt/moneyglitch
DATA_DIR=/var/lib/moneyglitch
SVC_USER=moneyglitch

echo "==> Installing system packages"
apt-get update -y
apt-get install -y python3 python3-venv python3-pip ca-certificates

echo "==> Creating service user"
if ! id -u "$SVC_USER" >/dev/null 2>&1; then
  useradd -r -s /usr/sbin/nologin -d "$DATA_DIR" -m "$SVC_USER"
fi

echo "==> Syncing application to $APP_DIR"
mkdir -p "$APP_DIR" "$DATA_DIR"
rm -rf "$APP_DIR/moneyglitch"
cp -r "$REPO_ROOT/moneyglitch" "$APP_DIR/moneyglitch"
cp "$REPO_ROOT/run_parser.py" "$APP_DIR/run_parser.py"
cp "$REPO_ROOT/run_bot.py"    "$APP_DIR/run_bot.py"
cp "$REPO_ROOT/requirements.txt" "$APP_DIR/requirements.txt"

echo "==> Provisioning Python venv"
if [ ! -d "$APP_DIR/.venv" ]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> Seeding config"
if [ ! -f "$DATA_DIR/config.json" ]; then
  cp "$REPO_ROOT/config.example.json" "$DATA_DIR/config.json"
  chmod 600 "$DATA_DIR/config.json"
  echo "    created $DATA_DIR/config.json — fill in api_id, api_hash, mexc keys, bot token, user_id"
fi

chown -R "$SVC_USER:$SVC_USER" "$APP_DIR" "$DATA_DIR"
chmod 700 "$DATA_DIR"

echo "==> Installing systemd units"
install -m 0644 "$REPO_ROOT/deploy/moneyglitch-parser.service" /etc/systemd/system/moneyglitch-parser.service
install -m 0644 "$REPO_ROOT/deploy/moneyglitch-bot.service"    /etc/systemd/system/moneyglitch-bot.service
systemctl daemon-reload

cat <<EOF

==> Install complete.

Next steps:
  1. Edit secrets:                   nano $DATA_DIR/config.json
  2. Authenticate Telethon ONCE (interactive, asks for phone + code):
       sudo -u $SVC_USER \\
         MONEYGLITCH_CONFIG=$DATA_DIR/config.json \\
         MONEYGLITCH_STATE=$DATA_DIR/state.json \\
         $APP_DIR/.venv/bin/python $APP_DIR/run_parser.py
     Press Ctrl+C after you see "parser connected".
  3. Enable and start services:
       systemctl enable --now moneyglitch-bot.service moneyglitch-parser.service
  4. Tail logs:
       journalctl -u moneyglitch-parser -f
       journalctl -u moneyglitch-bot -f
EOF

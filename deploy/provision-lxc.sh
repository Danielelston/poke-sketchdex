#!/usr/bin/env bash
# Provision PokeSketchDex inside a fresh Debian 12 LXC (run as root in the container).
# Idempotent-ish: safe to re-run to update code + restart.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Danielelston/poke-sketchdex.git}"
APP_DIR="/opt/pokesketch"
SVC_USER="pokesketch"

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git

echo "==> Creating service user"
if ! id "$SVC_USER" &>/dev/null; then
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SVC_USER"
fi

echo "==> Cloning / updating repo"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi

echo "==> Python venv + deps"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR"

echo "==> Data dir + .env"
mkdir -p "$APP_DIR/data"
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo "  !! Edit $APP_DIR/.env and set POKESKETCH_DISCORD_TOKEN, then re-run or start the service."
fi
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR"

echo "==> Installing systemd unit"
cp "$APP_DIR/deploy/pokesketch.service" /etc/systemd/system/pokesketch.service
systemctl daemon-reload
systemctl enable pokesketch.service

if grep -q '^POKESKETCH_DISCORD_TOKEN=.\+' "$APP_DIR/.env"; then
  systemctl restart pokesketch.service
  echo "==> Started. Logs: journalctl -u pokesketch -f"
else
  echo "==> Not started: set the token in $APP_DIR/.env then: systemctl start pokesketch"
fi

#!/usr/bin/env bash
# Provision PokeSketchDex inside a fresh Debian 12 LXC (run as root in the container).
# Idempotent-ish: safe to re-run to update code + restart.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Danielelston/poke-sketchdex.git}"
APP_DIR="/opt/pokesketch"
SVC_USER="pokesketch"

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git sqlite3 rsync openssh-client

echo "==> Creating service user"
if ! id "$SVC_USER" &>/dev/null; then
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SVC_USER"
fi

echo "==> Cloning / updating repo"
if [ -d "$APP_DIR/.git" ]; then
  bash "$APP_DIR/deploy/update-checkout.sh" "$APP_DIR"
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
# Units run deployment scripts as root. Keep the checkout and executables
# root-owned; only runtime data and the token file belong to the bot user.
chown -R root:root "$APP_DIR"
chown -R "$SVC_USER:$SVC_USER" "$APP_DIR/data"
chown "$SVC_USER:$SVC_USER" "$APP_DIR/.env"

# Every backup job must load an explicit host-only policy. New installs are
# local-only until the operator configures an off-host destination.
if [ ! -e /etc/pokesketch-backup.env ]; then
  install -o root -g root -m 600 /dev/null /etc/pokesketch-backup.env
  printf 'BACKUP_REQUIRE_REMOTE=0\n' > /etc/pokesketch-backup.env
  echo "  !! Backups are local-only until /etc/pokesketch-backup.env is configured."
fi
chmod 600 /etc/pokesketch-backup.env

echo "==> Installing systemd units and timers"
for unit in pokesketch.service pokesketch-backup.service pokesketch-backup.timer \
            pokesketch-healthcheck.service pokesketch-healthcheck.timer; do
  cp "$APP_DIR/deploy/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable pokesketch.service

if grep -q '^POKESKETCH_DISCORD_TOKEN=.\+' "$APP_DIR/.env"; then
  systemctl restart pokesketch.service
  echo "==> Started. Logs: journalctl -u pokesketch -f"
else
  echo "==> Not started: set the token in $APP_DIR/.env then: systemctl start pokesketch"
fi
systemctl enable --now pokesketch-backup.timer pokesketch-healthcheck.timer

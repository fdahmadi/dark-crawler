#!/usr/bin/env bash
# Copy this repo to the VPS and restart the existing systemd stack.
# The server does not use run.sh: run.sh is the local/Docker launcher.
# On the VPS, systemd already starts the same Python crawler plus Tor and the web UI.
#
# Usage:
#   ./deploy.sh
#   ./deploy.sh root@104.194.142.112
set -euo pipefail

cd "$(dirname "$0")"

HOST="${1:-${DEPLOY_HOST:-root@104.194.142.112}}"
DEST="/opt/dark-crawler"
SSH=(ssh -o BatchMode=yes)

echo "==> Syncing to ${HOST}:${DEST}"
rsync -az --delete \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude 'data/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude 'config.yaml' \
  --exclude 'deploy/web.env' \
  -e "ssh -o BatchMode=yes" \
  ./ "${HOST}:${DEST}/"

echo "==> Updating services on the VPS"
"${SSH[@]}" "$HOST" "bash -s" <<'EOF'
set -euo pipefail

APP_DIR="/opt/dark-crawler"
cd "$APP_DIR"

if [[ ! -x "$APP_DIR/.venv/bin/python" ]] || [[ ! -f /etc/systemd/system/dark-crawler-web.service ]]; then
  echo "==> First install"
  bash "$APP_DIR/deploy/install.sh"
  exit 0
fi

echo "==> Permissions"
chown -R darkcrawler:darkcrawler "$APP_DIR"
chmod 755 /opt "$APP_DIR" "$APP_DIR/deploy"
chmod 644 "$APP_DIR/deploy/torrc"
if [[ -f "$APP_DIR/deploy/web.env" ]]; then
  chmod 600 "$APP_DIR/deploy/web.env"
fi
if [[ ! -f "$APP_DIR/config.yaml" ]]; then
  cp -f "$APP_DIR/deploy/config.server.yaml" "$APP_DIR/config.yaml"
  chown darkcrawler:darkcrawler "$APP_DIR/config.yaml"
fi

echo "==> Python packages"
sudo -u darkcrawler "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> systemd units"
cp -f "$APP_DIR/deploy/sudoers-darkcrawler" /etc/sudoers.d/darkcrawler
chmod 440 /etc/sudoers.d/darkcrawler
visudo -cf /etc/sudoers.d/darkcrawler
cp -f "$APP_DIR/deploy/dark-crawler-tor.service" /etc/systemd/system/
cp -f "$APP_DIR/deploy/dark-crawler.service" /etc/systemd/system/
cp -f "$APP_DIR/deploy/dark-crawler-web.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable dark-crawler-tor.service dark-crawler.service dark-crawler-web.service

echo "==> Restarting Tor"
systemctl restart dark-crawler-tor.service
for _ in $(seq 1 60); do
  if ss -ltn | grep -q '127.0.0.1:19050'; then
    echo "SOCKS is up on 127.0.0.1:19050"
    break
  fi
  sleep 2
done

echo "==> Restarting web UI and crawler"
systemctl restart dark-crawler-web.service
systemctl restart dark-crawler.service

systemctl --no-pager --full status dark-crawler-tor.service | sed -n '1,8p'
systemctl --no-pager --full status dark-crawler-web.service | sed -n '1,8p'
systemctl --no-pager --full status dark-crawler.service | sed -n '1,8p'
echo "Web UI: http://104.194.142.112:19810/"
echo "Done."
EOF

#!/usr/bin/env bash
# Non-Docker install on the target host. Run as root.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/dark-crawler}"
TOR_DATA="/var/lib/dark-crawler-tor"
LOG_DIR="/var/log/dark-crawler"

echo "==> Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends tor python3-venv python3-pip ca-certificates curl

# IMPORTANT: never stop/disable the distro Tor on 9050 — other host services may use it.
# This project uses a separate instance on 127.0.0.1:19050 only.

echo "==> Creating users/dirs"
id debian-tor >/dev/null 2>&1 || true
id darkcrawler >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin darkcrawler

mkdir -p "$APP_DIR" "$TOR_DATA" "$LOG_DIR" "$APP_DIR/data"
chown -R debian-tor:debian-tor "$TOR_DATA"
chown darkcrawler:darkcrawler "$LOG_DIR"
chmod 755 "$LOG_DIR"
touch "$LOG_DIR/tor.log"
chown debian-tor:debian-tor "$LOG_DIR/tor.log"
chown -R darkcrawler:darkcrawler "$APP_DIR"
chmod 644 "$APP_DIR/deploy/torrc"
chmod 755 /opt "$APP_DIR" "$APP_DIR/deploy"

echo "==> Python venv"
sudo -u darkcrawler python3 -m venv "$APP_DIR/.venv"
sudo -u darkcrawler "$APP_DIR/.venv/bin/pip" install -U pip
sudo -u darkcrawler "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> Server config"
cp -f "$APP_DIR/deploy/config.server.yaml" "$APP_DIR/config.yaml"
chown darkcrawler:darkcrawler "$APP_DIR/config.yaml"

if [[ ! -f "$APP_DIR/deploy/web.env" ]]; then
  TOKEN="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
)"
  cat > "$APP_DIR/deploy/web.env" <<EOF
WEB_UI_TOKEN=${TOKEN}
EOF
  chown darkcrawler:darkcrawler "$APP_DIR/deploy/web.env"
  chmod 600 "$APP_DIR/deploy/web.env"
  echo "Created web UI token in deploy/web.env"
fi

echo "==> sudoers for crawl start button"
cp -f "$APP_DIR/deploy/sudoers-darkcrawler" /etc/sudoers.d/darkcrawler
chmod 440 /etc/sudoers.d/darkcrawler
visudo -cf /etc/sudoers.d/darkcrawler

echo "==> systemd units"
cp -f "$APP_DIR/deploy/dark-crawler-tor.service" /etc/systemd/system/
cp -f "$APP_DIR/deploy/dark-crawler.service" /etc/systemd/system/
cp -f "$APP_DIR/deploy/dark-crawler-web.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable dark-crawler-tor.service dark-crawler.service dark-crawler-web.service
systemctl restart dark-crawler-tor.service
systemctl restart dark-crawler-web.service

echo "==> Waiting for Tor SOCKS on 19050"
for i in $(seq 1 60); do
  if ss -ltn | grep -q '127.0.0.1:19050'; then
    echo "SOCKS is up on 127.0.0.1:19050"
    break
  fi
  sleep 2
done

echo "==> Status"
systemctl --no-pager --full status dark-crawler-tor.service | sed -n '1,12p'
systemctl --no-pager --full status dark-crawler-web.service | sed -n '1,12p'
ss -ltn | grep -E '19050|19051|19810' || true
TOKEN_VALUE="$(grep '^WEB_UI_TOKEN=' "$APP_DIR/deploy/web.env" | cut -d= -f2-)"
echo "Web UI: http://SERVER_IP:19810/?token=${TOKEN_VALUE}"
echo "Done."

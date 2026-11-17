#!/usr/bin/env bash
# Launch a Tor-proxied Firefox on a virtual display with noVNC for captcha/login.
# Intended to run under dark-crawler-browser.service (localhost websockify only).
set -euo pipefail

APP_DIR="${DARK_CRAWLER_ROOT:-/opt/dark-crawler}"
CONFIG="${DARK_CRAWLER_CONFIG:-$APP_DIR/config.yaml}"
PROFILE_DIR="${BROWSER_PROFILE_DIR:-$APP_DIR/data/browser-profile}"
DISPLAY_NUM="${BROWSER_DISPLAY:-99}"
VNC_PORT="${BROWSER_VNC_PORT:-5901}"
WS_PORT="${BROWSER_WS_PORT:-6080}"
SOCKS_HOST="${TOR_SOCKS_HOST:-127.0.0.1}"
SOCKS_PORT="${TOR_SOCKS_PORT:-19050}"
IDLE_SECONDS="${BROWSER_IDLE_SECONDS:-1800}"

PYTHON_BIN="$APP_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi

# Prefer the site selected by the Web UI Auth page.
TARGET_FILE="$APP_DIR/data/browser-target.json"
SEED_URL="${BROWSER_SEED_URL:-}"
SITE_ID="${BROWSER_SITE_ID:-}"
if [[ -z "$SEED_URL" && -f "$TARGET_FILE" ]]; then
  SEED_URL="$("$PYTHON_BIN" - <<PY
import json
from pathlib import Path
raw = json.loads(Path(r"${TARGET_FILE}").read_text(encoding="utf-8"))
print((raw.get("seed_url") or "").strip())
PY
)"
  SITE_ID="$("$PYTHON_BIN" - <<PY
import json
from pathlib import Path
raw = json.loads(Path(r"${TARGET_FILE}").read_text(encoding="utf-8"))
print((raw.get("site_id") or "").strip())
PY
)"
fi
if [[ -z "$SEED_URL" ]]; then
  SEED_URL="$("$PYTHON_BIN" - <<PY
import yaml
from pathlib import Path
raw = yaml.safe_load(Path(r"${CONFIG}").read_text(encoding="utf-8")) or {}
print((raw.get("seed_url") or "").strip())
PY
)"
fi
if [[ -z "$SEED_URL" ]]; then
  echo "No seed URL: set browser-target.json via Auth page or BROWSER_SEED_URL" >&2
  exit 1
fi

if [[ -n "$SITE_ID" ]]; then
  PROFILE_DIR="${BROWSER_PROFILE_DIR:-$APP_DIR/data/sites/$SITE_ID/browser-profile}"
fi

mkdir -p "$PROFILE_DIR"
cat > "$PROFILE_DIR/user.js" <<EOF
user_pref("network.proxy.type", 1);
user_pref("network.proxy.socks", "${SOCKS_HOST}");
user_pref("network.proxy.socks_port", ${SOCKS_PORT});
user_pref("network.proxy.socks_remote_dns", true);
user_pref("network.proxy.socks_version", 5);
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
user_pref("browser.startup.homepage", "${SEED_URL}");
user_pref("startup.homepage_welcome_url", "");
user_pref("startup.homepage_welcome_url.additional", "");
EOF

export DISPLAY=":${DISPLAY_NUM}"
NOVNC_WEB=""
for candidate in /usr/share/novnc /usr/share/novnc/utils/../; do
  if [[ -d "$candidate" && -f "$candidate/vnc.html" ]]; then
    NOVNC_WEB="$candidate"
    break
  fi
done
if [[ -z "$NOVNC_WEB" ]]; then
  echo "noVNC web files not found (install novnc package)" >&2
  exit 1
fi

FIREFOX_BIN=""
for candidate in firefox firefox-esr chromium chromium-browser google-chrome; do
  if command -v "$candidate" >/dev/null 2>&1; then
    FIREFOX_BIN="$(command -v "$candidate")"
    break
  fi
done
if [[ -z "$FIREFOX_BIN" ]]; then
  echo "No Firefox/Chromium binary found" >&2
  exit 1
fi

cleanup() {
  local code=$?
  [[ -n "${FIREFOX_PID:-}" ]] && kill "$FIREFOX_PID" 2>/dev/null || true
  [[ -n "${X11VNC_PID:-}" ]] && kill "$X11VNC_PID" 2>/dev/null || true
  [[ -n "${WS_PID:-}" ]] && kill "$WS_PID" 2>/dev/null || true
  [[ -n "${XVFB_PID:-}" ]] && kill "$XVFB_PID" 2>/dev/null || true
  exit "$code"
}
trap cleanup EXIT INT TERM

if ! pgrep -f "Xvfb :${DISPLAY_NUM}" >/dev/null 2>&1; then
  Xvfb ":${DISPLAY_NUM}" -screen 0 1280x900x24 -ac >/tmp/dark-crawler-xvfb.log 2>&1 &
  XVFB_PID=$!
  sleep 1
fi

x11vnc -display ":${DISPLAY_NUM}" -rfbport "$VNC_PORT" -localhost -nopw -forever -shared \
  >/tmp/dark-crawler-x11vnc.log 2>&1 &
X11VNC_PID=$!
sleep 1

websockify --web "$NOVNC_WEB" "127.0.0.1:${WS_PORT}" "127.0.0.1:${VNC_PORT}" \
  >/tmp/dark-crawler-websockify.log 2>&1 &
WS_PID=$!
sleep 1

if [[ "$(basename "$FIREFOX_BIN")" == firefox* ]]; then
  "$FIREFOX_BIN" --profile "$PROFILE_DIR" --no-remote "$SEED_URL" \
    >/tmp/dark-crawler-firefox.log 2>&1 &
else
  # Chromium fallback (onion DNS may be weaker than Firefox SOCKS remote DNS).
  "$FIREFOX_BIN" --user-data-dir="$PROFILE_DIR" \
    --proxy-server="socks5://${SOCKS_HOST}:${SOCKS_PORT}" \
    --no-first-run --disable-gpu --window-size=1280,900 \
    "$SEED_URL" >/tmp/dark-crawler-firefox.log 2>&1 &
fi
FIREFOX_PID=$!

echo "Browser session ready on 127.0.0.1:${WS_PORT} (idle limit ${IDLE_SECONDS}s)"
# Keep the unit alive until idle timeout or browser exit.
elapsed=0
while kill -0 "$FIREFOX_PID" 2>/dev/null; do
  sleep 5
  elapsed=$((elapsed + 5))
  if (( elapsed >= IDLE_SECONDS )); then
    echo "Idle timeout reached; stopping browser session"
    break
  fi
done

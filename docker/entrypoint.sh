#!/usr/bin/env bash
set -euo pipefail

TOR_HOST="${TOR_SOCKS_HOST:-tor}"
TOR_PORT="${TOR_SOCKS_PORT:-9050}"
WAIT_SECONDS="${TOR_WAIT_SECONDS:-300}"

echo "Waiting for Tor SOCKS at ${TOR_HOST}:${TOR_PORT} (timeout ${WAIT_SECONDS}s)..."
deadline=$((SECONDS + WAIT_SECONDS))
while (( SECONDS < deadline )); do
  if python - <<PY
import socket, sys
host = "${TOR_HOST}"
port = int("${TOR_PORT}")
try:
    with socket.create_connection((host, port), timeout=2):
        sys.exit(0)
except OSError:
    sys.exit(1)
PY
  then
    echo "Tor SOCKS is reachable."
    break
  fi
  sleep 2
done

if (( SECONDS >= deadline )); then
  echo "Timed out waiting for Tor at ${TOR_HOST}:${TOR_PORT}" >&2
  exit 2
fi

# Extra grace: circuits may still be building after the port opens.
sleep "${TOR_BOOTSTRAP_GRACE:-15}"

exec python -m crawler -c /app/config.yaml "$@"

#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# Prefer Docker stack when available.
if command -v docker >/dev/null 2>&1; then
  case "${1:-}" in
    up)
      shift
      exec docker compose up -d --build "$@"
      ;;
    down)
      shift
      exec docker compose down "$@"
      ;;
    logs)
      shift
      exec docker compose logs -f "$@"
      ;;
    crawl|stats|export)
      # One-shot crawler command against the compose stack.
      docker compose up -d tor
      exec docker compose run --rm crawler "$@"
      ;;
  esac
fi

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -U pip
  .venv/bin/pip install -r requirements.txt
fi

exec .venv/bin/python -m crawler "$@"

#!/usr/bin/env bash
# Start the FastAPI backend with reload.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -d "$ROOT/.venv" ] && [ -z "${VIRTUAL_ENV:-}" ]; then
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
fi

# Free the port first: a survivor from a previous run would otherwise keep
# serving stale code while this process dies with "address already in use".
# shellcheck disable=SC1091
source "$ROOT/scripts/backend_ctl.sh"
wq_stop_backend "${API_PORT:-8000}" "$ROOT/logs/backend.pid" || {
  echo "port ${API_PORT:-8000} is still busy"; exit 1;
}

export PHOTO_DIR="${PHOTO_DIR:-$HOME/properties}"
cd "$ROOT/backend"
exec uvicorn app.main:app --reload --host "${API_HOST:-127.0.0.1}" --port "${API_PORT:-8000}"

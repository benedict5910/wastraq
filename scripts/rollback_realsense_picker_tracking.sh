#!/usr/bin/env bash
# =====================================================================
# Undo the RealSense picker-tracking phase, completely.
#
#   ./scripts/rollback_realsense_picker_tracking.sh              # newest backup
#   ./scripts/rollback_realsense_picker_tracking.sh <backup-dir> # a specific one
#   DRY_RUN=1 ./scripts/rollback_realsense_picker_tracking.sh    # show only
#
# Restores the four modified files from backups/, removes the files this
# phase added, and restarts the backend.
#
# It does NOT touch PostgreSQL, because this phase never touched PostgreSQL.
# No table was added, no row was written, no verification state was changed.
# =====================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

ok()   { printf "  \033[32mok\033[0m    %s\n" "$*"; }
warn() { printf "  \033[33mwarn\033[0m  %s\n" "$*"; }
die()  { printf "  \033[31mFAIL\033[0m  %s\n" "$*"; exit 1; }

BACKUP="${1:-}"
if [ -z "$BACKUP" ]; then
  BACKUP="$(ls -d backups/realsense-picker-tracking-* 2>/dev/null | sort | tail -1)"
fi
[ -n "$BACKUP" ] && [ -d "$BACKUP" ] || die "no backup directory found under backups/"
echo "Rolling back RealSense picker tracking"
echo "backup: $BACKUP"
[ "${DRY_RUN:-0}" = "1" ] && echo "mode  : DRY RUN"

RESTORE=(
  backend/app/main.py
  backend/app/config.py
  backend/app/__init__.py
  backend/app/static/assets/wq.js
  README.md
)
REMOVE=(
  backend/app/vision
  backend/app/static/picker-tracking.html
  backend/requirements-vision.txt
  scripts/test_vision_logic.py
  scripts/test_realsense_picker_tracking.py
  scripts/add_realsense_picker_tracking.sh
  docs/VISION.md
)

printf "\n\033[1mRestore\033[0m\n"
for f in "${RESTORE[@]}"; do
  if [ -f "$BACKUP/$f" ]; then
    if [ "${DRY_RUN:-0}" = "1" ]; then echo "  would restore $f"
    else cp "$BACKUP/$f" "$f" && ok "restored $f"; fi
  else
    warn "not in this backup: $f"
  fi
done

printf "\n\033[1mRemove\033[0m\n"
for f in "${REMOVE[@]}"; do
  if [ -e "$f" ]; then
    if [ "${DRY_RUN:-0}" = "1" ]; then echo "  would remove $f"
    else rm -rf "$f" && ok "removed $f"; fi
  fi
done

# models/ and the fetched weights are left alone on purpose: they are a
# download, not a change to the project, and re-fetching them is slow.
[ -d models ] && warn "models/ left in place (delete it by hand if you want the disk back)"

printf "\n\033[1mBackend\033[0m\n"
if [ "${DRY_RUN:-0}" = "1" ] || [ "${SKIP_BACKEND:-0}" = "1" ]; then
  warn "not restarting the backend"
else
  # shellcheck disable=SC1091
  source "$ROOT/scripts/backend_ctl.sh"
  mkdir -p logs
  wq_stop_backend "${API_PORT:-8000}" "logs/backend.pid" >/dev/null 2>&1
  if wq_start_backend "$ROOT" "${API_HOST:-127.0.0.1}" "${API_PORT:-8000}" "$ROOT/logs"; then
    ok "backend restarted (version $WQ_EXPECTED_VERSION)"
  else
    warn "backend did not come up - see logs/backend.log"
  fi
fi

echo
echo "Rolled back. The database was not touched at any point."
echo "Verify with: ./scripts/verify_demo.sh"

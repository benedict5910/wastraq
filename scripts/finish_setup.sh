#!/usr/bin/env bash
# =====================================================================
# One command that takes the project from "PostgreSQL + PostGIS are
# installed" to "the whole demo has run and been verified".
#
#   ./scripts/finish_setup.sh
#
# It does NOT install PostgreSQL, does NOT drop your database, and does
# NOT touch anything outside this project folder.
#
# Steps:
#   1. locate PostgreSQL 17 (or 16/15) and check the server + PostGIS
#   2. load database/schema.sql, seed.sql, gis_dummy_data.sql, lookup_function.sql
#   3. create .venv and install backend/requirements.txt
#   4. start the FastAPI backend in the background
#   5. run simulation/simulate_picker.py
#   6. run scripts/verify_demo.sh
#
# Everything is logged under logs/ so the whole run can be reviewed
# (or handed back to Claude) afterwards.
# =====================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DB_NAME="${DB_NAME:-wastraq_demo}"
API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="${API_PORT:-8000}"
API="http://${API_HOST}:${API_PORT}"

LOGS="$ROOT/logs"
mkdir -p "$LOGS"
MAIN_LOG="$LOGS/finish_setup.log"

# Tee everything from here on.
exec > >(tee "$MAIN_LOG") 2>&1

step()  { printf "\n\033[1m=== %s\033[0m\n" "$*"; }
ok()    { printf "  \033[32mok\033[0m    %s\n" "$*"; }
warn()  { printf "  \033[33mwarn\033[0m  %s\n" "$*"; }
die()   { printf "  \033[31mFAIL\033[0m  %s\n" "$*"; printf "\nStopped. Full log: %s\n" "$MAIN_LOG"; exit 1; }

echo "Wastraq demo - finish setup"
echo "project: $ROOT"
echo "started: $(date)"

# ---------------------------------------------------------------------
step "1/6  PostgreSQL + PostGIS"
# ---------------------------------------------------------------------
# shellcheck disable=SC1091
source "$ROOT/scripts/pg_env.sh"
# shellcheck disable=SC1091
source "$ROOT/scripts/backend_ctl.sh"
wq_resolve_pg || die "no psql found. Try: export PATH=\"/opt/homebrew/opt/postgresql@17/bin:\$PATH\""

echo "  psql binary:  $(command -v psql)"
echo "  psql version: $(psql --version)"

pg_isready -q || die "PostgreSQL server is not accepting connections. Try: brew services start postgresql@17"
SERVER_VER="$(psql -tA -d postgres -c 'SHOW server_version;' 2>/dev/null)"
[ -n "$SERVER_VER" ] || die "cannot connect to the 'postgres' database as $(whoami)"
ok "server version $SERVER_VER"

psql -tA -lqt 2>/dev/null | cut -d'|' -f1 | grep -qx "$DB_NAME" \
  || die "database '$DB_NAME' does not exist. Create it with: createdb $DB_NAME"
ok "database $DB_NAME exists"

PGIS="$(psql -tA -d "$DB_NAME" -c 'SELECT PostGIS_Version();' 2>/dev/null)"
if [ -z "$PGIS" ]; then
  warn "PostGIS not enabled in $DB_NAME - enabling it now"
  psql -q -v ON_ERROR_STOP=1 -d "$DB_NAME" -c 'CREATE EXTENSION IF NOT EXISTS postgis;' \
    || die "could not enable PostGIS in $DB_NAME"
  PGIS="$(psql -tA -d "$DB_NAME" -c 'SELECT PostGIS_Version();')"
fi
ok "PostGIS $PGIS"

# ---------------------------------------------------------------------
step "2/6  Load SQL (schema -> seed -> gis -> lookup functions)"
# ---------------------------------------------------------------------
for f in database/schema.sql database/seed.sql database/gis_dummy_data.sql database/lookup_function.sql; do
  [ -f "$ROOT/$f" ] || die "missing $f"
done

load() {
  echo "  loading $1"
  psql -q -v ON_ERROR_STOP=1 -d "$DB_NAME" -f "$ROOT/$1" >>"$LOGS/psql.log" 2>&1 \
    || { echo "----- psql output -----"; tail -40 "$LOGS/psql.log"; die "$1 failed to load (see $LOGS/psql.log)"; }
}
: > "$LOGS/psql.log"
load database/schema.sql
load database/seed.sql
load database/gis_dummy_data.sql
load database/lookup_function.sql
ok "base schema + synthetic lane loaded"

# The real surveyed 16-property lane supersedes the synthetic one.
if [ -f "$ROOT/database/real_lane_16.sql" ]; then
  PHOTO_DIR="${PHOTO_DIR:-$HOME/properties}"
  PHOTO_DIR="${PHOTO_DIR/#\~/$HOME}"
  APPLY="$LOGS/real_lane_16_applied.sql"
  sed "s|__PHOTO_DIR__|${PHOTO_DIR}|g" "$ROOT/database/real_lane_16.sql" > "$APPLY"
  echo "  loading database/real_lane_16.sql  (photos: $PHOTO_DIR)"
  psql -q -v ON_ERROR_STOP=1 -d "$DB_NAME" -f "$APPLY" >>"$LOGS/psql.log" 2>&1 \
    || { echo "----- psql output -----"; tail -30 "$LOGS/psql.log"; \
         die "real_lane_16.sql failed (transaction rolled back; see $LOGS/psql.log)"; }
  ok "real 16-property lane loaded (2nd Cross Road, Krishnamurthy Puram)"
else
  warn "database/real_lane_16.sql not found - staying on the synthetic 10-property lane"
fi

# ---------------------------------------------------------------------
step "3/6  Python environment"
# ---------------------------------------------------------------------
# One place builds the virtualenv, for both this script and
# upgrade_dashboards.sh. It pins the interpreter (3.11 - see
# scripts/py_env.sh), rebuilds .venv when it is on the wrong Python, and
# refuses to report success unless backend/app/main.py imports.
#
# The previous version of this block retried `pip install` WITHOUT the
# requirements file when the pinned install failed. That produced a venv
# that looked healthy and was missing python-multipart, which FastAPI
# needs at route-build time - so the backend died at start-up with an
# error that pointed nowhere near the actual cause. There is no silent
# fallback here any more: it either works or it says exactly what is
# missing.
PYTHON="${PYTHON:-}" bash "$ROOT/scripts/setup_python_env.sh" \
  || die "could not build a working Python environment (see the messages above)"
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
ok "backend dependencies installed ($(python -V 2>&1))"

if [ ! -f "$ROOT/backend/.env" ]; then
  cp "$ROOT/backend/.env.example" "$ROOT/backend/.env"
  echo "DB_USER=$(whoami)" >> "$ROOT/backend/.env"
  ok "created backend/.env (DB_USER=$(whoami))"
else
  ok "backend/.env already exists"
fi
if ! grep -q '^PHOTO_DIR=' "$ROOT/backend/.env" 2>/dev/null; then
  printf 'PHOTO_DIR=%s\n' "${PHOTO_DIR:-$HOME/properties}" >> "$ROOT/backend/.env"
  ok "recorded PHOTO_DIR in backend/.env"
fi

# ---------------------------------------------------------------------
step "4/6  Backend"
# ---------------------------------------------------------------------
# Always (re)start rather than reusing whatever is on the port: a survivor
# from an earlier run serves stale code while answering every probe 200 OK.
echo "  starting uvicorn on $API"
if wq_start_backend "$ROOT" "$API_HOST" "$API_PORT" "$LOGS" "${PHOTO_DIR:-$HOME/properties}"; then
  ok "backend up (pid $(cat "$LOGS/backend.pid"), version $WQ_EXPECTED_VERSION)"
else
  die "backend did not come up cleanly on $API (see $LOGS/backend.log)"
fi

echo "  GET /        -> $(curl -sf "$API/")"
echo "  GET /health/db -> $(curl -sf "$API/health/db")"

# ---------------------------------------------------------------------
step "5/6  Simulation"
# ---------------------------------------------------------------------
python "$ROOT/simulation/simulate_picker.py" --api "$API" --delay 0 > "$LOGS/simulation.log" 2>&1
SIM_RC=$?
tail -32 "$LOGS/simulation.log"
if [ $SIM_RC -ne 0 ]; then
  echo "----- full simulation log -----"; cat "$LOGS/simulation.log"
  die "simulation exited $SIM_RC (see $LOGS/simulation.log)"
fi
ok "simulation completed (full output: $LOGS/simulation.log)"

# ---------------------------------------------------------------------
step "6/6  Verification"
# ---------------------------------------------------------------------
DB_NAME="$DB_NAME" API="$API" bash "$ROOT/scripts/verify_demo.sh" > "$LOGS/verify.log" 2>&1
VERIFY_RC=$?
cat "$LOGS/verify.log"

echo
echo "======================================================================"
if [ $VERIFY_RC -eq 0 ]; then
  printf "\033[32mVERIFICATION PASSED\033[0m\n"
else
  printf "\033[31mVERIFICATION FAILED (exit %d)\033[0m\n" "$VERIFY_RC"
fi
echo "======================================================================"
echo "Backend:    $API           (pid $(cat "$LOGS/backend.pid" 2>/dev/null || echo '?'))"
echo "Dashboard:  $API/dashboard"
echo "API docs:   $API/docs"
echo "Logs:       $LOGS/"
echo "Stop the backend with:  kill \$(cat logs/backend.pid)"
echo "finished: $(date)"
exit $VERIFY_RC

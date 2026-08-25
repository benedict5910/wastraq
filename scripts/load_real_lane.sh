#!/usr/bin/env bash
# =====================================================================
# Replace the synthetic demo lane with the REAL surveyed 16-property lane.
#
#   ./scripts/load_real_lane.sh
#
# Options (environment):
#   PHOTO_DIR=~/properties     where PROP-001.jpg ... PROP-016.jpg live
#   DB_NAME=wastraq_demo
#   API=http://127.0.0.1:8000
#   SKIP_BACKEND=1             don't touch the backend
#   SKIP_SIM=1                 don't run the picker simulation
#
# What it does, in order:
#   1. locate PostgreSQL 17 and check the server, the database and PostGIS
#   2. check the 16 frontage photos are actually on disk
#   3. back up the current GIS tables to logs/ (schema + data)
#   4. regenerate + apply database/real_lane_16.sql inside one transaction
#   5. validate the loaded geometry in PostGIS
#   6. restart the backend so it picks up the new data
#   7. run the picker simulation over the real lane
#   8. run ./scripts/verify_demo.sh
#
# It never drops the database and never touches pickers, collection_events
# or evidence.
# =====================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DB_NAME="${DB_NAME:-wastraq_demo}"
API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="${API_PORT:-8000}"
API="${API:-http://${API_HOST}:${API_PORT}}"
PHOTO_DIR="${PHOTO_DIR:-$HOME/properties}"
PHOTO_DIR="${PHOTO_DIR/#\~/$HOME}"

LOGS="$ROOT/logs"; mkdir -p "$LOGS"
STAMP="$(date +%Y%m%d-%H%M%S)"
MAIN_LOG="$LOGS/load_real_lane_${STAMP}.log"
exec > >(tee "$MAIN_LOG") 2>&1

step() { printf "\n\033[1m=== %s\033[0m\n" "$*"; }
ok()   { printf "  \033[32mok\033[0m    %s\n" "$*"; }
warn() { printf "  \033[33mwarn\033[0m  %s\n" "$*"; }
die()  { printf "  \033[31mFAIL\033[0m  %s\n" "$*"; printf "\nStopped. Log: %s\n" "$MAIN_LOG"; exit 1; }

echo "Wastraq - load the real 16-property lane"
echo "project   : $ROOT"
echo "database  : $DB_NAME"
echo "photo dir : $PHOTO_DIR"
echo "started   : $(date)"

# ---------------------------------------------------------------------
step "1/8  PostgreSQL 17 + PostGIS"
# ---------------------------------------------------------------------
# shellcheck disable=SC1091
source "$ROOT/scripts/pg_env.sh"
# shellcheck disable=SC1091
source "$ROOT/scripts/backend_ctl.sh"
wq_resolve_pg || die "no psql found. Try: export PATH=\"/opt/homebrew/opt/postgresql@17/bin:\$PATH\""
echo "  psql: $(command -v psql)  ($(psql --version))"

pg_isready -q || die "PostgreSQL is not accepting connections (brew services start postgresql@17)"
SV="$(psql -tA -d postgres -c 'SHOW server_version;' 2>/dev/null)" || true
[ -n "$SV" ] || die "cannot connect to PostgreSQL as $(whoami)"
ok "server $SV"

psql -tA -lqt 2>/dev/null | cut -d'|' -f1 | grep -qx "$DB_NAME" \
  || die "database '$DB_NAME' does not exist (createdb $DB_NAME)"
ok "database $DB_NAME exists"

PGIS="$(psql -tA -d "$DB_NAME" -c 'SELECT PostGIS_Version();' 2>/dev/null)"
[ -n "$PGIS" ] || die "PostGIS is not enabled in $DB_NAME (CREATE EXTENSION postgis;)"
ok "PostGIS $PGIS"

for t in properties property_entrances property_frontages property_service_zones \
         pickers collection_events evidence; do
  psql -tA -d "$DB_NAME" -c "SELECT to_regclass('public.$t');" | grep -q "$t" \
    || die "table '$t' is missing - load database/schema.sql first (./scripts/finish_setup.sh)"
done
ok "all base tables present"

# Persist PHOTO_DIR so `run_backend.sh` (and any manual uvicorn) finds the
# photos too - config.py reads backend/.env via load_dotenv().
if [ -f "$ROOT/backend/.env" ]; then
  if grep -q '^PHOTO_DIR=' "$ROOT/backend/.env" 2>/dev/null; then
    :
  else
    printf 'PHOTO_DIR=%s\n' "$PHOTO_DIR" >> "$ROOT/backend/.env"
    ok "recorded PHOTO_DIR in backend/.env"
  fi
fi

# ---------------------------------------------------------------------
step "2/8  Frontage photos"
# ---------------------------------------------------------------------
MISSING=""
for i in $(seq -w 1 16); do
  [ -f "$PHOTO_DIR/PROP-0$i.jpg" ] || MISSING="$MISSING PROP-0$i.jpg"
done
if [ -n "$MISSING" ]; then
  warn "not found in $PHOTO_DIR:$MISSING"
  warn "the rows will still be linked; GET /properties/{id}/photo will 404 until the files are there"
else
  ok "all 16 photos found in $PHOTO_DIR"
fi

# ---------------------------------------------------------------------
step "3/8  Back up the current GIS tables"
# ---------------------------------------------------------------------
BACKUP="$LOGS/backup_gis_${STAMP}.sql"
if command -v pg_dump >/dev/null 2>&1; then
  PHOTO_TBL=""
  psql -tA -d "$DB_NAME" -c "SELECT to_regclass('public.property_photos');" | grep -q property_photos \
    && PHOTO_TBL="-t property_photos"
  # shellcheck disable=SC2086
  pg_dump -d "$DB_NAME" --data-only --column-inserts \
      -t properties -t property_entrances -t property_frontages -t property_service_zones \
      $PHOTO_TBL > "$BACKUP" 2>"$LOGS/pg_dump.err" \
    && ok "backed up to $(basename "$BACKUP") ($(wc -l < "$BACKUP" | tr -d ' ') lines)" \
    || { cat "$LOGS/pg_dump.err"; die "pg_dump failed"; }
  cp "$BACKUP" "$LOGS/backup_gis_latest.sql"
else
  die "pg_dump not found on PATH"
fi

BEFORE="$(psql -tA -d "$DB_NAME" -c 'SELECT count(*) FROM properties;')"
echo "  properties before: $BEFORE"

# ---------------------------------------------------------------------
step "4/8  Generate + apply the real lane"
# ---------------------------------------------------------------------
PY="$ROOT/.venv/bin/python"; [ -x "$PY" ] || PY="python3"
if "$PY" -c "import numpy" >/dev/null 2>&1; then
  "$PY" "$ROOT/scripts/generate_real_lane.py" > "$LOGS/generate_real_lane.log" 2>&1 \
    && ok "regenerated database/real_lane_16.sql from the surveyed coordinates" \
    || { tail -20 "$LOGS/generate_real_lane.log"; die "geometry generation failed"; }
  grep -E "^  (ok|FAIL|note)" "$LOGS/generate_real_lane.log" | sed 's/^/  /'
else
  warn "numpy not installed - using the committed database/real_lane_16.sql as-is"
  warn "(pip install -r backend/requirements.txt to enable regeneration)"
fi

[ -f "$ROOT/database/real_lane_16.sql" ] || die "database/real_lane_16.sql not found"

# Substitute the photo directory into the generated SQL.
APPLY="$LOGS/real_lane_16_applied_${STAMP}.sql"
sed "s|__PHOTO_DIR__|${PHOTO_DIR}|g" "$ROOT/database/real_lane_16.sql" > "$APPLY"
grep -q "__PHOTO_DIR__" "$APPLY" && die "photo path substitution failed"

psql -v ON_ERROR_STOP=1 -d "$DB_NAME" -f "$APPLY" > "$LOGS/psql_real_lane.log" 2>&1 \
  || { echo "----- psql output -----"; tail -30 "$LOGS/psql_real_lane.log"; \
       die "real_lane_16.sql failed - the transaction rolled back, nothing changed"; }
grep -i "NOTICE" "$LOGS/psql_real_lane.log" | sed 's/^/  /'
ok "real_lane_16.sql applied (single transaction, committed)"

# ---------------------------------------------------------------------
step "5/8  Validate the loaded geometry in PostGIS"
# ---------------------------------------------------------------------
psql -d "$DB_NAME" <<'SQL'
\pset border 2
SELECT
  (SELECT count(*) FROM properties)                                   AS properties,
  (SELECT count(*) FROM property_entrances)                           AS entrances,
  (SELECT count(*) FROM property_frontages)                           AS frontages,
  (SELECT count(*) FROM property_service_zones)                       AS zones,
  (SELECT count(*) FROM property_photos WHERE photo_type='FRONTAGE')  AS photos;

SELECT count(*) FILTER (WHERE NOT ST_IsValid(geometry)) AS invalid_polygons,
       count(*) FILTER (WHERE ST_SRID(geometry) <> 4326) AS wrong_srid,
       round(min(ST_Area(geometry::geography))::numeric,1) AS min_area_m2,
       round(max(ST_Area(geometry::geography))::numeric,1) AS max_area_m2
FROM property_service_zones;

SELECT count(*) AS overlapping_zone_pairs
FROM property_service_zones a JOIN property_service_zones b ON a.zone_id < b.zone_id
WHERE ST_Overlaps(a.geometry, b.geometry) OR ST_Contains(a.geometry, b.geometry);

SELECT f.road_side, count(*) AS n
FROM property_frontages f GROUP BY f.road_side ORDER BY f.road_side;
SQL
ok "geometry validated"

# ---------------------------------------------------------------------
step "6/8  Backend"
# ---------------------------------------------------------------------
if [ "${SKIP_BACKEND:-0}" = "1" ]; then
  warn "SKIP_BACKEND=1 - leaving the backend alone"
elif [ ! -x "$ROOT/.venv/bin/uvicorn" ]; then
  warn "no .venv - run ./scripts/finish_setup.sh first; skipping backend start"
else
  # wq_start_backend stops by pidfile AND by port, waits for the port to be
  # genuinely free, then confirms the process answering reports the current
  # version - so a stale uvicorn can no longer keep serving old code.
  if wq_start_backend "$ROOT" "$API_HOST" "$API_PORT" "$LOGS" "$PHOTO_DIR"; then
    ok "backend restarted on $API (pid $(cat "$LOGS/backend.pid"), version $WQ_EXPECTED_VERSION)"
    echo "  /health/db -> $(curl -sf "$API/health/db")"
  else
    die "backend did not come up cleanly on $API (see $LOGS/backend.log)"
  fi
fi

# ---------------------------------------------------------------------
step "7/8  Picker simulation over the real lane"
# ---------------------------------------------------------------------
if [ "${SKIP_SIM:-0}" = "1" ]; then
  warn "SKIP_SIM=1 - skipping the simulation"
elif curl -sf "$API/" >/dev/null 2>&1; then
  "$PY" "$ROOT/simulation/simulate_picker.py" --api "$API" --delay 0 \
      > "$LOGS/simulation_real_lane.log" 2>&1
  SIM_RC=$?
  tail -22 "$LOGS/simulation_real_lane.log"
  [ $SIM_RC -eq 0 ] && ok "simulation completed (full log: $LOGS/simulation_real_lane.log)" \
                    || warn "simulation exited $SIM_RC - see $LOGS/simulation_real_lane.log"
else
  warn "backend not reachable - skipping the simulation"
fi

# ---------------------------------------------------------------------
step "8/8  Verification"
# ---------------------------------------------------------------------
DB_NAME="$DB_NAME" API="$API" bash "$ROOT/scripts/verify_demo.sh" > "$LOGS/verify_real_lane.log" 2>&1
VRC=$?
cat "$LOGS/verify_real_lane.log"

echo
echo "======================================================================"
if [ $VRC -eq 0 ]; then printf "\033[32mREAL LANE LOADED AND VERIFIED\033[0m\n"
else printf "\033[31mVERIFICATION FAILED (exit %d)\033[0m\n" "$VRC"; fi
echo "======================================================================"
echo "Dashboard : $API/dashboard"
echo "API docs  : $API/docs"
echo "Backup    : $BACKUP"
echo "Logs      : $LOGS/"
echo "Rollback  : psql -d $DB_NAME -f $LOGS/backup_gis_latest.sql   (after re-running schema.sql)"
echo "QGIS      : refresh property_entrances / property_frontages / property_service_zones"
echo "finished  : $(date)"
exit $VRC

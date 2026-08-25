#!/usr/bin/env bash
# =====================================================================
# Strip the synthetic city seed data and switch to the simplified,
# real-data-only survey UI.
#
#   ./scripts/simplify_survey.sh
#
# Options (environment):
#   DB_NAME=wastraq_demo
#   API=http://127.0.0.1:8000
#   SKIP_BACKEND=1     don't stop/start the backend
#   SKIP_VERIFY=1      don't run scripts/verify_demo.sh at the end
#   DRY_RUN=1          report only - no schema, no cleanup, no writes
#
# What it does:
#   1. check PostgreSQL, PostGIS and that the 16-property lane is loaded
#   2. back up the whole database to logs/
#   3. apply database/survey_schema.sql   (additive + idempotent)
#   4. run database/cleanup_synthetic_city.sql  (one transaction, idempotent)
#   5. restart the backend on the current code
#   6. run scripts/verify_demo.sh
#
# Step 3 is not optional. The schema file is additive and idempotent, and a
# release that adds a column has to reach the database before the backend
# queries it - otherwise endpoints 500 on a column that only exists in the
# source tree. That is exactly what happened when this script only cleaned
# up and never migrated.
#
# It NEVER re-seeds. It NEVER touches the 16 real lane properties, their
# geometry, photos, collection events or evidence - the cleanup aborts if
# the lane count moves by a single row. The schema, every table, view,
# index and trigger is left exactly as it is: this removes rows, not
# capability.
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
DEMO_ROUTE="${DEMO_ROUTE_ID:-ROUTE-DEMO-01}"

LOGS="$ROOT/logs"; mkdir -p "$LOGS"
STAMP="$(date +%Y%m%d-%H%M%S)"
MAIN_LOG="$LOGS/simplify_survey_${STAMP}.log"
exec > >(tee "$MAIN_LOG") 2>&1

step() { printf "\n\033[1m=== %s\033[0m\n" "$*"; }
ok()   { printf "  \033[32mok\033[0m    %s\n" "$*"; }
warn() { printf "  \033[33mwarn\033[0m  %s\n" "$*"; }
die()  { printf "  \033[31mFAIL\033[0m  %s\n" "$*"; printf "\nStopped. Log: %s\n" "$MAIN_LOG"; exit 1; }

echo "Wastraq - simplify the survey module to real pilot data only"
echo "project   : $ROOT"
echo "database  : $DB_NAME"
echo "pilot lane: $DEMO_ROUTE"
echo "started   : $(date)"

# ---------------------------------------------------------------------
step "1/6  Preconditions"
# ---------------------------------------------------------------------
# shellcheck disable=SC1091
source "$ROOT/scripts/pg_env.sh"
# shellcheck disable=SC1091
source "$ROOT/scripts/backend_ctl.sh"
wq_resolve_pg || die "no psql found - see scripts/pg_env.sh"
pg_isready -q || die "PostgreSQL is not accepting connections"

PGIS="$(psql -tA -d "$DB_NAME" -c 'SELECT PostGIS_Version();' 2>/dev/null)"
[ -n "$PGIS" ] || die "PostGIS not available in $DB_NAME"
ok "database $DB_NAME with PostGIS $PGIS"

LANE="$(psql -tA -d "$DB_NAME" -c "SELECT count(*) FROM properties WHERE route_id='$DEMO_ROUTE';")"
[ "${LANE:-0}" -eq 16 ] || die "expected 16 properties on $DEMO_ROUTE, found ${LANE:-0}"
ok "the 16-property pilot lane is present and will be left untouched"

[ -f "$ROOT/database/cleanup_synthetic_city.sql" ] \
  || die "database/cleanup_synthetic_city.sql not found"

# Back up before anything is written, including the schema step.
BACKUP="$LOGS/backup_before_simplify_${STAMP}.sql"
command -v pg_dump >/dev/null 2>&1 || die "pg_dump not found on PATH"
pg_dump -d "$DB_NAME" --column-inserts > "$BACKUP" 2>"$LOGS/pg_dump_simplify.err" \
  && ok "full backup -> $(basename "$BACKUP") ($(wc -l < "$BACKUP" | tr -d ' ') lines)" \
  || { cat "$LOGS/pg_dump_simplify.err"; die "pg_dump failed"; }
cp "$BACKUP" "$LOGS/backup_before_simplify_latest.sql"

if [ "${DRY_RUN:-0}" = "1" ]; then
  psql -d "$DB_NAME" <<SQL
\\pset border 2
SELECT
  (SELECT count(*) FROM properties)                                     AS properties_total,
  (SELECT count(*) FROM properties WHERE route_id='$DEMO_ROUTE')        AS pilot_lane,
  (SELECT count(*) FROM properties WHERE property_id ~ '^PROP-[0-9]{5}\$') AS synthetic,
  (SELECT count(*) FROM administrative_units)                           AS admin_units,
  (SELECT count(*) FROM survey_users)                                   AS users;
SQL
  warn "DRY_RUN=1 - nothing was changed (no schema, no cleanup)"
  exit 0
fi

# ---------------------------------------------------------------------
step "2/6  Schema"
# ---------------------------------------------------------------------
# Always applied: additive, idempotent, and it never touches data.
[ -f "$ROOT/database/survey_schema.sql" ] || die "database/survey_schema.sql not found"
psql -v ON_ERROR_STOP=1 -d "$DB_NAME" -f "$ROOT/database/survey_schema.sql" \
     > "$LOGS/psql_schema_${STAMP}.log" 2>&1 \
  || { echo "----- psql output -----"; tail -30 "$LOGS/psql_schema_${STAMP}.log"; \
       die "survey_schema.sql failed - rolled back, nothing changed"; }
ok "survey schema applied (existing objects left alone)"

if [ -f "$ROOT/database/property_master.sql" ]; then
  psql -v ON_ERROR_STOP=1 -d "$DB_NAME" -f "$ROOT/database/property_master.sql" \
       > "$LOGS/psql_property_master_${STAMP}.log" 2>&1 \
    || { echo "----- psql output -----"; tail -30 "$LOGS/psql_property_master_${STAMP}.log"; \
         die "property_master.sql failed - rolled back, nothing changed"; }
  ok "property master schema applied"
fi

# Named check: a stale schema must fail here, by column name, not later as a
# 500 from an endpoint three layers away.
MISSING_COLS="$(psql -tA -d "$DB_NAME" -c "
  SELECT string_agg(c, ', ') FROM (
    SELECT c FROM unnest(ARRAY['owner_phone','owner_email','street_name','locality',
                               'pincode','service_entity_type','updated_at']) c
    WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name='properties' AND column_name=c)
    UNION ALL
    SELECT c FROM unnest(ARRAY['capture_method','capture_latitude','capture_longitude']) c
    WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name='property_photos' AND column_name=c)
  ) x;")"
[ -z "$MISSING_COLS" ] || die "schema is still behind: missing $MISSING_COLS"
ok "field-workflow columns present on properties and property_photos"

# ---------------------------------------------------------------------
step "3/6  Current contents"
# ---------------------------------------------------------------------
psql -d "$DB_NAME" <<SQL
\\pset border 2
SELECT
  (SELECT count(*) FROM properties)                                     AS properties_total,
  (SELECT count(*) FROM properties WHERE route_id='$DEMO_ROUTE')        AS pilot_lane,
  (SELECT count(*) FROM properties WHERE property_id ~ '^PROP-[0-9]{5}\$') AS synthetic,
  (SELECT count(*) FROM administrative_units)                           AS admin_units,
  (SELECT count(*) FROM survey_users)                                   AS users,
  (SELECT count(*) FROM survey_assignments)                             AS assignments,
  (SELECT count(*) FROM property_surveys)                               AS surveys,
  (SELECT count(*) FROM property_qa_issues)                             AS qa_issues;
SQL

SYNTH="$(psql -tA -d "$DB_NAME" -c \
  "SELECT count(*) FROM properties WHERE property_id ~ '^PROP-[0-9]{5}\$';")"
if [ "${SYNTH:-0}" -eq 0 ]; then
  ok "no synthetic properties found - the database is already clean"
else
  ok "${SYNTH} synthetic propert(ies) will be removed"
fi

# ---------------------------------------------------------------------
step "4/6  Remove the synthetic city data"
# ---------------------------------------------------------------------
# One transaction with its own post-conditions: if the lane count moves,
# or synthetic rows survive, or the last surveyor/reviewer would be lost,
# it raises and the whole thing rolls back.
psql -v ON_ERROR_STOP=1 -d "$DB_NAME" \
     -f "$ROOT/database/cleanup_synthetic_city.sql" \
     > "$LOGS/psql_cleanup_${STAMP}.log" 2>&1 \
  || { echo "----- psql output -----"; tail -30 "$LOGS/psql_cleanup_${STAMP}.log"; \
       die "cleanup failed - rolled back, the database is as it was"; }
grep -i "NOTICE" "$LOGS/psql_cleanup_${STAMP}.log" | sed 's/^[^ ]* //' | sed 's/^/  /'
ok "cleanup committed"

LANE_AFTER="$(psql -tA -d "$DB_NAME" -c "SELECT count(*) FROM properties WHERE route_id='$DEMO_ROUTE';")"
[ "${LANE_AFTER:-0}" -eq 16 ] || die "the pilot lane changed - restore $BACKUP"
ok "pilot lane still holds $LANE_AFTER properties"

psql -d "$DB_NAME" <<SQL
\\pset border 2
SELECT
  (SELECT count(*) FROM properties)               AS properties_total,
  (SELECT count(*) FROM administrative_units)     AS admin_units,
  (SELECT count(*) FROM survey_users)             AS users,
  (SELECT count(*) FROM property_surveys)         AS surveys,
  (SELECT count(*) FROM property_entrances)       AS entrances,
  (SELECT count(*) FROM property_frontages)       AS frontages,
  (SELECT count(*) FROM property_service_zones)   AS service_zones,
  (SELECT count(*) FROM property_photos)          AS photos;

SELECT unit_type, count(*) AS units FROM administrative_units GROUP BY 1
 ORDER BY CASE unit_type WHEN 'CITY' THEN 0 WHEN 'ZONE' THEN 1
                         WHEN 'WARD' THEN 2 ELSE 3 END;
SQL

# ---------------------------------------------------------------------
step "5/6  Restart the backend"
# ---------------------------------------------------------------------
if [ "${SKIP_BACKEND:-0}" = "1" ]; then
  warn "SKIP_BACKEND=1 - leaving the backend alone"
elif [ ! -x "$ROOT/.venv/bin/uvicorn" ]; then
  warn "no .venv - run ./scripts/setup_python_env.sh first; skipping restart"
else
  if wq_start_backend "$ROOT" "$API_HOST" "$API_PORT" "$LOGS" "$PHOTO_DIR"; then
    ok "backend running on $API (version $WQ_EXPECTED_VERSION)"
  else
    die "backend did not come up cleanly (see $LOGS/backend.log)"
  fi
fi

# ---------------------------------------------------------------------
step "6/6  Verification"
# ---------------------------------------------------------------------
VRC=0
if [ "${SKIP_VERIFY:-0}" = "1" ]; then
  warn "SKIP_VERIFY=1 - skipping ./scripts/verify_demo.sh"
else
  DB_NAME="$DB_NAME" API="$API" DEMO_ROUTE_ID="$DEMO_ROUTE" \
    bash "$ROOT/scripts/verify_demo.sh" > "$LOGS/verify_after_simplify.log" 2>&1
  VRC=$?
  cat "$LOGS/verify_after_simplify.log"
fi

echo
echo "======================================================================"
if [ $VRC -eq 0 ]; then printf "\033[32mSIMPLIFIED - REAL PILOT DATA ONLY\033[0m\n"
else printf "\033[31mVERIFICATION FAILED (exit %d)\033[0m\n" "$VRC"; fi
echo "======================================================================"
echo "City survey overview : $API/survey"
echo "Field survey         : $API/survey/field"
echo "Review queue         : $API/survey/review"
echo "Live lane operations : $API/dashboard"
echo "Backup               : $BACKUP"
echo "Rollback             : psql -d $DB_NAME -f $LOGS/backup_before_simplify_latest.sql"
echo "                       (after dropdb/createdb + CREATE EXTENSION postgis)"
echo "finished             : $(date)"
exit $VRC

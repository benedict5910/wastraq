#!/usr/bin/env bash
# =====================================================================
# Install the city-scale survey module and the rebuilt dashboards.
#
#   ./scripts/upgrade_dashboards.sh
#
# Options (environment):
#   DB_NAME=wastraq_demo
#   PHOTO_DIR=~/properties      where PROP-001.jpg ... PROP-016.jpg live
#   API=http://127.0.0.1:8000
#   SKIP_BACKEND=1              don't stop/start the backend
#   SKIP_VERIFY=1               don't run scripts/verify_demo.sh at the end
#   SKIP_SEED=1                 apply the schema but not the demo city data
#   FORCE_DB=1                  re-apply schema + seed even if already migrated
#   WQ_PYTHON=/path/to/python3.11   use exactly this interpreter
#   REBUILD_VENV=1              rebuild .venv even if it is already correct
#
# What it does, in order:
#   1. locate PostgreSQL, check the server, database, PostGIS and base tables
#   2. back up everything the migration can touch, to logs/
#   3. apply database/survey_schema.sql   (additive + idempotent)
#      and database/property_master.sql  (additive + idempotent)
#   4. apply database/survey_seed.sql     (idempotent; asserts the 16
#      demo-lane properties are untouched and rolls back if they are not)
#   5. build .venv on a supported Python (3.11) and install dependencies
#   6. restart the backend and confirm it is serving the NEW code
#   7. run scripts/verify_demo.sh
#
# It is safe to run more than once. It never drops the database, never
# drops a table, and never edits the 16 real lane properties: every
# statement it applies is additive.
#
# Re-running is the intended recovery path. If the demonstration city data
# is already seeded, step 4 is skipped entirely - no re-seed, no rewrite.
# The SCHEMA still runs every time: it is additive and idempotent, and later
# releases add columns the backend needs. FORCE_DB=1 forces both.
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
MAIN_LOG="$LOGS/upgrade_dashboards_${STAMP}.log"
exec > >(tee "$MAIN_LOG") 2>&1

step() { printf "\n\033[1m=== %s\033[0m\n" "$*"; }
ok()   { printf "  \033[32mok\033[0m    %s\n" "$*"; }
warn() { printf "  \033[33mwarn\033[0m  %s\n" "$*"; }
die()  { printf "  \033[31mFAIL\033[0m  %s\n" "$*"; printf "\nStopped. Log: %s\n" "$MAIN_LOG"; exit 1; }

echo "Wastraq - upgrade dashboards + install the city survey module"
echo "project   : $ROOT"
echo "database  : $DB_NAME"
echo "photo dir : $PHOTO_DIR"
echo "demo route: $DEMO_ROUTE  (the live lane demo stays scoped to this)"
echo "started   : $(date)"

# ---------------------------------------------------------------------
step "1/7  PostgreSQL + PostGIS + base tables"
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
PGIS="$(psql -tA -d "$DB_NAME" -c 'SELECT PostGIS_Version();' 2>/dev/null)"
[ -n "$PGIS" ] || die "PostGIS is not enabled in $DB_NAME (CREATE EXTENSION postgis;)"
ok "database $DB_NAME with PostGIS $PGIS"

for t in properties property_entrances property_frontages property_service_zones \
         property_photos pickers collection_events evidence; do
  psql -tA -d "$DB_NAME" -c "SELECT to_regclass('public.$t');" | grep -q "$t" \
    || die "table '$t' is missing - run ./scripts/finish_setup.sh and ./scripts/load_real_lane.sh first"
done
ok "all base tables present"

# Resolve the interpreter now, before any database work. If this machine has
# no supported Python, that is a two-second failure - much better than finding
# out after a backup and a seed have already run.
# shellcheck disable=SC1091
source "$ROOT/scripts/py_env.sh"
if wq_resolve_python; then
  ok "Python $WQ_PY_VER available at $WQ_PY"
else
  warn "no supported Python on PATH yet - step 5 will try to install one"
fi

LANE_BEFORE="$(psql -tA -d "$DB_NAME" -c "SELECT count(*) FROM properties WHERE route_id='$DEMO_ROUTE';")"
[ "${LANE_BEFORE:-0}" -eq 16 ] \
  || die "expected 16 properties on $DEMO_ROUTE, found ${LANE_BEFORE:-0} - run ./scripts/load_real_lane.sh first"
ok "the 16-property demo lane is loaded (will be left exactly as it is)"

# ---------------------------------------------------------------------
step "2/7  Is the migration already applied?"
# ---------------------------------------------------------------------
# Re-running the schema and seed is safe (both are idempotent and both run
# in a transaction), but "safe" is not the same as "necessary". If the
# migration already landed, the right move is to leave it completely alone
# and go fix whatever failed further down.
HAVE_TABLES="$(psql -tA -d "$DB_NAME" -c "SELECT count(*) FROM information_schema.tables
      WHERE table_name IN ('administrative_units','survey_users','survey_assignments',
                           'property_surveys','property_geometry_history','property_qa_issues');")"
HAVE_CITY="$(psql -tA -d "$DB_NAME" -c "SELECT count(*) FROM properties
      WHERE route_id IS DISTINCT FROM '$DEMO_ROUTE';" 2>/dev/null)"

# Two different questions, and conflating them was a mistake worth not
# repeating:
#   SEED_WORK - is the demonstration city data already loaded? If so, leave it
#               alone. Re-seeding rewrites nothing useful and risks a lot.
#   SCHEMA_WORK - is the schema at the version this code expects? The schema
#               file is additive and idempotent, so it ALWAYS runs: later
#               releases add columns, and skipping it would leave the backend
#               querying columns that do not exist yet.
SEED_WORK=1
SCHEMA_WORK=1
if [ "${FORCE_DB:-0}" = "1" ]; then
  warn "FORCE_DB=1 - re-applying the schema and re-seeding even though they may be current"
elif [ "${HAVE_TABLES:-0}" -eq 6 ] && [ "${HAVE_CITY:-0}" -ge 500 ]; then
  SEED_WORK=0
  ok "already seeded: 6 survey tables, ${HAVE_CITY} city properties"
  ok "the seed will be skipped entirely (FORCE_DB=1 to override)"
  ok "the schema still runs - it is additive and idempotent, and later"
  ok "  releases add columns the backend needs"
fi
DB_WORK="$SEED_WORK"

BACKUP="(none - the database was not modified)"
if [ "$SCHEMA_WORK" = "1" ]; then
  BACKUP="$LOGS/backup_before_survey_${STAMP}.sql"
  command -v pg_dump >/dev/null 2>&1 || die "pg_dump not found on PATH"
  pg_dump -d "$DB_NAME" --column-inserts > "$BACKUP" 2>"$LOGS/pg_dump_survey.err" \
    && ok "full backup -> $(basename "$BACKUP") ($(wc -l < "$BACKUP" | tr -d ' ') lines)" \
    || { cat "$LOGS/pg_dump_survey.err"; die "pg_dump failed"; }
  cp "$BACKUP" "$LOGS/backup_before_survey_latest.sql"
fi

# ---------------------------------------------------------------------
step "3/7  Survey schema"
# ---------------------------------------------------------------------
if [ "$SCHEMA_WORK" = "1" ]; then
  [ -f "$ROOT/database/survey_schema.sql" ] || die "database/survey_schema.sql not found"
  psql -v ON_ERROR_STOP=1 -d "$DB_NAME" -f "$ROOT/database/survey_schema.sql" \
       > "$LOGS/psql_survey_schema.log" 2>&1 \
    || { echo "----- psql output -----"; tail -40 "$LOGS/psql_survey_schema.log"; \
         die "survey_schema.sql failed - the transaction rolled back, nothing changed"; }
  grep -i "NOTICE" "$LOGS/psql_survey_schema.log" | sed 's/^/  /' | head -20
  ok "survey schema applied (additive; existing objects were left alone)"

  # The property master layer rides along: it is additive and idempotent too,
  # and leaving it to a separate script is how a backend ends up querying a
  # column that only exists in the source tree.
  if [ -f "$ROOT/database/property_master.sql" ]; then
    psql -v ON_ERROR_STOP=1 -d "$DB_NAME" -f "$ROOT/database/property_master.sql" \
         > "$LOGS/psql_property_master.log" 2>&1 \
      || { echo "----- psql output -----"; tail -40 "$LOGS/psql_property_master.log"; \
           die "property_master.sql failed - the transaction rolled back, nothing changed"; }
    ok "property master schema applied"
  else
    ok "property master schema not present in this checkout - skipped"
  fi
else
  ok "schema step skipped"
fi

# The field-survey workflow needs these columns. Checking here means a stale
# schema is caught now, by name, rather than as a 500 from a query later.
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
[ -z "$MISSING_COLS" ] || die "schema is behind: missing columns $MISSING_COLS"
ok "field-workflow columns present on properties and property_photos"

NEWT="$(psql -tA -d "$DB_NAME" -c "SELECT count(*) FROM information_schema.tables
        WHERE table_name IN ('administrative_units','survey_users','survey_assignments',
                             'property_surveys','property_geometry_history','property_qa_issues');")"
[ "${NEWT:-0}" -eq 6 ] || die "expected 6 survey tables, found ${NEWT:-0}"
ok "6 survey tables present"

# ---------------------------------------------------------------------
step "4/7  Demonstration city data"
# ---------------------------------------------------------------------
if [ "$SEED_WORK" = "0" ]; then
  ok "city data already seeded - skipped"
elif [ "${SKIP_SEED:-0}" = "1" ]; then
  warn "SKIP_SEED=1 - schema installed, no city data loaded"
else
  [ -f "$ROOT/database/survey_seed.sql" ] || die "database/survey_seed.sql not found"
  APPLY="$LOGS/survey_seed_applied_${STAMP}.sql"
  sed "s|__PHOTO_DIR__|${PHOTO_DIR}|g" "$ROOT/database/survey_seed.sql" > "$APPLY"
  grep -q "__PHOTO_DIR__" "$APPLY" && die "photo path substitution failed"

  # The seed asserts, inside its own transaction, that the 16 demo-lane rows
  # are still there and still 16. If they are not, it raises and rolls back.
  psql -v ON_ERROR_STOP=1 -d "$DB_NAME" -f "$APPLY" > "$LOGS/psql_survey_seed.log" 2>&1 \
    || { echo "----- psql output -----"; tail -40 "$LOGS/psql_survey_seed.log"; \
         die "survey_seed.sql failed - rolled back, the database is as it was"; }
  grep -i "NOTICE" "$LOGS/psql_survey_seed.log" | sed 's/^/  /' | head -30
  ok "city survey data seeded (idempotent: re-running changes nothing)"
fi

# Checked whether or not we wrote anything: this is the invariant the whole
# design rests on, so it is worth re-asserting on every run.
LANE_AFTER="$(psql -tA -d "$DB_NAME" -c "SELECT count(*) FROM properties WHERE route_id='$DEMO_ROUTE';")"
[ "${LANE_AFTER:-0}" -eq "$LANE_BEFORE" ] \
  || die "the demo lane changed from $LANE_BEFORE to ${LANE_AFTER:-0} properties - restore $BACKUP"
ok "demo lane still has $LANE_AFTER properties, untouched"

psql -d "$DB_NAME" <<SQL
\\pset border 2
SELECT
  (SELECT count(*) FROM administrative_units)                     AS admin_units,
  (SELECT count(*) FROM survey_users)                             AS users,
  (SELECT count(*) FROM survey_assignments)                       AS assignments,
  (SELECT count(*) FROM properties)                               AS properties_total,
  (SELECT count(*) FROM properties WHERE route_id='$DEMO_ROUTE')  AS demo_lane,
  (SELECT count(*) FROM property_surveys)                         AS surveys,
  (SELECT count(*) FROM property_qa_issues WHERE status='OPEN')   AS open_qa;

SELECT survey_status, count(*) FROM property_surveys GROUP BY 1 ORDER BY 2 DESC;
SQL

# ---------------------------------------------------------------------
step "5/7  Python environment"
# ---------------------------------------------------------------------
# Delegated, because getting this wrong is what broke the last run: the
# venv had been built on whatever `python3` happened to be (3.14), for
# which psycopg-binary and numpy publish no wheels yet. pip then aborted
# the WHOLE resolution on the first unsatisfiable pin, python-multipart
# was never installed, and FastAPI refused to start - a database problem
# that was never a database problem.
#
# setup_python_env.sh pins the interpreter, rebuilds .venv when it is on
# the wrong Python, installs, and then imports app.main to prove every
# route actually builds.
REBUILD_ARG=""
[ "${REBUILD_VENV:-0}" = "1" ] && REBUILD_ARG="--force"

if bash "$ROOT/scripts/setup_python_env.sh" $REBUILD_ARG; then
  ok "Python environment ready"
else
  die "could not build a working Python environment - see the messages above"
fi

PY="$ROOT/.venv/bin/python"
echo "  venv python: $("$PY" -V 2>&1)"

mkdir -p "$PHOTO_DIR/survey-uploads" 2>/dev/null \
  && ok "photo upload directory ready: $PHOTO_DIR/survey-uploads" \
  || warn "could not create $PHOTO_DIR/survey-uploads"

# ---------------------------------------------------------------------
step "6/7  Restart the backend on the new code"
# ---------------------------------------------------------------------
if [ "${SKIP_BACKEND:-0}" = "1" ]; then
  warn "SKIP_BACKEND=1 - leaving the backend alone"
elif [ ! -x "$ROOT/.venv/bin/uvicorn" ]; then
  warn "no .venv - run ./scripts/finish_setup.sh first; skipping backend start"
else
  # Stops by pidfile AND by port, waits for the port to be genuinely free,
  # then refuses to continue unless the process answering reports the
  # current version. A stale uvicorn cannot quietly keep serving old code.
  if wq_start_backend "$ROOT" "$API_HOST" "$API_PORT" "$LOGS" "$PHOTO_DIR"; then
    ok "backend running on $API (pid $(cat "$LOGS/backend.pid"), version $WQ_EXPECTED_VERSION)"
    echo "  /health/db -> $(curl -sf "$API/health/db")"
  else
    die "backend did not come up cleanly on $API (see $LOGS/backend.log)"
  fi
fi

# ---------------------------------------------------------------------
step "7/7  Verification"
# ---------------------------------------------------------------------
VRC=0
if [ "${SKIP_VERIFY:-0}" = "1" ]; then
  warn "SKIP_VERIFY=1 - skipping ./scripts/verify_demo.sh"
else
  DB_NAME="$DB_NAME" API="$API" DEMO_ROUTE_ID="$DEMO_ROUTE" \
    bash "$ROOT/scripts/verify_demo.sh" > "$LOGS/verify_after_upgrade.log" 2>&1
  VRC=$?
  cat "$LOGS/verify_after_upgrade.log"
fi

echo
echo "======================================================================"
if [ $VRC -eq 0 ]; then printf "\033[32mDASHBOARDS UPGRADED AND VERIFIED\033[0m\n"
else printf "\033[31mVERIFICATION FAILED (exit %d)\033[0m\n" "$VRC"; fi
echo "======================================================================"
echo "Operations dashboard : $API/dashboard"
echo "Survey overview      : $API/survey"
echo "Survey map           : $API/survey/map"
echo "Assignments          : $API/survey/assignments"
echo "Field survey         : $API/survey/field"
echo "Review queue         : $API/survey/review"
echo "GIS QA               : $API/survey/qa"
echo "Surveyors            : $API/survey/surveyors"
echo "API docs             : $API/docs"
echo "Python               : $("$ROOT/.venv/bin/python" -V 2>&1)"
echo "Backup               : $BACKUP"
echo "Logs                 : $LOGS/"
echo "Rollback             : dropdb $DB_NAME && createdb $DB_NAME && \\"
echo "                       psql -d $DB_NAME -c 'CREATE EXTENSION postgis;' && \\"
echo "                       psql -d $DB_NAME -f $LOGS/backup_before_survey_latest.sql"
echo "finished             : $(date)"
exit $VRC

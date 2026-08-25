#!/usr/bin/env bash
# =====================================================================
# Add the Property Registration / Property Master dashboard.
#
#   ./scripts/add_property_master_dashboard.sh
#
# Options (environment):
#   DB_NAME=wastraq_demo
#   API=http://127.0.0.1:8000
#   SKIP_BACKEND=1     don't stop/start the backend
#   SKIP_VERIFY=1      don't run scripts/verify_demo.sh at the end
#   DRY_RUN=1          report only - no schema, no writes
#
# What it does:
#   1. check PostgreSQL, PostGIS and that the 16-property lane is loaded
#   2. back up the whole database to logs/
#   3. apply database/survey_schema.sql     (additive + idempotent)
#   4. apply database/property_master.sql   (additive + idempotent)
#   5. reconcile verification_status with the review record (idempotent)
#   6. restart the backend on the current code
#   7. run scripts/verify_demo.sh
#
# Both schema files are additive: they add nullable columns, widen CHECK
# vocabularies and create one table and one view. Nothing is dropped,
# truncated, rewritten or reseeded. Existing values - including the legacy
# property types the pilot rows use - stay valid, which is why no data
# migration is needed and none is performed.
#
# It NEVER seeds a property. The Property Master is populated by real
# registrations; a dashboard that looks full because someone generated
# 1,300 fake rows is worse than an honest one showing 16.
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
MAIN_LOG="$LOGS/add_property_master_${STAMP}.log"
exec > >(tee "$MAIN_LOG") 2>&1

step() { printf "\n\033[1m=== %s\033[0m\n" "$*"; }
ok()   { printf "  \033[32mok\033[0m    %s\n" "$*"; }
warn() { printf "  \033[33mwarn\033[0m  %s\n" "$*"; }
die()  { printf "  \033[31mFAIL\033[0m  %s\n" "$*"; printf "\nStopped. Log: %s\n" "$MAIN_LOG"; exit 1; }

echo "Wastraq - add the Property Registration / Property Master dashboard"
echo "project   : $ROOT"
echo "database  : $DB_NAME"
echo "pilot lane: $DEMO_ROUTE"
echo "started   : $(date)"

# ---------------------------------------------------------------------
step "1/7  Preconditions"
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

for f in database/survey_schema.sql database/property_master.sql \
         database/reconcile_verification_status.sql \
         backend/app/property_master.py backend/app/routes/property_registry.py \
         backend/app/static/property-registration.html \
         scripts/test_property_master.py; do
  [ -f "$ROOT/$f" ] || die "$f not found - the code half of this change is missing"
done
ok "registration code, page, migration and test are all present"

BACKUP="$LOGS/backup_before_property_master_${STAMP}.sql"
command -v pg_dump >/dev/null 2>&1 || die "pg_dump not found on PATH"
pg_dump -d "$DB_NAME" --column-inserts > "$BACKUP" 2>"$LOGS/pg_dump_property_master.err" \
  && ok "full backup -> $(basename "$BACKUP") ($(wc -l < "$BACKUP" | tr -d ' ') lines)" \
  || { cat "$LOGS/pg_dump_property_master.err"; die "pg_dump failed"; }
cp "$BACKUP" "$LOGS/backup_before_property_master_latest.sql"

if [ "${DRY_RUN:-0}" = "1" ]; then
  psql -d "$DB_NAME" <<SQL
\\pset border 2
SELECT
  (SELECT count(*) FROM properties)                              AS properties_total,
  (SELECT count(*) FROM properties WHERE route_id='$DEMO_ROUTE') AS pilot_lane,
  (SELECT count(*) FROM information_schema.columns
    WHERE table_name='properties' AND column_name='captured_latitude') AS has_capture_cols,
  (SELECT count(*) FROM information_schema.views
    WHERE table_name='v_property_master')                        AS has_master_view;
SQL
  warn "DRY_RUN=1 - nothing was changed (no schema applied)"
  exit 0
fi

# ---------------------------------------------------------------------
step "2/7  Survey schema (prerequisite)"
# ---------------------------------------------------------------------
# property_master.sql assumes the survey layer's columns exist. Applying it
# here rather than assuming it was applied earlier is what stops an endpoint
# 500ing three layers away on a column that only exists in the source tree.
psql -v ON_ERROR_STOP=1 -d "$DB_NAME" -f "$ROOT/database/survey_schema.sql" \
     > "$LOGS/psql_survey_schema_${STAMP}.log" 2>&1 \
  || { echo "----- psql output -----"; tail -30 "$LOGS/psql_survey_schema_${STAMP}.log"; \
       die "survey_schema.sql failed - rolled back, nothing changed"; }
ok "survey schema applied (existing objects left alone)"

# ---------------------------------------------------------------------
step "3/7  Property master schema"
# ---------------------------------------------------------------------
psql -v ON_ERROR_STOP=1 -d "$DB_NAME" -f "$ROOT/database/property_master.sql" \
     > "$LOGS/psql_property_master_${STAMP}.log" 2>&1 \
  || { echo "----- psql output -----"; tail -30 "$LOGS/psql_property_master_${STAMP}.log"; \
       die "property_master.sql failed - rolled back, nothing changed"; }
ok "property master schema applied"

# Named checks. A stale schema has to fail HERE, by column name, rather than
# later as a 500 from an endpoint nobody has connected to the migration yet.
MISSING="$(psql -tA -d "$DB_NAME" -c "
  SELECT string_agg(x, ', ') FROM (
    SELECT c AS x FROM unnest(ARRAY['captured_latitude','captured_longitude',
        'captured_accuracy_m','captured_at','location_source','created_by',
        'updated_by','active','inactive_reason']) c
     WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name='properties' AND column_name=c)
    UNION ALL
    SELECT 'table property_change_log' WHERE NOT EXISTS
      (SELECT 1 FROM information_schema.tables WHERE table_name='property_change_log')
    UNION ALL
    SELECT 'view v_property_master' WHERE NOT EXISTS
      (SELECT 1 FROM information_schema.views WHERE table_name='v_property_master')
  ) y;")"
[ -z "$MISSING" ] || die "schema is still behind: missing $MISSING"
ok "registration columns, audit table and master view are all present"

# The widened vocabularies must still accept everything the pilot rows use.
BADVOCAB="$(psql -tA -d "$DB_NAME" -c "
  SELECT count(*) FROM properties p
   WHERE p.property_type IS NULL
      OR p.property_type NOT IN ('INDEPENDENT_HOUSE','APARTMENT','SHOP',
         'COMMERCIAL_BUILDING','OFFICE','SCHOOL','HOSPITAL','HOTEL','MARKET',
         'GATED_COMMUNITY','INDUSTRIAL','VACANT_PROPERTY','OTHER',
         'RESIDENTIAL','COMMERCIAL','MIXED','INSTITUTIONAL','VACANT');")"
[ "${BADVOCAB:-1}" -eq 0 ] \
  || die "$BADVOCAB propert(ies) hold a property_type outside the widened vocabulary"
ok "every existing property is still valid under the widened vocabulary"

psql -d "$DB_NAME" <<SQL
\\pset border 2
SELECT
  (SELECT count(*) FROM properties)                              AS properties_total,
  (SELECT count(*) FROM properties WHERE route_id='$DEMO_ROUTE') AS pilot_lane,
  (SELECT count(*) FROM properties WHERE property_id ~ '^PROP-[0-9]{5}\$') AS synthetic,
  (SELECT count(*) FROM v_property_master)                       AS master_rows,
  (SELECT count(*) FROM properties WHERE verification_status='PENDING_SURVEY')
                                                                 AS pending_survey,
  (SELECT count(*) FROM property_change_log)                     AS change_log_rows;

SELECT property_type, count(*) AS properties FROM properties GROUP BY 1 ORDER BY 2 DESC;
SQL

# ---------------------------------------------------------------------
step "4/7  Nothing was seeded"
# ---------------------------------------------------------------------
LANE_AFTER="$(psql -tA -d "$DB_NAME" -c "SELECT count(*) FROM properties WHERE route_id='$DEMO_ROUTE';")"
[ "${LANE_AFTER:-0}" -eq 16 ] || die "the pilot lane changed - restore $BACKUP"
ok "pilot lane still holds $LANE_AFTER properties"

SYNTH="$(psql -tA -d "$DB_NAME" -c \
  "SELECT count(*) FROM properties WHERE property_id ~ '^PROP-[0-9]{5}\$';")"
[ "${SYNTH:-1}" -eq 0 ] || die "$SYNTH synthetic city properties are present"
ok "no synthetic city properties were introduced"

GEOM="$(psql -tA -d "$DB_NAME" -c "
  SELECT (SELECT count(*) FROM property_entrances)     || '/' ||
         (SELECT count(*) FROM property_frontages)     || '/' ||
         (SELECT count(*) FROM property_service_zones) || '/' ||
         (SELECT count(*) FROM property_photos);")"
ok "geometry and photos untouched (entrances/frontages/zones/photos: $GEOM)"

# ---------------------------------------------------------------------
step "5/7  Reconcile verification_status with the review record"
# ---------------------------------------------------------------------
# The 16 pilot surveys were written directly by the seed - APPROVED, with a
# reviewer id, a reviewed_at and review_status APPROVED - but the seed never
# performed review_survey()'s SECOND write, so the property rows stayed at
# FIELD_SURVEYED. The database has been telling two stories about the same
# property since the lane was first loaded. This applies the missing write,
# driven entirely by approval records already in the database. It promotes
# nothing that lacks its own reviewer and reviewed_at, and it never demotes.
VBEFORE="$(psql -tA -d "$DB_NAME" -c "SELECT verification_status || ' x' ||
             count(*) FROM properties WHERE route_id='$DEMO_ROUTE'
             GROUP BY verification_status ORDER BY 1;" | paste -sd', ' -)"
ok "before: ${VBEFORE:-none}"

psql -v ON_ERROR_STOP=1 -d "$DB_NAME" \
     -f "$ROOT/database/reconcile_verification_status.sql" \
     > "$LOGS/psql_reconcile_${STAMP}.log" 2>&1 \
  || { echo "----- psql output -----"; tail -30 "$LOGS/psql_reconcile_${STAMP}.log"; \
       die "reconcile_verification_status.sql failed - rolled back, nothing changed"; }
grep -i "NOTICE" "$LOGS/psql_reconcile_${STAMP}.log" | sed 's/^[^ ]* //' | sed 's/^/  /'

VAFTER="$(psql -tA -d "$DB_NAME" -c "SELECT verification_status || ' x' ||
            count(*) FROM properties WHERE route_id='$DEMO_ROUTE'
            GROUP BY verification_status ORDER BY 1;" | paste -sd', ' -)"
ok "after:  ${VAFTER:-none}"

# Nothing may be verified for operation without an approved, reviewed survey.
UNBACKED="$(psql -tA -d "$DB_NAME" -c "
  SELECT count(*) FROM properties p
  LEFT JOIN v_property_current_survey s ON s.property_id = p.property_id
  WHERE p.verification_status = 'VERIFIED_FOR_OPERATION'
    AND (s.survey_status IS DISTINCT FROM 'APPROVED' OR s.reviewed_at IS NULL);")"
[ "${UNBACKED:-1}" -eq 0 ] \
  || die "$UNBACKED property(ies) are VERIFIED_FOR_OPERATION with no approved survey"
ok "every verified property is backed by its own approved, reviewed survey"

LANE_GEOM="$(psql -tA -d "$DB_NAME" -c "
  SELECT count(*) FROM v_property_master
   WHERE route_id='$DEMO_ROUTE' AND has_entrance AND has_frontage
     AND has_service_zone AND has_frontage_photo;")"
ok "$LANE_GEOM pilot properties still carry entrance, frontage, zone and photo"

# ---------------------------------------------------------------------
step "6/7  Restart the backend"
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
step "7/7  Verification"
# ---------------------------------------------------------------------
VRC=0
if [ "${SKIP_VERIFY:-0}" = "1" ]; then
  warn "SKIP_VERIFY=1 - skipping ./scripts/verify_demo.sh"
else
  DB_NAME="$DB_NAME" API="$API" DEMO_ROUTE_ID="$DEMO_ROUTE" \
    bash "$ROOT/scripts/verify_demo.sh" > "$LOGS/verify_after_property_master.log" 2>&1
  VRC=$?
  cat "$LOGS/verify_after_property_master.log"
fi

echo
echo "======================================================================"
if [ $VRC -eq 0 ]; then printf "\033[32mPROPERTY MASTER INSTALLED\033[0m\n"
else printf "\033[31mVERIFICATION FAILED (exit %d)\033[0m\n" "$VRC"; fi
echo "======================================================================"
echo "Property master      : $API/property-registration"
echo "Field survey         : $API/survey/field"
echo "Review queue         : $API/survey/review"
echo "Live lane operations : $API/dashboard"
echo "Backup               : $BACKUP"
echo "Rollback             : psql -d $DB_NAME -f $LOGS/backup_before_property_master_latest.sql"
echo "                       (after dropdb/createdb + CREATE EXTENSION postgis)"
echo "finished             : $(date)"
exit $VRC

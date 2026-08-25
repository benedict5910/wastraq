#!/usr/bin/env bash
# =====================================================================
# Testing checklist, automated.
#   ./scripts/verify_demo.sh
#
# Expects the REAL 16-property lane (2nd Cross Road, Krishnamurthy Puram).
# Test coordinates come from scripts/real_lane_testpoints.env, which is
# regenerated alongside the geometry by scripts/generate_real_lane.py - so
# moving a property never leaves a stale coordinate hard-coded in here.
#
# Backend must be running (./scripts/run_backend.sh) for the API stages.
# =====================================================================
# `set -u` is deliberate: a typo in a variable name should stop the run, not
# silently compare against an empty string and report a green PASS. The cost is
# that every OPTIONAL variable has to be given a default explicitly - see
# `wq_default` below and the "0. Configuration" section, which is where all of
# them are resolved before anything reads them.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_NAME="${DB_NAME:-wastraq_demo}"
API="${API:-http://127.0.0.1:8000}"

# Set a variable only if it is unset or empty. Safe under `set -u` (the
# indirect read uses :- itself), and it keeps "optional with a default" and
# "required, must be resolved" visibly different in the source.
wq_default() {  # <VAR_NAME> <default-value>
  local name="$1" dflt="${2-}" cur
  eval "cur=\${$name:-}"
  if [ -z "$cur" ]; then
    eval "$name=\$dflt"
  fi
  eval "export $name"
}

# Fail with a readable message instead of a bare "unbound variable" abort.
wq_require() {  # <VAR_NAME> <what it is>
  local name="$1" cur
  eval "cur=\${$name:-}"
  if [ -z "$cur" ]; then
    printf "\033[31mCONFIG ERROR\033[0m  %s (%s) could not be resolved.\n" "$name" "${2:-required}" >&2
    printf "  Set it explicitly, e.g.  %s=... ./scripts/verify_demo.sh\n" "$name" >&2
    exit 2
  fi
}

# Put the right Homebrew PostgreSQL client on PATH (17, then 16, then 15).
# shellcheck disable=SC1091
source "$ROOT/scripts/pg_env.sh"
wq_resolve_pg || { echo "psql not found - see scripts/pg_env.sh"; exit 1; }

# Generated expectations (coordinates + property count).
TP="$ROOT/scripts/real_lane_testpoints.env"
if [ -f "$TP" ]; then
  # shellcheck disable=SC1090
  source "$TP"
else
  echo "WARNING: $TP missing - run  python3 scripts/generate_real_lane.py"
fi
wq_default N "${EXPECTED_PROPERTIES:-16}"

# The expected backend version is READ FROM THE SOURCE, never typed here.
# It used to be a literal, which meant bumping backend/app/__init__.py and
# forgetting this line produced a confusing "stale process" failure against a
# backend that was perfectly current.
WQ_SOURCE_VERSION="$(sed -n 's/^__version__ *= *"\(.*\)"/\1/p' \
                     "$ROOT/backend/app/__init__.py" 2>/dev/null | head -1)"
wq_default WQ_EXPECTED_VERSION "$WQ_SOURCE_VERSION"
wq_default WQ_EXPECTED_VERSION "0.4.0"   # only if the source could not be read

# Use the project virtualenv's interpreter, not whatever `python3` happens to
# be on PATH. The scripts below import numpy and psycopg, which live in .venv -
# and the system python3 on a Mac can be a version those wheels do not exist
# for at all. Running them with the wrong interpreter produces failures that
# have nothing to do with the thing being verified.
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

PASS=0; FAIL=0
ok()   { printf "  \033[32mPASS\033[0m  %s\n" "$1"; PASS=$((PASS+1)); }
bad()  { printf "  \033[31mFAIL\033[0m  %s\n" "$1"; FAIL=$((FAIL+1)); }
head_() { printf "\n\033[1m%s\033[0m\n" "$1"; }

q() { psql -tA -d "$DB_NAME" -c "$1" 2>/dev/null; }

check_eq() { # description expected actual
  if [ "$2" = "$3" ]; then ok "$1 ($3)"; else bad "$1 - expected '$2', got '$3'"; fi
}

# --- API helpers -------------------------------------------------------------
# JSON bodies are built inside Python, never assembled from backslash-escaped
# quotes inside $( ) - that construct is parsed differently by bash 3.2 (the
# /bin/bash macOS ships) and silently produced malformed bodies, which FastAPI
# then rejected with 422. These helpers also surface the server's error text
# instead of dying on an empty response and a JSONDecodeError traceback.
api_json() {   # <method> <path> <json-or-empty> <python-expression-on `d`>
  API="$API" "$PY" - "$1" "$2" "$3" "$4" <<'WQEOF' 2>/dev/null
import json, os, sys, urllib.error, urllib.request
method, path, body, expr = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
url = os.environ["API"].rstrip("/") + path
data = body.encode() if body else None
req = urllib.request.Request(url, data=data, method=method,
                             headers={"Content-Type": "application/json"} if data else {})
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode() or "null")
except urllib.error.HTTPError as e:
    print("HTTP %d: %s" % (e.code, e.read().decode(errors="replace")[:200]))
    sys.exit(0)
except Exception as e:
    print("ERROR: %s: %s" % (type(e).__name__, e))
    sys.exit(0)
try:
    print(eval(expr))
except Exception as e:
    print("BADSHAPE: %s: %s" % (type(e).__name__, e))
WQEOF
}

api_status() { # <method> <path> <json-or-empty>  -> HTTP status code
  API="$API" "$PY" - "$1" "$2" "$3" <<'WQEOF' 2>/dev/null
import os, sys, urllib.error, urllib.request
method, path, body = sys.argv[1], sys.argv[2], sys.argv[3]
url = os.environ["API"].rstrip("/") + path
data = body.encode() if body else None
req = urllib.request.Request(url, data=data, method=method,
                             headers={"Content-Type": "application/json"} if data else {})
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception as e:
    print("ERROR: %s: %s" % (type(e).__name__, e))
WQEOF
}

lookup_body() { # <lat> <lon>
  "$PY" -c 'import json,sys;print(json.dumps({"latitude":float(sys.argv[1]),"longitude":float(sys.argv[2])}))' "$1" "$2"
}

event_body() { # <lat> <lon> <picker_id>
  "$PY" -c 'import json,sys;print(json.dumps({"latitude":float(sys.argv[1]),"longitude":float(sys.argv[2]),"picker_id":sys.argv[3]}))' "$1" "$2" "$3"
}

# =====================================================================
# 0. Configuration
#
# Everything the checks below interpolate is resolved HERE, once, from a
# real source - never assumed into existence further down the file. The
# demo route in particular used to be a bare `$DEMO_ROUTE` that nothing
# ever assigned; under `set -u` that aborted the whole run at the first
# use, with a line number that pointed at a check rather than at the
# missing definition.
#
# Resolution order for the demo route, most authoritative first:
#   1. DEMO_ROUTE_ID in the environment      (explicit operator override)
#   2. DEMO_ROUTE_ID in backend/.env         (what the backend is configured with)
#   3. the default in backend/app/config.py  (what the backend falls back to)
#   4. the running backend's own answer      (GET /summary reports its scope)
#   5. the database                          (the route that actually holds N properties)
#   6. ROUTE-DEMO-01                         (last-resort literal)
# =====================================================================
head_ "0. Configuration"

DEMO_ROUTE_SOURCE=""
wq_default DEMO_ROUTE "${DEMO_ROUTE_ID:-}"
[ -n "$DEMO_ROUTE" ] && DEMO_ROUTE_SOURCE="DEMO_ROUTE_ID in the environment"

if [ -z "$DEMO_ROUTE" ] && [ -f "$ROOT/backend/.env" ]; then
  DEMO_ROUTE="$(sed -n 's/^[[:space:]]*DEMO_ROUTE_ID[[:space:]]*=[[:space:]]*//p' \
                "$ROOT/backend/.env" 2>/dev/null | tail -1 | tr -d '"'"'"' \r')"
  [ -n "$DEMO_ROUTE" ] && DEMO_ROUTE_SOURCE="backend/.env"
fi

if [ -z "$DEMO_ROUTE" ] && [ -f "$ROOT/backend/app/config.py" ]; then
  # DEMO_ROUTE_ID = os.getenv("DEMO_ROUTE_ID", "ROUTE-DEMO-01")
  DEMO_ROUTE="$(sed -n 's/.*getenv("DEMO_ROUTE_ID"[^"]*"\([^"]*\)".*/\1/p' \
                "$ROOT/backend/app/config.py" 2>/dev/null | head -1)"
  [ -n "$DEMO_ROUTE" ] && DEMO_ROUTE_SOURCE="backend/app/config.py"
fi

if [ -z "$DEMO_ROUTE" ] && curl -sf "$API/" >/dev/null 2>&1; then
  DEMO_ROUTE="$(api_json GET /summary '' 'd["route_id"]')"
  case "$DEMO_ROUTE" in ERROR*|None|"") DEMO_ROUTE="" ;;
                        *) DEMO_ROUTE_SOURCE="the running backend (GET /summary)" ;; esac
fi

if [ -z "$DEMO_ROUTE" ]; then
  # The route that actually carries exactly N properties, oldest first so the
  # answer is stable when a test route happens to have the same count.
  DEMO_ROUTE="$(q "SELECT route_id FROM properties WHERE route_id IS NOT NULL
                   GROUP BY route_id HAVING count(*) = $N
                   ORDER BY min(created_at), route_id LIMIT 1;")"
  [ -n "$DEMO_ROUTE" ] && DEMO_ROUTE_SOURCE="the database (the route holding $N properties)"
fi

wq_default DEMO_ROUTE "ROUTE-DEMO-01"
[ -n "$DEMO_ROUTE_SOURCE" ] || DEMO_ROUTE_SOURCE="built-in default"
wq_require DEMO_ROUTE "the collection route the lane demo is scoped to"

# One predicate, defined once, reused by every lane-scoped check below.
LANE="property_id IN (SELECT property_id FROM properties WHERE route_id = '$DEMO_ROUTE')"

ok "demo route: $DEMO_ROUTE  (from $DEMO_ROUTE_SOURCE)"
ok "expected backend version: $WQ_EXPECTED_VERSION  (from backend/app/__init__.py)"
ok "python: $("$PY" -V 2>&1)  [$PY]"

# Cross-check the resolved route against the database rather than trusting it.
ROUTE_N="$(q "SELECT count(*) FROM properties WHERE route_id = '$DEMO_ROUTE';")"
wq_default ROUTE_N "0"
if [ "$ROUTE_N" -eq "$N" ]; then
  ok "the route resolves to $ROUTE_N properties, as expected"
elif [ "$ROUTE_N" -eq 0 ]; then
  bad "route '$DEMO_ROUTE' has no properties - is the lane loaded? (./scripts/load_real_lane.sh)"
else
  bad "route '$DEMO_ROUTE' has $ROUTE_N properties, expected $N"
fi

head_ "1. Database and PostGIS"
SV="$(q 'SHOW server_version;')"
[ -n "$SV" ] && ok "PostgreSQL server $SV" || bad "cannot connect to $DB_NAME"
V="$(q 'SELECT PostGIS_Version();')"
[ -n "$V" ] && ok "PostGIS available: $V" || bad "PostGIS extension not available in $DB_NAME"

head_ "2. The real lane is loaded ($N properties)"
check_eq "$N properties on $DEMO_ROUTE" "$N" \
  "$(q "SELECT count(*) FROM properties WHERE route_id = '$DEMO_ROUTE';")"
check_eq "$N entrance points"       "$N" "$(q "SELECT count(*) FROM property_entrances WHERE $LANE;")"
check_eq "$N frontage lines"        "$N" "$(q "SELECT count(*) FROM property_frontages WHERE $LANE;")"
check_eq "$N service zones"         "$N" "$(q "SELECT count(*) FROM property_service_zones WHERE $LANE;")"
check_eq "$N frontage photos linked" "$N" \
  "$(q "SELECT count(*) FROM property_photos ph WHERE ph.photo_type='FRONTAGE' AND ph.survey_id IS NULL
        AND ph.$LANE;")"
check_eq "2 pickers"                "2"  "$(q 'SELECT count(*) FROM pickers;')"

# every id present, no gaps
MISSING="$(q "SELECT string_agg(id,',') FROM (
    SELECT 'PROP-'||lpad(g::text,3,'0') AS id FROM generate_series(1,$N) g
  ) w WHERE NOT EXISTS (SELECT 1 FROM properties p WHERE p.property_id = w.id);")"
[ -z "$MISSING" ] && ok "PROP-001..PROP-$(printf '%03d' "$N") all present" \
                 || bad "missing property ids: $MISSING"

# exactly one of each geometry per property
for pair in "property_entrances:entrance" "property_frontages:frontage" "property_service_zones:service zone"; do
  tbl="${pair%%:*}"; label="${pair##*:}"
  n_bad="$(q "SELECT count(*) FROM (
      SELECT p.property_id, count(g.*) c FROM properties p
      LEFT JOIN $tbl g ON g.property_id = p.property_id
      WHERE p.route_id = '$DEMO_ROUTE'
      GROUP BY p.property_id HAVING count(g.*) <> 1) x;")"
  check_eq "every property has exactly one $label" "0" "$n_bad"
done
check_eq "every property has exactly one frontage photo" "0" \
  "$(q "SELECT count(*) FROM (
       SELECT p.property_id FROM properties p
       LEFT JOIN property_photos ph ON ph.property_id = p.property_id
            AND ph.photo_type='FRONTAGE' AND ph.survey_id IS NULL
       WHERE p.route_id = '$DEMO_ROUTE'
       GROUP BY p.property_id HAVING count(ph.*) <> 1) x;")"
check_eq "photo filenames match their property id" "0" \
  "$(q "SELECT count(*) FROM property_photos
        WHERE photo_type='FRONTAGE' AND survey_id IS NULL AND $LANE
          AND file_path NOT LIKE '%'||property_id||'.jpg';")"

head_ "3. Geometry integrity"
check_eq "GiST index on zones"      "1"  "$(q "SELECT count(*) FROM pg_indexes WHERE tablename='property_service_zones' AND indexdef ILIKE '%gist%';")"
check_eq "GiST index on entrances"  "1"  "$(q "SELECT count(*) FROM pg_indexes WHERE tablename='property_entrances' AND indexdef ILIKE '%gist%';")"
check_eq "GiST index on frontages"  "1"  "$(q "SELECT count(*) FROM pg_indexes WHERE tablename='property_frontages' AND indexdef ILIKE '%gist%';")"
check_eq "all geometry SRID = 4326" "4326" \
  "$(q 'SELECT DISTINCT ST_SRID(geometry)::text FROM (
          SELECT geometry FROM property_service_zones
          UNION ALL SELECT geometry FROM property_entrances
          UNION ALL SELECT geometry FROM property_frontages) g;')"
# Validity is checked authority-wide: an invalid polygon anywhere is a real defect.
check_eq "no invalid geometry anywhere in the authority" "0" \
  "$(q 'SELECT (SELECT count(*) FROM property_service_zones WHERE NOT ST_IsValid(geometry))
             + (SELECT count(*) FROM property_frontages    WHERE NOT ST_IsValid(geometry));')"
check_eq "all lane zone polygons valid"  "0"  "$(q "SELECT count(*) FROM property_service_zones WHERE $LANE AND NOT ST_IsValid(geometry);")"
check_eq "all lane frontage lines valid" "0"  "$(q "SELECT count(*) FROM property_frontages WHERE $LANE AND NOT ST_IsValid(geometry);")"
check_eq "no overlapping service zones on the lane" "0" \
  "$(q "SELECT count(*) FROM property_service_zones a JOIN property_service_zones b
        ON a.zone_id < b.zone_id
        WHERE a.$LANE AND b.$LANE
          AND (ST_Overlaps(a.geometry, b.geometry) OR ST_Contains(a.geometry, b.geometry));")"
check_eq "every surveyed anchor sits inside its own zone" "0" \
  "$(q "SELECT count(*) FROM property_entrances e JOIN property_service_zones z USING (property_id)
        WHERE e.$LANE AND NOT ST_Within(e.geometry, z.geometry);")"
check_eq "provisional geometry is flagged for review" "$N" \
  "$(q "SELECT count(*) FROM property_service_zones
        WHERE $LANE AND source = 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY' AND verified = FALSE;")"
check_eq "lookup functions exist"   "2"  "$(q "SELECT count(*) FROM pg_proc WHERE proname IN ('wastraq_lookup_property','wastraq_lookup_candidates');")"

head_ "4. Distances are metres, not degrees"
# Zones are a few metres deep on a narrow lane: tens of m2, never thousands.
BADAREA="$(q "SELECT count(*) FROM property_service_zones
              WHERE $LANE
                AND (ST_Area(geometry::geography) < 5 OR ST_Area(geometry::geography) > 200);")"
check_eq "every zone area is 5-200 m2" "0" "$BADAREA"
AREASPAN="$(q "SELECT round(min(ST_Area(geometry::geography)))||'-'||round(max(ST_Area(geometry::geography)))
               FROM property_service_zones WHERE $LANE;")"
ok "zone areas span ${AREASPAN} m2"

# The lane is ~85 m end to end. In degrees the same span is ~0.0008.
SPAN="$(q "SELECT round(ST_Distance(a.geometry::geography, b.geometry::geography))
           FROM property_entrances a, property_entrances b
           WHERE a.property_id='PROP-001' AND b.property_id='PROP-010';")"
if [ "${SPAN:-0}" -ge 75 ] && [ "${SPAN:-0}" -le 95 ]; then
  ok "PROP-001 -> PROP-010 is ${SPAN} m apart (surveyed lane length)"
else
  bad "PROP-001 -> PROP-010 measured ${SPAN} - expected ~82 m; degrees treated as metres?"
fi
check_eq "raw ST_Distance on 4326 returns degrees, so geography casts are required" "true" \
  "$(q "SELECT (ST_Distance(a.geometry, b.geometry) < 0.01)::text
        FROM property_entrances a, property_entrances b
        WHERE a.property_id='PROP-001' AND b.property_id='PROP-010';")"
check_eq "ST_DWithin(…, 0.5 m) around a zone centre matches exactly 1 zone" "1" \
  "$(q "SELECT count(*) FROM property_service_zones
        WHERE $LANE
          AND ST_DWithin(ST_SetSRID(ST_MakePoint(${INSIDE_LON:-0}, ${INSIDE_LAT:-0}),4326)::geography,
                         geometry::geography, 0.5);")"

head_ "5. Spatial lookup on the real lane (SQL function)"
check_eq "inside a zone -> AUTO_ASSOCIATED ${INSIDE_PROPERTY:-?}" \
  "AUTO_ASSOCIATED|${INSIDE_PROPERTY:-?}" \
  "$(q "SELECT decision || \$\$|\$\$ || property_id FROM wastraq_lookup_property(${INSIDE_LAT:-0}, ${INSIDE_LON:-0});")"
check_eq "between ${AMBIG_A:-?} and ${AMBIG_B:-?} -> AMBIGUOUS" "AMBIGUOUS" \
  "$(q "SELECT decision FROM wastraq_lookup_property(${AMBIG_LAT:-0}, ${AMBIG_LON:-0});")"
check_eq "...and carries no property_id" "" \
  "$(q "SELECT coalesce(property_id,'') FROM wastraq_lookup_property(${AMBIG_LAT:-0}, ${AMBIG_LON:-0});")"
check_eq "off the lane -> NO_MATCH" "NO_MATCH" \
  "$(q "SELECT decision FROM wastraq_lookup_property(${FAR_LAT:-0}, ${FAR_LON:-0});")"
NC="$(q "SELECT count(*) FROM wastraq_lookup_candidates(${ROAD_LAT:-0}, ${ROAD_LON:-0});")"
if [ "${NC:-0}" -ge 2 ]; then ok "mid-carriageway returns $NC plausible candidates"; else bad "mid-carriageway returned ${NC:-0} candidates, expected >= 2"; fi
check_eq "candidates come back nearest-first" "true" \
  "$(q "SELECT (min(distance_m) = (array_agg(distance_m ORDER BY ord))[1])::text
        FROM (SELECT distance_m, row_number() OVER () ord
              FROM wastraq_lookup_candidates(${ROAD_LAT:-0}, ${ROAD_LON:-0})) c;")"
check_eq "mid-carriageway is not silently forced onto a property" "" \
  "$(q "SELECT coalesce(property_id,'') FROM wastraq_lookup_property(${ROAD_LAT:-0}, ${ROAD_LON:-0});")"

head_ "6. Offline logic tests (no DB needed)"
if "$PY" "$ROOT/scripts/generate_real_lane.py" --check --quiet >/dev/null 2>&1; then
  ok "real-lane geometry regenerates and validates"
else bad "generate_real_lane.py --check failed"; fi
if "$PY" "$ROOT/scripts/test_real_lane_lookup.py" >/dev/null 2>&1; then
  ok "gis.py decision ladder on the real lane"
else bad "test_real_lane_lookup.py failed"; fi
if "$PY" "$ROOT/scripts/test_lookup_logic.py" >/dev/null 2>&1; then
  ok "gis.py decision ladder on the synthetic lane (regression)"
else bad "test_lookup_logic.py failed"; fi

head_ "7. API (backend must be running)"
if curl -sf "$API/" >/dev/null 2>&1; then
  check_eq "GET /" "Wastraq Demo Backend Running" \
    "$(api_json GET / '' 'd["status"]')"

  # A stale uvicorn that never released the port answers every probe while
  # serving old code. Catch that here rather than blaming the endpoints.
  BV="$(api_json GET / '' 'd.get("version","<none>")')"
  if [ "$BV" = "$WQ_EXPECTED_VERSION" ]; then
    ok "backend is running current code (version $BV)"
  else
    bad "backend reports version '$BV', expected '$WQ_EXPECTED_VERSION' - stale process holding the port; re-run ./scripts/load_real_lane.sh"
  fi

  check_eq "GET /properties?route_id=$DEMO_ROUTE returns $N" "$N" \
    "$(api_json GET "/properties?route_id=$DEMO_ROUTE" '' 'len(d)')"
  check_eq "GET /routes lists the demo lane" "True" \
    "$(api_json GET /routes '' "any(r['route_id']=='$DEMO_ROUTE' for r in d)")"
  check_eq "GET /properties/${INSIDE_PROPERTY:-PROP-001} has a service zone" "True" \
    "$(api_json GET "/properties/${INSIDE_PROPERTY:-PROP-001}" '' 'd["service_zone_geojson"] is not None')"
  check_eq "...and a linked frontage photo" "True" \
    "$(api_json GET "/properties/${INSIDE_PROPERTY:-PROP-001}" '' 'd.get("frontage_photo_path") is not None')"
  check_eq "GET /properties/PROP-001/photo serves the image" "200" \
    "$(api_status GET /properties/PROP-001/photo '')"
  check_eq "GET /properties/PROP-001/photo-info says the file is on disk" "True" \
    "$(api_json GET /properties/PROP-001/photo-info '' 'd["exists_on_disk"]')"
  check_eq "GET /gis/layers/service-zones returns $N features" "$N" \
    "$(api_json GET /gis/layers/service-zones '' 'len(d["features"])')"

  # Bodies are built into plain variables first. Nothing here relies on
  # backslash-escaped quotes surviving a nested $( ) - the construct that
  # bash 3.2 mangles.
  DECISION_EXPR='d["decision"]+"|"+str(d["property_id"])'
  BODY_INSIDE="$(lookup_body "${INSIDE_LAT:-0}" "${INSIDE_LON:-0}")"
  BODY_AMBIG="$(lookup_body "${AMBIG_LAT:-0}" "${AMBIG_LON:-0}")"
  BODY_FAR="$(lookup_body "${FAR_LAT:-0}" "${FAR_LON:-0}")"
  BODY_ROAD="$(lookup_body "${ROAD_LAT:-0}" "${ROAD_LON:-0}")"

  check_eq "POST /gis/lookup inside a zone" "AUTO_ASSOCIATED|${INSIDE_PROPERTY:-?}" \
    "$(api_json POST /gis/lookup "$BODY_INSIDE" "$DECISION_EXPR")"
  check_eq "POST /gis/lookup between two zones" "AMBIGUOUS|None" \
    "$(api_json POST /gis/lookup "$BODY_AMBIG" "$DECISION_EXPR")"
  check_eq "POST /gis/lookup off the lane" "NO_MATCH|None" \
    "$(api_json POST /gis/lookup "$BODY_FAR" "$DECISION_EXPR")"
  ORDER_EXPR='len(d["candidates"]) >= 2 and [c["distance_m"] for c in d["candidates"]] == sorted(c["distance_m"] for c in d["candidates"])'
  check_eq "POST /gis/lookup mid-carriageway returns ordered candidates" "True" \
    "$(api_json POST /gis/lookup "$BODY_ROAD" "$ORDER_EXPR")"

  AMB_EVENT_BODY="$(event_body "${AMBIG_LAT:-0}" "${AMBIG_LON:-0}" PICKER-01)"
  check_eq "ambiguous coordinate is refused a collection event" "409" \
    "$(api_status POST /collection-events "$AMB_EVENT_BODY")"

  check_eq "GET /dashboard is served" "200" "$(api_status GET /dashboard '')"

  # --- prepared-statement stability -----------------------------------
  # psycopg promotes a query to a server-side PREPARE after prepare_threshold
  # (5) executions. At that point PostgreSQL has to infer every parameter's
  # type at parse time, and a bare `%(x)s IS NULL OR col = %(x)s` cannot be
  # resolved - it raises AmbiguousParameter and the endpoint starts returning
  # 500 only AFTER it has already worked several times. That is exactly what
  # took the dashboard event feed down, and a single call would never have
  # caught it. So call each filterable endpoint past the threshold.
  PREP_FAILS=0
  for path in "/collection-events/feed/detailed?route_id=$DEMO_ROUTE&limit=20" \
              "/collection-events/feed/detailed?limit=20" \
              "/collection-events/feed/detailed?segregation_status=SEGREGATED&limit=20" \
              "/collection-events/feed/detailed?since_hours=48&q=PROP&limit=20" \
              "/survey/api/properties?limit=5" \
              "/survey/api/properties?status=APPROVED&limit=5" \
              "/survey/api/properties/geojson?limit=5" \
              "/survey/api/properties/PROP-001/survey" \
              "/survey/api/analytics/overview" \
              "/survey/api/analytics/scale" \
              "/survey/api/surveys?survey_status=SUBMITTED&limit=5"; do
    for _ in 1 2 3 4 5 6 7; do
      code="$(api_status GET "$path" '')"
      [ "$code" = "200" ] || { PREP_FAILS=$((PREP_FAILS+1)); \
        bad "repeat call -> $code : $path"; break; }
    done
  done
  check_eq "filterable endpoints survive being prepared (7 calls each)" "0" "$PREP_FAILS"
  check_eq "GET /summary is scoped to $DEMO_ROUTE" "$DEMO_ROUTE" \
    "$(api_json GET /summary '' 'd["route_id"]')"
  check_eq "GET /summary reports $N mapped properties" "$N" \
    "$(api_json GET /summary '' 'd["totals"]["properties"]')"
  check_eq "GET /summary reports $N frontage photos" "$N" \
    "$(api_json GET /summary '' 'd["totals"]["frontage_photos"]')"
  check_eq "GET /analytics/operations is scoped too" "True" \
    "$(api_json GET "/analytics/operations?route_id=$DEMO_ROUTE" '' 'isinstance(d, dict)')"
else
  bad "backend not reachable at $API - start it with ./scripts/run_backend.sh"
fi

head_ "8. End-to-end chain"
EV="$(q "SELECT count(*) FROM collection_events;")"
NS="$(q "SELECT count(*) FROM collection_events WHERE segregation_status='NOT_SEGREGATED' AND rfid_triggered;")"
EVD="$(q "SELECT count(*) FROM evidence e JOIN collection_events c ON c.event_id=e.event_id;")"
ORPH="$(q "SELECT count(*) FROM collection_events c
           LEFT JOIN properties p ON p.property_id=c.property_id WHERE p.property_id IS NULL;")"
if [ "${EV:-0}" -gt 0 ]; then ok "$EV collection event(s) recorded"; else bad "no collection events - run simulation/simulate_picker.py"; fi
if [ "${NS:-0}" -gt 0 ]; then ok "$NS NOT_SEGREGATED event(s) with RFID trigger"; else bad "no non-segregation event recorded"; fi
if [ "${EVD:-0}" -gt 0 ]; then ok "$EVD evidence record(s) linked to events"; else bad "no evidence linked"; fi
check_eq "no collection event orphaned by the migration" "0" "${ORPH:-1}"


# =====================================================================
# 9-12. City survey module
#
# The survey module is additive: it shares one property master with the
# lane demo. These checks prove both halves of that claim - the city data
# is really there, and the 16 demo-lane rows were not disturbed by it.
# The section is skipped when the survey schema is absent, so a lane-only
# install still reports a clean run.
# =====================================================================
HAS_SURVEY="$(q "SELECT count(*) FROM information_schema.tables
                 WHERE table_name IN ('property_surveys','survey_assignments',
                                      'administrative_units','survey_users',
                                      'property_qa_issues','property_geometry_history');")"
if [ "${HAS_SURVEY:-0}" -eq 6 ]; then
  head_ "9. City survey module (database)"
  ok "all six survey tables exist"

  check_eq "the $N demo-lane properties are untouched" "$N" \
    "$(q "SELECT count(*) FROM properties WHERE route_id = '$DEMO_ROUTE';")"
  check_eq "demo-lane geometry still carries its survey source" "$N" \
    "$(q "SELECT count(*) FROM property_service_zones
          WHERE $LANE AND source = 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY';")"

  # The module is real-data-only by design. There is no synthetic city seed
  # any more, so the contract is the opposite of what it used to be: the
  # generated PROP-##### rows must be GONE, and the property master must
  # contain the pilot lane and nothing invented.
  SYNTH="$(q "SELECT count(*) FROM properties WHERE property_id ~ '^PROP-[0-9]{5}\$';")"
  wq_default SYNTH "0"
  check_eq "0 synthetic city properties (real data only)" "0" "$SYNTH"

  TOTAL="$(q "SELECT count(*) FROM properties;")"
  check_eq "the property master holds exactly the $N real pilot properties" "$N" "$TOTAL"

  EXTRA="$(q "SELECT count(*) FROM properties WHERE route_id IS DISTINCT FROM '$DEMO_ROUTE';")"
  wq_default EXTRA "0"
  if [ "$EXTRA" -eq 0 ]; then
    ok "no properties outside the pilot route (0)"
  else
    ok "$EXTRA propert(ies) on other routes - real expansion beyond the pilot"
  fi

  check_eq "admin hierarchy is a tree with one root" "1" \
    "$(q "SELECT count(*) FROM administrative_units WHERE parent_id IS NULL;")"
  check_eq "no administrative unit is its own parent" "0" \
    "$(q "SELECT count(*) FROM administrative_units WHERE parent_id = admin_unit_id;")"
  DEPTH="$(q "WITH RECURSIVE t AS (
                SELECT admin_unit_id, 1 AS d FROM administrative_units WHERE parent_id IS NULL
                UNION ALL
                SELECT a.admin_unit_id, t.d + 1 FROM administrative_units a
                  JOIN t ON a.parent_id = t.admin_unit_id
              ) SELECT max(d) FROM t;")"
  if [ "${DEPTH:-0}" -ge 3 ]; then ok "hierarchy is $DEPTH levels deep (city -> zone -> ward -> route area)"
  else bad "admin hierarchy only ${DEPTH:-0} level(s) deep"; fi

  # Scale capability is asserted STRUCTURALLY - every level the city rollout
  # needs exists and is reachable - rather than by counting invented rows in
  # it. An empty ward is a ward that is ready, not a ward that is missing.
  check_eq "all four scale levels exist (CITY/ZONE/WARD/ROUTE_AREA)" "4" \
    "$(q "SELECT count(DISTINCT unit_type) FROM administrative_units
          WHERE unit_type IN ('CITY','ZONE','WARD','ROUTE_AREA');")"
  check_eq "the pilot route area chains all the way up to the city" "1" \
    "$(q "WITH RECURSIVE up AS (
            SELECT admin_unit_id, parent_id, unit_type FROM administrative_units
             WHERE admin_unit_id IN (SELECT DISTINCT admin_unit_id FROM properties
                                      WHERE route_id = '$DEMO_ROUTE' AND admin_unit_id IS NOT NULL)
            UNION ALL
            SELECT a.admin_unit_id, a.parent_id, a.unit_type
              FROM administrative_units a JOIN up ON up.parent_id = a.admin_unit_id)
          SELECT count(*) FROM up WHERE unit_type = 'CITY';")"
  check_eq "every pilot property is attached to an administrative unit" "0" \
    "$(q "SELECT count(*) FROM properties
          WHERE route_id = '$DEMO_ROUTE' AND admin_unit_id IS NULL;")"

  check_eq "every property points at a real admin unit" "0" \
    "$(q "SELECT count(*) FROM properties p WHERE p.admin_unit_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM administrative_units a
                          WHERE a.admin_unit_id = p.admin_unit_id);")"
  check_eq "every survey points at a real property" "0" \
    "$(q "SELECT count(*) FROM property_surveys s
          WHERE NOT EXISTS (SELECT 1 FROM properties p WHERE p.property_id = s.property_id);")"
  check_eq "at most one survey row per property" "0" \
    "$(q "SELECT count(*) FROM (SELECT property_id FROM property_surveys
          GROUP BY property_id HAVING count(*) > 1) x;")"
  check_eq "no survey is APPROVED without a reviewer" "0" \
    "$(q "SELECT count(*) FROM property_surveys
          WHERE survey_status = 'APPROVED' AND reviewer_id IS NULL;")"
  check_eq "VERIFIED_FOR_OPERATION only follows an approved survey" "0" \
    "$(q "SELECT count(*) FROM properties p
          WHERE p.verification_status = 'VERIFIED_FOR_OPERATION'
            AND p.route_id IS DISTINCT FROM '$DEMO_ROUTE'
            AND NOT EXISTS (SELECT 1 FROM property_surveys s
                            WHERE s.property_id = p.property_id
                              AND s.survey_status = 'APPROVED');")"

  head_ "10. Device location capture is kept honestly"
  check_eq "every captured fix stores its accuracy" "0" \
    "$(q "SELECT count(*) FROM property_surveys
          WHERE captured_latitude IS NOT NULL AND location_accuracy_m IS NULL;")"
  check_eq "every captured fix records its source" "0" \
    "$(q "SELECT count(*) FROM property_surveys
          WHERE captured_latitude IS NOT NULL AND location_source IS NULL;")"
  check_eq "the raw GNSS point is stored alongside the entrance" "0" \
    "$(q "SELECT count(*) FROM property_surveys
          WHERE captured_latitude IS NOT NULL AND captured_point IS NULL;")"
  check_eq "an adjusted fix records who moved it and when" "0" \
    "$(q "SELECT count(*) FROM property_surveys
          WHERE manually_adjusted AND (adjusted_by IS NULL OR adjustment_timestamp IS NULL);")"
  # This is the invariant the whole location-capture design rests on, so the
  # failure names its own remedy rather than just printing a number.
  BADCONF="$(q "SELECT count(*) FROM property_surveys
                WHERE location_accuracy_m > ${GNSS_ACCURACY_WARN_M:-10}
                  AND mapping_confidence = 'HIGH';")"
  wq_default BADCONF "0"
  if [ "$BADCONF" -eq 0 ]; then
    ok "no poor fix is quietly marked HIGH confidence (0)"
  else
    bad "$BADCONF survey(s) carry HIGH confidence on a fix worse than ${GNSS_ACCURACY_WARN_M:-10} m"
    echo "        seeded before the generator applied the cap. Fix with:"
    echo "        psql -d $DB_NAME -f database/repair_survey_confidence.sql"
  fi
  POOR="$(q "SELECT count(*) FROM property_surveys
             WHERE location_accuracy_m > ${GNSS_ACCURACY_WARN_M:-10};")"
  ok "${POOR:-0} survey(s) carry a low-accuracy fix and are flagged rather than hidden"

  head_ "10b. Field-survey geometry model"
  check_eq "properties carries the surveyor-editable columns" "7" \
    "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='properties'
          AND column_name IN ('owner_phone','owner_email','street_name','locality',
                              'pincode','service_entity_type','updated_at');")"
  check_eq "property_photos records how the image was captured" "3" \
    "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='property_photos'
          AND column_name IN ('capture_method','capture_latitude','capture_longitude');")"
  check_eq "entrances are POINT geometry" "ST_Point" \
    "$(q "SELECT DISTINCT ST_GeometryType(geometry) FROM property_entrances LIMIT 1;")"
  check_eq "frontages are LINESTRING geometry" "ST_LineString" \
    "$(q "SELECT DISTINCT ST_GeometryType(geometry) FROM property_frontages LIMIT 1;")"
  check_eq "service zones are POLYGON geometry" "ST_Polygon" \
    "$(q "SELECT DISTINCT ST_GeometryType(geometry) FROM property_service_zones LIMIT 1;")"
  check_eq "every frontage has at least 2 points" "0" \
    "$(q "SELECT count(*) FROM property_frontages WHERE ST_NPoints(geometry) < 2;")"
  check_eq "every service zone has at least 3 distinct vertices" "0" \
    "$(q "SELECT count(*) FROM property_service_zones
          WHERE ST_NPoints(ST_RemoveRepeatedPoints(geometry)) - 1 < 3;")"
  check_eq "no service zone has zero area" "0" \
    "$(q "SELECT count(*) FROM property_service_zones
          WHERE ST_Area(geometry::geography) <= 0;")"
  FARENT="$(q "SELECT count(*) FROM property_entrances e
               JOIN property_frontages f USING (property_id)
               WHERE ST_Distance(e.geometry::geography, f.geometry::geography)
                     > ${ENTRANCE_PROXIMITY_MAX_M:-20};")"
  wq_default FARENT "0"
  if [ "$FARENT" -eq 0 ]; then
    ok "every entrance is within ${ENTRANCE_PROXIMITY_MAX_M:-20} m of its own frontage (0)"
  else
    bad "$FARENT entrance(s) sit further than ${ENTRANCE_PROXIMITY_MAX_M:-20} m from their frontage"
  fi

  head_ "11. Geometry history and QA"
  check_eq "history trigger exists on all three geometry tables" "3" \
    "$(q "SELECT count(*) FROM pg_trigger
          WHERE tgname LIKE 'trg\\_%\\_history' AND NOT tgisinternal;")"
  # Prove the trigger really fires, then roll the probe back so nothing changes.
  HIST="$(psql -tA -d "$DB_NAME" -v ON_ERROR_STOP=1 2>/dev/null <<'SQL'
BEGIN;
UPDATE property_entrances SET verified = NOT verified
 WHERE entrance_id = (SELECT entrance_id FROM property_entrances ORDER BY entrance_id LIMIT 1);
SELECT count(*) FROM property_geometry_history
 WHERE changed_at > now() - interval '1 minute';
ROLLBACK;
SQL
)"
  HIST="$(printf '%s' "$HIST" | tr -s '[:space:]' '\n' | grep -E '^[0-9]+$' | tail -1)"
  if [ "${HIST:-0}" -ge 1 ]; then ok "editing geometry writes a history row (probe rolled back)"
  else bad "geometry edit did not produce a history row"; fi

  check_eq "no QA issue references a missing property" "0" \
    "$(q "SELECT count(*) FROM property_qa_issues q
          WHERE NOT EXISTS (SELECT 1 FROM properties p WHERE p.property_id = q.property_id);")"
  check_eq "at most one OPEN issue of a given type per property" "0" \
    "$(q "SELECT count(*) FROM (SELECT property_id, issue_type FROM property_qa_issues
          WHERE status = 'OPEN' GROUP BY property_id, issue_type HAVING count(*) > 1) x;")"
  OPENQA="$(q "SELECT count(*) FROM property_qa_issues WHERE status = 'OPEN';")"
  ok "${OPENQA:-0} open QA issue(s) currently detected"

  head_ "12. Survey API and dashboards (backend must be running)"
  if curl -sf "$API/" >/dev/null 2>&1; then
    # Three primary views + the scale tooling that stays reachable.
    for page in /survey /survey/field /survey/review \
                /survey/map /survey/assignments /survey/qa; do
      check_eq "GET $page is served" "200" "$(api_status GET "$page" '')"
    done
    # The surveyor-performance page was retired with the synthetic data it
    # measured. Its absence is the contract now, so assert it.
    check_eq "GET /survey/surveyors is retired (404)" "404" \
      "$(api_status GET /survey/surveyors '')"
    check_eq "...but the surveyor analytics API is still available" "True" \
      "$(api_json GET /survey/api/analytics/surveyors '' 'isinstance(d, list)')"
    check_eq "GET /assets/wq.css is served"    "200" "$(api_status GET /assets/wq.css '')"
    check_eq "GET /assets/wq.js is served"     "200" "$(api_status GET /assets/wq.js '')"
    check_eq "GET /assets/wq-map.js is served" "200" "$(api_status GET /assets/wq-map.js '')"

    check_eq "GET /survey/api/admin-units/tree returns a rooted tree" "True" \
      "$(api_json GET /survey/api/admin-units/tree '' 'any(u["depth"] == 1 for u in d)')"
    check_eq "GET /survey/api/analytics/overview reports the REAL total" "$N" \
      "$(api_json GET /survey/api/analytics/overview '' 'd["totals"]["total_properties"]')"
    check_eq "...with surveyed <= total and verified <= surveyed" "True" \
      "$(api_json GET /survey/api/analytics/overview '' \
         'd["totals"]["surveyed"] <= d["totals"]["total_properties"] and d["totals"]["verified"] <= d["totals"]["surveyed"]')"
    check_eq "GET /survey/api/analytics/scale reports the pilot route" "$DEMO_ROUTE" \
      "$(api_json GET /survey/api/analytics/scale '' 'd["pilot_route_id"]')"
    check_eq "...and the scale path names all five levels" "5" \
      "$(api_json GET /survey/api/analytics/scale '' 'len(d["path"])')"
    check_eq "...with the pilot properties rolling up to city level" "True" \
      "$(api_json GET /survey/api/analytics/scale '' \
         'any(l["unit_type"] == "CITY" and l["properties"] >= 1 for l in d["levels"])')"
    check_eq "GET /survey/api/assignments returns assignments" "True" \
      "$(api_json GET /survey/api/assignments '' 'len(d) > 0')"
    check_eq "GET /survey/api/properties is paged" "True" \
      "$(api_json GET "/survey/api/properties?limit=25" '' 'len(d["items"]) <= 25 and d["total"] >= 16')"
    check_eq "GET /survey/api/properties/geojson is a FeatureCollection" "FeatureCollection" \
      "$(api_json GET "/survey/api/properties/geojson?limit=50" '' 'd["type"]')"
    check_eq "GET /survey/api/analytics/surveyors returns per-surveyor rows" "True" \
      "$(api_json GET /survey/api/analytics/surveyors '' 'len(d) > 0')"
    check_eq "GET /survey/api/qa-issues returns a list" "True" \
      "$(api_json GET /survey/api/qa-issues '' 'isinstance(d, list)')"
    # --- the field survey works on the REAL pilot properties -----------
    check_eq "the pilot lane is visible to the survey API" "PROP-001" \
      "$(api_json GET /survey/api/properties/PROP-001/survey '' 'd["property"]["property_id"]')"
    check_eq "...and it returns the pilot geometry, not a placeholder" "True" \
      "$(api_json GET /survey/api/properties/PROP-001/survey '' \
         'all(d["geometry"].get(k) for k in ("entrance","frontage","service_zone"))')"
    check_eq "...and its frontage photo is linked" "True" \
      "$(api_json GET /survey/api/properties/PROP-001/survey '' 'len(d["photos"]) >= 1')"
    check_eq "...and it exposes the GNSS accuracy threshold" "True" \
      "$(api_json GET /survey/api/properties/PROP-001/survey '' 'd["thresholds"]["gnss_accuracy_warn_m"] > 0')"
    check_eq "...and the proximity thresholds the submit gate uses" "True" \
      "$(api_json GET /survey/api/properties/PROP-001/survey '' \
         'd["thresholds"]["entrance_proximity_max_m"] > d["thresholds"]["entrance_proximity_ok_m"] > 0')"
    check_eq "...and the vocabulary the field form is built from" "True" \
      "$(api_json GET /survey/api/properties/PROP-001/survey '' \
         'len(d["vocabulary"]["service_entity_type"]) > 0')"

    # GNSS accuracy is CONFIGURATION, not seeded data: it has to come back
    # from a real pilot survey with no synthetic row involved anywhere.
    PILOT_SID="$(q "SELECT s.survey_id FROM property_surveys s
                    JOIN properties p ON p.property_id = s.property_id
                    WHERE p.route_id = '$DEMO_ROUTE'
                    ORDER BY s.survey_id LIMIT 1;")"
    wq_default PILOT_SID ""
    if [ -n "$PILOT_SID" ]; then
      check_eq "GNSS threshold reaches readiness on a real pilot survey" "True" \
        "$(api_json GET "/survey/api/surveys/$PILOT_SID/readiness" '' \
           'd["thresholds"]["gnss_accuracy_warn_m"] > 0')"
    else
      bad "no survey row found for the pilot lane - the field workflow has nothing to open"
    fi

    # --- the review workflow is available -------------------------------
    check_eq "the review queue is queryable" "True" \
      "$(api_json GET "/survey/api/surveys?survey_status=SUBMITTED" '' 'isinstance(d, list)')"
    REVIEW_STATES='("NOT_SURVEYED","IN_PROGRESS","SUBMITTED","APPROVED","CORRECTION_REQUIRED","REJECTED")'
    check_eq "a real pilot survey exposes its review state" "True" \
      "$(api_json GET /survey/api/properties/PROP-001/survey '' \
         "d['survey'] is not None and d['survey']['survey_status'] in $REVIEW_STATES")"
    check_eq "reviewers exist to run the workflow" "True" \
      "$(api_json GET "/survey/api/users?role=REVIEWER" '' 'len(d) >= 1')"
    check_eq "surveyors exist to run the field workflow" "True" \
      "$(api_json GET "/survey/api/users?role=SURVEYOR" '' 'len(d) >= 1')"

    # Capture the workflow test's own output instead of discarding it. Telling
    # someone to "run it directly to see which step broke" is a worse answer
    # than simply showing them the step that broke.
    SURVEY_TEST_LOG="$ROOT/logs/test_survey_api.log"
    mkdir -p "$ROOT/logs"
    if "$PY" "$ROOT/scripts/test_survey_api.py" -v > "$SURVEY_TEST_LOG" 2>&1; then
      SUBN="$(grep -c 'PASS' "$SURVEY_TEST_LOG" 2>/dev/null)"
      wq_default SUBN "?"
      ok "survey workflow test passed ($SUBN checks: assign -> capture -> draw -> submit -> review)"
    else
      bad "scripts/test_survey_api.py failed - full log: logs/test_survey_api.log"
      # surface the failing assertions inline, trimmed
      sed 's/\x1b\[[0-9;]*m//g' "$SURVEY_TEST_LOG" \
        | grep -E "^  FAIL|^Missing:|^[0-9]+/[0-9]+ survey workflow" \
        | head -12 | sed 's/^/        /'
    fi
  else
    bad "backend not reachable at $API - survey API checks skipped"
  fi
else
  head_ "9. City survey module"
  ok "survey schema not installed - skipping (run ./scripts/upgrade_dashboards.sh to add it)"
fi

# =====================================================================
# 13. Property master / registration
#
# The administrative half of the workflow: a property record exists BEFORE
# anyone walks to it. Registration writes properties.*; the field survey
# writes property_entrances / _frontages / _service_zones. These checks are
# mostly about that boundary holding.
# =====================================================================
HAVE_PM="$(q "SELECT count(*) FROM information_schema.views WHERE table_name='v_property_master';")"
wq_default HAVE_PM "0"

if [ "$HAVE_PM" = "1" ]; then
  head_ "13. Property master (database)"

  check_eq "v_property_master exists" "1" "$HAVE_PM"
  check_eq "property_change_log exists" "1" \
    "$(q "SELECT count(*) FROM information_schema.tables WHERE table_name='property_change_log';")"
  check_eq "properties carries the registration reference fix" "5" \
    "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='properties'
          AND column_name IN ('captured_latitude','captured_longitude',
                              'captured_accuracy_m','captured_at','location_source');")"
  check_eq "properties records who created and who last changed it" "2" \
    "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='properties'
          AND column_name IN ('created_by','updated_by');")"
  check_eq "properties can be deactivated without being deleted" "2" \
    "$(q "SELECT count(*) FROM information_schema.columns WHERE table_name='properties'
          AND column_name IN ('active','inactive_reason');")"

  # The registration fix and the surveyed entrance are DIFFERENT columns in
  # DIFFERENT tables. Collapsing them would let an unreviewed phone fix decide
  # which property a picker collected from.
  check_eq "the registration fix is not stored as survey geometry" "0" \
    "$(q "SELECT count(*) FROM information_schema.columns
          WHERE table_name IN ('property_entrances','property_frontages','property_service_zones')
          AND column_name LIKE 'captured_%';")"

  # Vocabulary was WIDENED, not replaced: every value the pilot rows already
  # use has to remain legal or the migration would have needed a data rewrite.
  PMCHK="$(q "SELECT pg_get_constraintdef(oid) FROM pg_constraint
              WHERE conname='properties_property_type_check';")"
  wq_default PMCHK ""
  for V in INDEPENDENT_HOUSE APARTMENT SHOP COMMERCIAL_BUILDING OFFICE SCHOOL \
           HOSPITAL HOTEL MARKET GATED_COMMUNITY INDUSTRIAL VACANT_PROPERTY OTHER; do
    case "$PMCHK" in *"$V"*) ok "property_type accepts $V" ;;
                     *) bad "property_type does not accept $V" ;; esac
  done
  for V in RESIDENTIAL COMMERCIAL MIXED INSTITUTIONAL; do
    case "$PMCHK" in *"$V"*) ok "property_type still accepts the legacy value $V" ;;
                     *) bad "property_type dropped the legacy value $V - pilot rows would be invalid" ;; esac
  done
  SECHK="$(q "SELECT pg_get_constraintdef(oid) FROM pg_constraint
              WHERE conname='properties_service_entity_type_check';")"
  wq_default SECHK ""
  for V in INDIVIDUAL_PROPERTY BUILDING COMMON_COLLECTION_POINT COMMERCIAL_COMPLEX \
           COMMUNITY_COLLECTION_POINT SINGLE_HOUSEHOLD APARTMENT_BLOCK; do
    case "$SECHK" in *"$V"*) ok "service_entity_type accepts $V" ;;
                     *) bad "service_entity_type does not accept $V" ;; esac
  done
  VSCHK="$(q "SELECT pg_get_constraintdef(oid) FROM pg_constraint
              WHERE conname='properties_verification_status_check';")"
  wq_default VSCHK ""
  case "$VSCHK" in *PENDING_SURVEY*) ok "verification_status has a PENDING_SURVEY state" ;;
                   *) bad "verification_status has no PENDING_SURVEY state" ;; esac
  case "$VSCHK" in *VERIFIED_FOR_OPERATION*) ok "...and still has VERIFIED_FOR_OPERATION" ;;
                   *) bad "VERIFIED_FOR_OPERATION was dropped" ;; esac

  # Registration must not have invented rows. The pilot is the dataset.
  check_eq "no synthetic city properties" "0" \
    "$(q "SELECT count(*) FROM properties WHERE property_id ~ '^PROP-[0-9]{5}\$';")"
  check_eq "the property master is ONE table, not a competing one" "1" \
    "$(q "SELECT count(*) FROM information_schema.tables
          WHERE table_name IN ('properties','property_master','property_registry','property_records');")"
  check_eq "v_property_master reports every real property" "$(q 'SELECT count(*) FROM properties;')" \
    "$(q 'SELECT count(*) FROM v_property_master;')"

  # A property whose current survey is APPROVED, by a named reviewer, at a
  # recorded time, IS cleared for operation. If the property row disagrees,
  # the database is telling two stories about the same property.
  check_eq "every property with an approved, reviewed survey is verified for operation" "0" \
    "$(q "SELECT count(*) FROM properties p
          JOIN v_property_current_survey s ON s.property_id = p.property_id
          WHERE s.survey_status = 'APPROVED' AND s.review_status = 'APPROVED'
            AND s.reviewer_id IS NOT NULL AND s.reviewed_at IS NOT NULL
            AND p.verification_status <> 'VERIFIED_FOR_OPERATION';")"
  check_eq "...and nothing is verified for operation without one" "0" \
    "$(q "SELECT count(*) FROM properties p
          LEFT JOIN v_property_current_survey s ON s.property_id = p.property_id
          WHERE p.verification_status = 'VERIFIED_FOR_OPERATION'
            AND (s.survey_status IS DISTINCT FROM 'APPROVED' OR s.reviewed_at IS NULL);")"
  check_eq "the $N pilot properties are verified for operation" "$N" \
    "$(q "SELECT count(*) FROM properties
          WHERE route_id = '$DEMO_ROUTE' AND verification_status = 'VERIFIED_FOR_OPERATION';")"
  check_eq "...each still carrying its own entrance, frontage, zone and photo" "$N" \
    "$(q "SELECT count(*) FROM v_property_master
          WHERE route_id = '$DEMO_ROUTE'
            AND has_entrance AND has_frontage AND has_service_zone AND has_frontage_photo;")"

  if curl -sf "$API/" >/dev/null 2>&1; then
    head_ "13b. Property master (API and page)"

    check_eq "the property registration page is served" "200" \
      "$(curl -s -o /dev/null -w '%{http_code}' "$API/property-registration")"
    check_eq "...and it is a page, not the JSON API" "True" \
      "$(curl -s "$API/property-registration" | grep -qi '<title>.*Property master' && echo True || echo False)"

    # /properties/master must not be swallowed by /properties/{property_id}.
    check_eq "GET /properties/master is not shadowed by /properties/{id}" "True" \
      "$(api_json GET /properties/master '' 'isinstance(d, dict) and "items" in d')"
    check_eq "the master list returns as many rows as it reports" "True" \
      "$(api_json GET /properties/master '' 'len(d["items"]) == min(d["total"], d["limit"]) and d["total"] >= 16')"
    check_eq "the master summary is COUNT(*), and says so" "True" \
      "$(api_json GET /properties/master/summary '' '"COUNT(*)" in d["source"]')"
    check_eq "...and reports the five headline figures" "True" \
      "$(api_json GET /properties/master/summary '' \
         'all(k in d for k in ("total","verified","pending_survey","pending_review","inactive"))')"
    check_eq "the vocabulary endpoint offers the registration property types" "True" \
      "$(api_json GET /properties/master/vocabulary '' \
         '{"INDEPENDENT_HOUSE","APARTMENT","SHOP","SCHOOL","HOSPITAL","GATED_COMMUNITY"} <= {t["value"] for t in d["property_types"]}')"
    check_eq "...and the service entity types, as a separate axis" "True" \
      "$(api_json GET /properties/master/vocabulary '' \
         '{"INDIVIDUAL_PROPERTY","BUILDING","COMMON_COLLECTION_POINT"} <= {t["value"] for t in d["service_entity_types"]}')"
    check_eq "...and the next property id, generated server-side" "True" \
      "$(api_json GET /properties/master/vocabulary '' 'd["next_property_id"].startswith("PROP-")')"
    # Two separate facts, asserted separately. Bundling them meant a failure
    # could not say WHICH half was wrong - and the half that was wrong was an
    # assumption about the data, not the GIS calculation being tested.
    check_eq "a real pilot property reports its GIS state to the registry" "True" \
      "$(api_json GET /properties/PROP-001/survey-status '' \
         'd["missing"] == [] and all(d["gis"].values())')"
    check_eq "...read from the geometry tables, not inferred from a status" "True" \
      "$(api_json GET /properties/PROP-001/survey-status '' \
         'd["gis"] == {"entrance": True, "frontage": True, "service_zone": True, "frontage_photo": True}')"
    check_eq "...and ready_for_operation follows verification_status alone" "True" \
      "$(api_json GET /properties/PROP-001/survey-status '' \
         'd["ready_for_operation"] == (d["verification_status"] == "VERIFIED_FOR_OPERATION")')"
    check_eq "...and its administrative record" "PROP-001" \
      "$(api_json GET /properties/PROP-001/master '' 'd["property_id"]')"
    check_eq "the existing property detail endpoint still works" "PROP-001" \
      "$(api_json GET /properties/PROP-001 '' 'd["property_id"]')"

    # The full registration workflow, with its own throwaway property.
    PM_TEST_LOG="$ROOT/logs/test_property_master.log"
    mkdir -p "$ROOT/logs"
    if "$PY" "$ROOT/scripts/test_property_master.py" -v > "$PM_TEST_LOG" 2>&1; then
      PMN="$(grep -c 'PASS' "$PM_TEST_LOG" 2>/dev/null)"
      wq_default PMN "?"
      ok "registration workflow test passed ($PMN checks: duplicate check -> register -> locate -> edit -> hand off to survey)"
    else
      bad "scripts/test_property_master.py failed - full log: logs/test_property_master.log"
      sed 's/\x1b\[[0-9;]*m//g' "$PM_TEST_LOG" \
        | grep -E "^  FAIL|^[0-9]+/[0-9]+ property master" | head -12 | sed 's/^/        /'
    fi
  else
    bad "backend not reachable at $API - property master API checks skipped"
  fi
else
  head_ "13. Property master"
  ok "property master schema not installed - skipping (run ./scripts/add_property_master_dashboard.sh)"
fi

printf "\n\033[1m%d passed, %d failed\033[0m\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]

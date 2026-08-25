# Wastraq — Source Segregation Evidence Engine

Two dashboards on one database.

**1. The live lane demo** (`/dashboard`) proves one chain, end to end, on the 16
real surveyed properties of 2nd Cross Road:

```
picker coordinate
      ↓
PostGIS service-zone association      ← the part that matters
      ↓
collection event
      ↓
segregation status (SEGREGATED by default)
      ↓
evidence
      ↓
dashboard
```

**2. The city survey module** (`/survey`) answers the next question: how do the
*other* few hundred thousand properties get mapped, and how do we know the
mapping is any good? Three views — overview, field survey, review queue — plus
assignments and GIS QA behind them, all against the same PostgreSQL + PostGIS
database. See **[docs/SURVEY.md](docs/SURVEY.md)**.

**Every number it shows is real.** There is no synthetic city data: the module
reports the 16 surveyed pilot properties and nothing else, and a level of the
hierarchy that exists but holds no properties says so rather than being padded.
The scale path — `Pilot Lane → Route → Ward → Zone → City` — is computed from
the database, not asserted in the markup.

The two share one property master and are separated by scope, not by schema:
every operations endpoint is scoped by `route_id`, so the city data cannot move a
number on the lane dashboard.

## The lane

The demo runs on a **real surveyed lane**: 16 properties on 2nd Cross Road,
Krishnamurthy Puram, Mysuru. The 16 entrance/service anchors were collected on
site; the frontages and service zones around them are **auto-generated first
approximations**, marked `source = FIELD_SURVEY_PLUS_AUTO_GEOMETRY` and
`verified = false` so it is obvious in QGIS what still needs a human eye.

Geometry is built in **EPSG:32643 (UTM 43N)** — real metres — and stored back in
EPSG:4326. Which side of the road a property sits on is decided by the geometry
(PCA over the anchors), not by its number: the survey walked west down the south
side (`PROP-001`…`PROP-010`) and back east along the north side
(`PROP-011`…`PROP-016`), and the generator works that out for itself.

The original synthetic 10-property lane is still in `database/gis_dummy_data.sql`
if you want a throwaway reset.

## The core rule

A property is **never** identified by "nearest vehicle GPS point". Every
property carries a mapped GIS structure — a permanent `property_id`, an
**entrance point**, a **frontage line**, and a **service-zone polygon** — and
association is decided against those polygons.

The engine returns one of three decisions and will not fudge the middle one:

| Decision | When | Effect |
|---|---|---|
| `AUTO_ASSOCIATED` | inside exactly one service zone, or clearly nearest to one | collection event may be created |
| `AMBIGUOUS` | inside overlapping zones, too far from any, or two zones too close to separate | all candidates returned with distances; **no event created** |
| `NO_MATCH` | nothing within the search radius | nothing created |

`POST /collection-events` with a coordinate returns **409** when the lookup is
not `AUTO_ASSOCIATED`. Refusing to guess is a feature, not a gap.

Distances are metres, not degrees: 4326 geometry is cast to `geography` so
`ST_Distance` / `ST_DWithin` measure on the spheroid. (`METRIC_SRID=32643`,
UTM 43N, is configured for the projected equivalent.)

---

## 1. Install (macOS)

One command does everything — Homebrew packages, database, schema, seed data,
Python venv:

```bash
cd wastraq-demo
chmod +x scripts/*.sh
./scripts/setup_macos.sh
```

**Already installed PostgreSQL + PostGIS and created `wastraq_demo` yourself?**
Skip the install and run the finisher instead — it loads the SQL (including the
real 16-property lane), builds the venv, starts the API, runs the simulation and
verifies the lot, without ever dropping your database:

```bash
./scripts/finish_setup.sh
```

**Already have the demo running and just want the real lane?**

```bash
./scripts/load_real_lane.sh
```

That backs the current GIS tables up to `logs/`, regenerates and applies
`database/real_lane_16.sql` in one transaction, validates the geometry in
PostGIS, restarts the backend, re-runs the simulation and re-runs verification.
It never drops the database and never touches `pickers`, `collection_events` or
`evidence`. Point it at your photos with `PHOTO_DIR=~/properties` (the default).

Everything either script does is logged under `logs/`.

<details>
<summary>What it runs, if you'd rather do it by hand</summary>

```bash
# PostgreSQL + PostGIS  (17 is the default; 16 also works)
brew install postgresql@17 postgis
brew services start postgresql@17
export PATH="$(brew --prefix)/opt/postgresql@17/bin:$PATH"

# Database
createdb wastraq_demo
psql -v ON_ERROR_STOP=1 -d wastraq_demo -f database/schema.sql
psql -v ON_ERROR_STOP=1 -d wastraq_demo -f database/seed.sql
psql -v ON_ERROR_STOP=1 -d wastraq_demo -f database/gis_dummy_data.sql
psql -v ON_ERROR_STOP=1 -d wastraq_demo -f database/lookup_function.sql
# ...then the real lane (substitute your photo directory first)
sed "s|__PHOTO_DIR__|$HOME/properties|g" database/real_lane_16.sql \
  | psql -v ON_ERROR_STOP=1 -d wastraq_demo

# Python 3.11 — do NOT use whatever `python3` happens to be
brew install python@3.11
./scripts/setup_python_env.sh        # builds .venv on 3.11 and verifies it

cp backend/.env.example backend/.env
echo "DB_USER=$USER" >> backend/.env
```

**On the Python version.** The project pins itself to **Python 3.11** and picks
the interpreter deliberately (`scripts/py_env.sh`) rather than inheriting
`python3`. This is not fussiness: `psycopg-binary` and `numpy` publish wheels
months behind each new CPython release, so a Mac whose Homebrew python has
rolled forward to 3.14 cannot install this stack at all — and because pip
abandons the *entire* resolution on the first unsatisfiable requirement, the
visible symptom is a missing package that had nothing to do with the failure.

`./scripts/setup_python_env.sh` handles all of it: it finds 3.11 (installing
`python@3.11` via Homebrew if that is the only way), rebuilds `.venv` when it is
on the wrong Python — moving the old one aside rather than deleting it —
installs the dependencies, and then imports `backend/app/main.py` to prove every
route actually builds before reporting success. `WQ_PYTHON=/path/to/python3.11`
overrides the search; `--force` rebuilds unconditionally.

QGIS, when you want it: `brew install --cask qgis`
</details>

## 2. Run

```bash
# terminal 1 — backend
./scripts/run_backend.sh                  # http://127.0.0.1:8000

# terminal 2 — the simulated picker walk
source .venv/bin/activate
python3 simulation/simulate_picker.py

# then
open http://127.0.0.1:8000/property-registration   # the property master
open http://127.0.0.1:8000/dashboard              # live lane operations
open http://127.0.0.1:8000/survey                 # city survey overview
open http://127.0.0.1:8000/docs                   # OpenAPI, clickable
```

The workflow, in the order it happens:

```
PROPERTY REGISTRATION   the administrative record: who owns it, what kind of
        |               premises, which route, one indicative phone fix
        v
FIELD SURVEY            the physical GIS truth: entrance point, frontage line,
        |               service-zone polygon, frontage photo
        v
REVIEW                  a human approves the geometry
        |
        v
VERIFIED_FOR_OPERATION  the property can now be associated by a picker position
        |
        v
OPERATIONS              collection events, segregation status, evidence
```

Registration and survey write to the SAME `properties` row. They are separated
by responsibility, not by table - there is one property master, and the two
screens own different columns of it.

To add the property registration dashboard:

```bash
./scripts/add_property_master_dashboard.sh
```

Backs up the database, applies two additive migrations, restarts the backend and
runs the full verification. It seeds nothing: the Property Master is populated by
real registrations, not by generated rows. `DRY_RUN=1` reports and exits without
writing. See [docs/PROPERTY_MASTER.md](docs/PROPERTY_MASTER.md).

To add the city survey module (schema + demonstration data + dashboards):

```bash
./scripts/upgrade_dashboards.sh
```

One command: checks the environment, backs up the database, applies the additive
survey schema, seeds the demonstration city, builds `.venv` on Python 3.11,
installs the dependencies, restarts the backend on the new code, and runs the
full verification.

It is safe to run repeatedly, and re-running after a failure is the intended way
to recover: **if the migration has already been applied it skips steps 2–4
entirely** — no backup, no re-seed, no database writes at all — and goes straight
to whatever actually failed. `FORCE_DB=1` overrides that. It aborts if the
16-property lane count ever changes.

| URL | |
|---|---|
| `/property-registration` | the property master — register, search, edit |
| `/dashboard` | live lane operations |
| `/survey` | city survey overview |
| `/survey/field` | the field-surveyor interface |
| `/survey/review` | the reviewer queue |
| `/survey/map` | full-screen property map *(scale tooling)* |
| `/survey/assignments` | who is surveying what *(scale tooling)* |
| `/survey/qa` | GIS quality assurance *(scale tooling)* |

## 3. Verify

```bash
./scripts/verify_demo.sh
```

Checks PostgreSQL and PostGIS; 16 properties / entrances / frontages / service
zones / frontage photos with no gaps and exactly one of each per property; GiST
indexes on all three geometry tables; SRID 4326; polygon validity; zone overlap;
that every surveyed anchor sits inside its own zone; that distances really are
metres and not degrees; all three lookup decisions plus candidate ordering and
the no-silent-forcing rule; the offline logic tests; every API endpoint
including the photo route; and that the event → evidence chain survived the
migration with no orphans.

When the survey module is installed it adds four more sections: the survey
schema and admin hierarchy; that device location capture is stored honestly
(every fix keeps its accuracy and source, every adjusted entrance names who
moved it, no poor fix is quietly marked HIGH confidence); that the geometry
history trigger really fires; and the survey API, all seven survey pages, and
the full workflow test. The lane assertions stay at exactly 16 because they are
scoped by `route_id`.

```bash
python3 scripts/test_survey_api.py -v        # the survey workflow, step by step
python3 scripts/test_property_master.py -v   # the registration workflow
```

Both create their own throwaway properties and delete them at the end, including
when a step fails, so the counts every other check asserts on stay put.

The offline tests also run standalone, without a database or FastAPI:

```bash
python3 scripts/generate_real_lane.py --check   # real-lane geometry validates
python3 scripts/test_real_lane_lookup.py        # gis.py ladder on the real lane
python3 scripts/test_lookup_logic.py            # ...and on the synthetic lane
python3 scripts/utm.py                          # projection self-test
```

---

## Project structure

```
wastraq-demo/
├── backend/
│   ├── app/
│   │   ├── main.py              FastAPI app, health, /summary, /dashboard
│   │   ├── config.py            env-driven settings + GIS thresholds
│   │   ├── database.py          psycopg3 pool, raw SQL helpers
│   │   ├── models.py            vocabularies, ID generation, evidence paths
│   │   ├── schemas.py           pydantic request/response models
│   │   ├── gis.py               ★ the association logic
│   │   ├── property_master.py ★ registration: vocabularies, id generation,
│   │   │                        update-with-audit, duplicate detection
│   │   ├── routes/
│   │   │   ├── property_registry.py ★ the Property Master endpoints
│   │   │   ├── properties.py
│   │   │   ├── gis_routes.py        /gis/lookup + GeoJSON layers
│   │   │   ├── collection_events.py
│   │   │   └── evidence.py
│   │   ├── survey/              ★ the city survey module
│   │   │   ├── api.py               read side  (/survey/api/…)
│   │   │   ├── actions.py           write side: assign, capture, draw,
│   │   │   │                        submit, review
│   │   │   └── qa_checks.py         11 SQL-backed GIS quality checks
│   │   └── static/
│   │       ├── property-registration.html ★ the Property Master
│   │       ├── dashboard.html       operations command centre
│   │       ├── assets/              wq.css · wq.js · wq-map.js (no CDN)
│   │       └── survey/              index · map · assignments · field
│   │                                · review · qa · surveyors
│   ├── requirements.txt
│   └── .env.example
├── database/
│   ├── schema.sql               8 tables, GiST indexes, dashboard view
│   ├── seed.sql                 synthetic properties + 2 dummy pickers
│   ├── gis_dummy_data.sql       the old synthetic lane (kept as a reset)
│   ├── real_lane_16.sql       ★ generated: the real 16-property lane
│   ├── real_lane_16.geojson     same geometry, for a quick eyeball
│   ├── lookup_function.sql      the same decision ladder as SQL functions
│   ├── survey_schema.sql      ★ the city survey schema (additive, idempotent)
│   ├── property_master.sql    ★ registration columns, audit table, master view
│   ├── reconcile_verification_status.sql ★ pairs an approved survey with its
│   │                            property row (the seed wrote only one half)
│   └── survey_seed.sql        ★ generated: the demonstration city
├── simulation/
│   ├── simulate_picker.py       the picker walk (loads the real track)
│   └── track_real_lane.json     generated: 10 waypoints on the real lane
├── scripts/
│   ├── setup_macos.sh           install + create + load
│   ├── finish_setup.sh          load SQL, venv, start API, simulate, verify
│   ├── load_real_lane.sh      ★ back up, load the real lane, verify
│   ├── upgrade_dashboards.sh  ★ back up, migrate, seed, restart, verify
│   ├── add_property_master_dashboard.sh ★ back up, migrate, restart, verify
│   ├── setup_python_env.sh    ★ build .venv on Python 3.11 and prove it works
│   ├── py_env.sh                finds a supported interpreter, not just python3
│   ├── generate_real_lane.py  ★ surveyed anchors -> frontages + zones
│   ├── generate_survey_seed.py  regenerates database/survey_seed.sql
│   ├── test_survey_api.py       the survey workflow, end to end over HTTP
│   ├── test_property_master.py  the registration workflow, end to end
│   ├── backend_ctl.sh           stop/start that refuses to serve stale code
│   ├── utm.py                   WGS84 <-> UTM 43N, with a self-test
│   ├── pg_env.sh                puts PostgreSQL 17/16/15 on PATH
│   ├── run_backend.sh
│   ├── verify_demo.sh           the testing checklist, automated
│   ├── real_lane_testpoints.env generated: coordinates verify asserts on
│   ├── generate_gis_data.py     regenerates the old synthetic lane
│   ├── validate_geometry.py     offline check, synthetic lane
│   ├── test_real_lane_lookup.py offline decision-ladder test, real lane
│   └── test_lookup_logic.py     offline decision-ladder test, synthetic lane
└── docs/
    ├── PROPERTY_MASTER.md     ★ registration vs survey, and why they are split
    ├── QGIS.md                  Phase 6 — connect, refresh, inspect, edit
    └── SURVEY.md              ★ the city survey module in full
```

## Database

| Table | Holds |
|---|---|
| `properties` | 16 surveyed properties, `PROP-001`…`PROP-016` (admin data stays dummy) |
| `property_entrances` | `GEOMETRY(POINT, 4326)` |
| `property_frontages` | `GEOMETRY(LINESTRING, 4326)`, plus `road_side` |
| `property_service_zones` | `GEOMETRY(POLYGON, 4326)` + **GiST index** — the association surface |
| `property_photos` | one `FRONTAGE` photo per property — survey QA / dispute review only, never live recognition |
| `pickers` | `PICKER-01`, `PICKER-02` with dummy RFID UIDs |
| `collection_events` | defaults to `SEGREGATED`; `NOT_SEGREGATED` is the exception path |
| `evidence` | fake file paths for now: `COLLECTION_PROOF`, `NON_SEGREGATION_PROOF`, `VIDEO_CLIP`, `CAMERA_FRAME` |

`v_collection_summary` joins events, properties, pickers and evidence counts for
the dashboard.

The survey module adds six more tables to the same database —
`administrative_units`, `survey_users`, `survey_assignments`,
`property_surveys`, `property_geometry_history` and `property_qa_issues` — and
extends `properties`, the three geometry tables and `property_photos` with
provenance columns. Every statement in `database/survey_schema.sql` is additive:
nothing is dropped, nothing is rewritten, and the migration is idempotent.
[docs/SURVEY.md](docs/SURVEY.md) has the details.

### How the real lane geometry is built

`scripts/generate_real_lane.py` turns the 16 surveyed anchors into frontages and
service zones:

1. project every anchor to **EPSG:32643** so all work is in real metres;
2. fit the road axis by PCA — the sign of each anchor's offset from that axis
   decides its side of the road;
3. order each side along the axis and take each property's local direction from
   its same-side neighbours, so a bend is followed rather than flattened;
4. the **frontage** is a segment through the anchor, ending halfway to each
   same-side neighbour (capped at 7 m either way, shrunk by half the inter-zone
   gap so neighbours never touch);
5. the **service zone** is the quad swept from that frontage 1 m back into the
   plot and out toward the road, stopping short of the crown so the two sides
   can never meet — no circular buffers anywhere;
6. every quad is checked against every other with a separating-axis test and
   shrunk until nothing overlaps;
7. convert back to EPSG:4326 for storage.

Result: frontages 5.4–13.7 m, zones 19–51 m², zero overlaps, every surveyed
anchor inside its own zone. Re-run it any time — it validates before it writes,
and refuses to write if validation fails.

The old synthetic lane (10 plots on a straight Bengaluru street) is still
regenerable with `python3 scripts/generate_gis_data.py`.

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/` | `{"status": "Wastraq Demo Backend Running"}` |
| GET | `/health/db` | PostGIS version + row counts |
| GET | `/properties` | all dummy properties (`?route_id=` filter) |
| GET | `/properties/{id}` | attributes + entrance/frontage/zone as GeoJSON |
| GET | `/properties/{id}/events` | that property's collection history |
| GET | `/properties/{id}/photo` | the surveyed frontage photo (survey QA only) |
| GET | `/properties/{id}/photo-info` | photo row + whether the file is on disk |
| POST | `/gis/lookup` | `{"latitude": …, "longitude": …}` → decision + candidates |
| GET | `/gis/layers/{service-zones,entrances,frontages}` | GeoJSON FeatureCollections |
| GET | `/pickers` | dummy pickers |
| POST | `/collection-events` | `property_id` **or** lat/lon; 409 if not unambiguous |
| GET | `/collection-events` | `?segregation_status=` `?picker_id=` |
| GET | `/collection-events/{id}` | event + its evidence |
| POST | `/collection-events/{id}/non-segregated` | the picker's exception action |
| GET/POST | `/collection-events/{id}/evidence` | list / attach evidence |
| GET | `/summary` | everything the dashboard needs |
| GET | `/dashboard` | the HTML dashboard |

### Try it

Live coordinates are kept in `scripts/real_lane_testpoints.env`, regenerated
with the geometry so they can never go stale:

```bash
source scripts/real_lane_testpoints.env

# inside PROP-003's service zone
curl -s -X POST localhost:8000/gis/lookup -H 'content-type: application/json' \
  -d "{\"latitude\":$INSIDE_LAT,\"longitude\":$INSIDE_LON}" | python3 -m json.tool

# between PROP-004 and PROP-005 — deliberately ambiguous
curl -s -X POST localhost:8000/gis/lookup -H 'content-type: application/json' \
  -d "{\"latitude\":$AMBIG_LAT,\"longitude\":$AMBIG_LON}" | python3 -m json.tool

# the same logic straight from SQL
psql -d wastraq_demo -c "SELECT * FROM wastraq_lookup_property($INSIDE_LAT, $INSIDE_LON);"
psql -d wastraq_demo -c "SELECT * FROM wastraq_lookup_candidates($ROAD_LAT, $ROAD_LON);"
```

## The simulation

`simulation/simulate_picker.py` walks the ten generated waypoints of the real
lane (`simulation/track_real_lane.json`) and prints the decision at each one:

| # | Waypoint | Decision | Result |
|---|---|---|---|
| 1 | vehicle, well off the lane | `NO_MATCH` | nothing |
| 2 | walking in from the east end | `AUTO_ASSOCIATED` conf 0.795 | pass-by — under the 0.90 collection threshold |
| 3 | inside PROP-001's zone | `AUTO_ASSOCIATED` conf 0.99 | **event**, `SEGREGATED` |
| 4 | inside PROP-002's zone | `AUTO_ASSOCIATED` conf 0.99 | **event**, `SEGREGATED` |
| 5 | inside PROP-003's zone | `AUTO_ASSOCIATED` conf 0.99 | **event**, `SEGREGATED` |
| 6 | between PROP-004 and PROP-005 | `AMBIGUOUS` | nothing — a genuine 0.00 m tie |
| 7 | mid-carriageway | `AMBIGUOUS` | nothing — both rows plausible |
| 8 | inside PROP-016's zone (north side) | `AUTO_ASSOCIATED` conf 0.99 | **event**, `SEGREGATED` |
| 9 | inside PROP-015's zone (north side) | `AUTO_ASSOCIATED` conf 0.99 | **event**, `SEGREGATED` |
| 10 | walking back to the vehicle | `NO_MATCH` | nothing |

Then the exception path: the picker taps the RFID tag at the last property, that
event flips to `NOT_SEGREGATED`, `rfid_triggered` becomes true, `review_status`
becomes `NEEDS_REVIEW`, and a `NON_SEGREGATION_PROOF` evidence record is linked.
The original event row is updated in place — nothing is deleted or re-created.

`--track synthetic` walks the old Bengaluru lane instead.

## Tuning

`backend/.env` (metres):

| Setting | Default | Meaning |
|---|---|---|
| `SEARCH_RADIUS_M` | 15 | `ST_DWithin` radius when outside every zone |
| `AUTO_MAX_DISTANCE_M` | 3 | furthest a nearest-zone match may auto-associate |
| `AMBIGUITY_MARGIN_M` | 2 | separation the runner-up must have before we call a winner |
| `MIN_AUTO_CONFIDENCE` | 0.70 | confidence floor for auto-association |

Events below 0.85 confidence are flagged `NEEDS_REVIEW` automatically.

## QGIS

See **[docs/QGIS.md](docs/QGIS.md)** — connection setup, loading the four
layers, editing geometry, adding a property with its entrance/frontage/zone,
saving straight back to PostGIS, and swapping the synthetic lane for a real one.

## Testing checklist

- [ ] `psql -d wastraq_demo -c "SELECT PostGIS_Version();"` returns a version
- [ ] all SQL files load with no errors, `real_lane_16.sql` included
- [ ] 16 properties, 16 entrances, 16 frontages, 16 service zones, 16 frontage photos
- [ ] `PROP-001`…`PROP-016` all present; exactly one of each geometry per property
- [ ] GiST indexes on all three geometry tables; every geometry SRID 4326
- [ ] all polygons valid, none overlapping, every anchor inside its own zone
- [ ] zone areas are tens of m², and `PROP-001`→`PROP-010` measures ~82 m (metres, not degrees)
- [ ] `GET /` returns `Wastraq Demo Backend Running`
- [ ] `GET /properties` returns 16; `GET /properties/PROP-003` includes GeoJSON and a photo path
- [ ] `GET /properties/PROP-001/photo` serves the image
- [ ] lookup inside a zone → `AUTO_ASSOCIATED` with that property
- [ ] lookup between two zones → `AMBIGUOUS`, candidates nearest-first, no `property_id`
- [ ] lookup off the lane → `NO_MATCH`
- [ ] `POST /collection-events` with an ambiguous coordinate → **409**, no row written
- [ ] simulation creates events on both sides of the road
- [ ] non-segregation flips status, sets `rfid_triggered`, links evidence, preserves the row
- [ ] dashboard shows 16 mapped properties, both rows of the lane, and the event table
- [ ] QGIS: **Reload Layer** on the three layers → **Zoom to Layer** → 16 real properties

With the survey module installed, also:

- [ ] the demo lane still reports exactly 16 of everything (it is scoped by `route_id`)
- [ ] the admin hierarchy is a single-rooted tree at least three levels deep
- [ ] every captured GNSS fix stores its accuracy, its source and its raw point
- [ ] an adjusted entrance records who moved it and when, and the original fix is intact
- [ ] no survey with an accuracy worse than the threshold carries `HIGH` confidence
- [ ] editing geometry writes a `property_geometry_history` row
- [ ] `VERIFIED_FOR_OPERATION` never appears without an approved survey behind it
- [ ] all seven `/survey/*` pages are served, and the workflow test passes end to end

Everything except the QGIS line is automated by `./scripts/verify_demo.sh`.

## Troubleshooting

**`No matching distribution found for psycopg-binary` / `numpy`**
Your `.venv` is on a Python too new for the wheels. Fix:
`./scripts/setup_python_env.sh` (add `--force` to rebuild regardless). It moves
the old `.venv` to `.venv.old-<timestamp>` rather than deleting it.

**`RuntimeError: Form data requires "python-multipart" to be installed`**
The backend cannot even start — FastAPI needs it when it *builds* the survey
photo-upload route, not when that route is first called. It means the dependency
install did not complete. Same fix as above; the script now fails loudly on a
missing package instead of letting a half-installed venv through.

**The backend serves old code after an upgrade**
`scripts/backend_ctl.sh` stops by pidfile *and* by port, waits for the port to be
genuinely free, and refuses to report success unless the process answering
reports the expected version. If you started uvicorn by hand, stop it first.

**`N survey(s) carry HIGH confidence on a fix worse than 10 m`**
Demonstration data seeded before the generator applied the poor-fix cap. The
generator is fixed; correct an already-seeded database with:
```bash
psql -d wastraq_demo -f database/repair_survey_confidence.sql
```
Transactional, idempotent, touches no geometry and no captured coordinate, and
aborts if the 16-property lane count moves.

**`unbound variable` from verify_demo.sh**
Shouldn't happen any more — every optional variable is defaulted through
`wq_default` in section 0, and the demo route is resolved from the environment,
`backend/.env`, `config.py`, the running backend or the database, in that order.
If you do see one, the variable name in the error is genuinely undeclared; it is
a bug, not a configuration problem.

**The dashboard event feed returns 500 after working for a while**
`AmbiguousParameter: could not determine data type of parameter $1`. psycopg
promotes a query to a server-side `PREPARE` after five executions, and at that
point PostgreSQL must infer every parameter's type at parse time — a bare
`%(x)s IS NULL OR col = %(x)s` cannot be resolved. Every such parameter now
carries an explicit cast (`%(x)s::text`). `verify_demo.sh` calls each filterable
endpoint seven times specifically to force the prepare and catch a regression.

**A step failed and you want to re-run**
Just run `./scripts/upgrade_dashboards.sh` again. It detects an already-applied
migration and skips all database work, so re-running cannot disturb data that is
already correct.

**Which Python is it actually using?**
```bash
source scripts/py_env.sh && wq_resolve_python && echo "$WQ_PY ($WQ_PY_VER)"
.venv/bin/python -V
```

## Current status

| Area | State |
| --- | --- |
| Database (PostgreSQL 17 + PostGIS 3.6, `wastraq_demo`) | working — real lane, survey schema, property master, all migrations idempotent |
| GIS association (service zones, `ST_DWithin`, ambiguity refusal) | working — the core rule holds; never falls back to nearest vehicle GPS |
| Backend (FastAPI, `backend/app`) | working — properties, GIS lookup, collection events, evidence, survey, property master, vision |
| Frontend | vanilla JS + `wq.js` / `wq.css`, served from `backend/app/static`. No build step, no CDN — deliberate, for restricted municipal egress |
| Property Master & Survey module | working — registration, assignments, field survey, review queue, GIS QA |
| Perception (Intel RealSense D455 + YOLO + ByteTrack) | phase 1 built — persistent tracks in camera-local metres at `/picker-tracking`; no world transform yet |
| RFID | **simulated only.** `POST /collection-events/{id}/non-segregated` accepts `rfid_uid`; there is no ESP32 firmware in this repo yet |
| GNSS / IMU | not started — the simulation supplies coordinates |
| Evidence | records and links correctly; `file_path` is still a placeholder |

### What this repository does and does not contain

Committed: application source, SQL schema and seed data (all owner names,
addresses, phones and emails are synthetic), scripts, docs and the simulation.

Not committed, by design — see `.gitignore`:

- `backend/.env` — local database user and photo directory. Copy
  `backend/.env.example` to `backend/.env` after cloning.
- `logs/` — contains `pg_dump` backups of the working database.
- `models/` — YOLO weights; `scripts/add_realsense_picker_tracking.sh` fetches
  them on demand.
- `.venv/`, `backups/`, `_to_delete/` — local working state.
- Survey photos and captured frames live outside the repository (`PHOTO_DIR`).

## What becomes real later

The demo is shaped so these swap in without touching the association logic:

- **real one-lane GIS geometry** → surveyed anchors are already in; refine the
  provisional frontages/zones in QGIS (docs/QGIS.md §5) and flip `verified`
- **camera-based picker tracking** → phase 1 is **built**: a RealSense turns a
  person into a persistent `TRACK-nn` with live camera-local X / Z metres, at
  `/picker-tracking`. See **[docs/VISION.md](docs/VISION.md)**. It stops at
  camera-local coordinates on purpose — there is no vehicle and no GNSS yet, so
  there is no honest world transform to make
- **GNSS / IMU** → replaces the hard-coded `TRACK` in the simulation
- **RFID** → `POST /collection-events/{id}/non-segregated` already takes `rfid_uid`
- **real evidence** → `evidence.file_path` stops being a placeholder
- **better GNSS hardware** → `location_source` already names
  `EXTERNAL_BLUETOOTH_GNSS` and `RTK_GNSS`; adding one is an integration, not a
  migration
- **offline field survey** → the data model is already idempotent, versioned and
  timestamped for it; the client-side queue is not written
  ([docs/SURVEY.md](docs/SURVEY.md#offline-sync--future))

Deliberately not here: Kubernetes, microservices, Kafka, Redis, auth, ML models,
municipal hierarchy, monitoring. It's a demo.

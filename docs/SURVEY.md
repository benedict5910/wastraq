# The city-scale survey module

The one-lane demo proves that a picker coordinate can be turned into a property,
a segregation state and a piece of evidence. It does that for 16 properties on
2nd Cross Road.

This module answers the next question: **how do the other few hundred thousand
properties get mapped in the first place, and how do we know the mapping is any
good?**

It is a second dashboard suite on the same database, not a second system.

---

## What it is, and what it is not

| It is | It is not |
|---|---|
| A real PostgreSQL/PostGIS schema for surveying an authority | A frontend mock with hard-coded JSON |
| A field interface a surveyor uses on a phone or tablet | A replacement for QGIS |
| A reviewer workflow that gates operational use | An approval rubber stamp |
| An automated GIS QA pass over the whole property master | A machine-learning quality score |
| Offline-*ready* in its data shapes | An offline application (see [Offline](#offline-sync--future)) |

QGIS stays what it always was: the admin/GIS tool for bulk editing, careful
geometry work and inspection. A field surveyor standing at a gate should not be
running a desktop GIS. That is what `/survey/field` is for.

---

## One property master, two scopes

The survey module writes into the **same `properties` table** the operations
demo reads from. There is no second property list to keep in sync, and no
import step between "surveyed" and "operational".

The two halves are separated by **scope**, not by schema:

```
                    properties (one master)
                   /                        \
   route_id = ROUTE-DEMO-01              other routes / wards / zones
   16 surveyed properties                (empty today - the schema is
            |                             there, the rows are not)
   /dashboard  (operations)              /survey/*  (city survey)
   scoped by route_id                    scoped by administrative unit
```

Every operations endpoint takes a `route_id` and defaults to
`settings.DEMO_ROUTE_ID`, so the two views can never contaminate each other.

### Real data only

There was a synthetic city seed — about 1,300 invented properties with invented
survey percentages, surveyor throughput and QA counts. It is gone.

A demonstration you have to preface with "ignore those numbers, they're made up"
is worse than one showing 16 real properties and an honestly empty hierarchy. So
the dashboard now reports only ground that has actually been surveyed, and a
level of the hierarchy that exists but holds nothing says exactly that:
**configured, no properties loaded yet**.

```bash
./scripts/simplify_survey.sh     # removes the synthetic rows, keeps the schema
```

What survives: the 16 pilot properties and everything attached to them, the real
administrative chain they sit in (city → zone → ward → route area), and at least
one surveyor and one reviewer so the workflow still runs. What goes: every
`PROP-#####` property (5 digits — the real lane is `PROP-001`…`PROP-016`, three
digits, and cannot match), its surveys, geometry, photos and QA issues, plus any
administrative branch that was only ever scaffolding.

The cleanup runs in one transaction with post-conditions, is idempotent, and
aborts if the lane count moves by a single row.

---

## Architecture

```
  Field device (phone/tablet)          Supervisor / reviewer / GIS admin
  browser, /survey/field               browser, /survey · /map · /assignments
        |                                     · /review · /qa · /surveyors
        |  fetch()                                   |  fetch()
        v                                            v
  ┌───────────────────────────────────────────────────────────────┐
  │  FastAPI                                                      │
  │    backend/app/survey/api.py       read side  (/survey/api/…) │
  │    backend/app/survey/actions.py   write side                 │
  │    backend/app/survey/qa_checks.py 11 SQL-backed checks        │
  └───────────────────────────────────────────────────────────────┘
                              |  raw SQL (psycopg3)
                              v
  ┌───────────────────────────────────────────────────────────────┐
  │  PostgreSQL 17 + PostGIS 3.6                                  │
  │    administrative_units   survey_users     survey_assignments │
  │    property_surveys       property_qa_issues                  │
  │    property_geometry_history                                  │
  │    + properties / entrances / frontages / service_zones       │
  │      / photos  (shared with the operations demo)              │
  └───────────────────────────────────────────────────────────────┘
                              ^
                              |  direct PostGIS connection
                        QGIS (admin / bulk GIS work)
```

The front end has no build step, no framework and no CDN. `wq.css` is the design
system, `wq.js` the runtime (shell, state vocabulary, auto-refresh, drawer,
modal, toasts), `wq-map.js` a canvas slippy map with GeoJSON layers, hit-testing
and drawing tools. A municipal laptop behind a restrictive proxy still renders
every page; only the OSM basemap tiles degrade, and the map notices and turns
them off rather than hanging.

---

## Database tables

### `administrative_units` — the hierarchy, not a hard-coded ward column

Self-referencing (`parent_id`), so an authority can be as deep or shallow as it
actually is. The seed uses `CITY → ZONE → WARD → ROUTE_AREA`, but nothing in the
code requires four levels or those names — `unit_type` also accepts `DISTRICT`,
and a two-level authority works unchanged. Rollups are computed with
`WITH RECURSIVE`, so they follow whatever tree exists.

`properties.admin_unit_id` points at the deepest unit a property belongs to.

### `survey_users` — who may do what

`role ∈ (SURVEYOR, REVIEWER, GIS_ADMIN, SUPERVISOR, ADMIN)`. Enforced where it
matters: `POST /surveys/{id}/review` returns **403** unless the caller's role is
one that may review. There is no login in the demo — the acting user is chosen
in a dropdown — but the authorisation check itself is real and server-side.

### `survey_assignments` — an area handed to a surveyor

`admin_unit_id` + optional `route_id` + `assigned_to` + `due_date`. Creating one
with `include_properties` seeds a `NOT_SURVEYED` survey row for every property in
the area, so progress is measurable from day one. Existing surveys are never
overwritten.

Progress counters on the table are a convenience cache; every dashboard reads
`v_assignment_progress`, which recomputes them live from `property_surveys`.

### `property_surveys` — one survey attempt against one property

Holds the workflow state (`NOT_SURVEYED → IN_PROGRESS → SUBMITTED → APPROVED |
CORRECTION_REQUIRED | REJECTED`), the surveyor's quality assessment
(`mapping_confidence`, `source_class`, `anomaly_type[]`, `notes`), the review
outcome, and the **device location-capture block** described below.

### `property_geometry_history` — versions, not overwrites

A `BEFORE UPDATE OR DELETE` trigger on all three geometry tables snapshots the
old row before it changes:

```sql
history_id, property_id, geometry_kind, feature_id, geometry,
version, source, verified, survey_id, operation, changed_by, changed_at
```

Editing an entrance five times leaves five history rows and one current row. A
no-op touch (nothing changed but `updated_at`) is skipped, so the history stays
meaningful rather than noisy. `verify_demo.sh` proves the trigger fires by
performing an edit inside a transaction and rolling it back.

### `property_photos` — extended, not replaced

Gains `survey_id`, `captured_by`, `sha256` and `bytes`; `photo_type` widens to
`FRONTAGE | HOUSE_NUMBER | GATE | CONTEXT | OTHER`. The original "exactly one
frontage photo per property" index is replaced by one scoped per survey, so a
re-survey can carry its own photo without deleting the old one.

The SHA-256 is computed at upload. It is what makes a photo usable as evidence
later: you can show the file on disk is the file that was uploaded.

### `property_qa_issues` — what the automated checks found

`(property_id, issue_type)` is unique among `OPEN` rows, so a check that keeps
matching updates its issue rather than piling up duplicates. Issues auto-resolve
when the condition that raised them no longer holds.

---

## Field location capture

This is the part most likely to be misunderstood in a demo, so it is worth
stating plainly:

> **A device coordinate is an initial survey anchor, not ground truth.**

A phone in a narrow lane between two-storey buildings can be 15–30 m out. If
that coordinate silently became the entrance geometry, the whole association
model downstream inherits the error — and nobody would know.

So the schema keeps **both** coordinates, permanently:

| Column | Meaning |
|---|---|
| `captured_latitude` / `captured_longitude` | exactly what the device reported |
| `captured_point` | the same fix as `GEOMETRY(POINT, 4326)` |
| `location_accuracy_m` | the accuracy the device claimed |
| `location_source` | `DEVICE_GNSS` · `MANUAL_MAP` · `SIMULATED` · `EXTERNAL_BLUETOOTH_GNSS` · `RTK_GNSS` |
| `captured_at` / `captured_by` / `capture_device` | when, by whom, on what |
| `manually_adjusted` | did a human move the point afterwards |
| `adjusted_by` / `adjustment_timestamp` | who moved it, and when |

The **authoritative entrance** lives in `property_entrances` where it always
did. `POST /surveys/{id}/location` seeds it from the fix;
`POST /surveys/{id}/location/adjust` moves it and returns
`moved_from_gnss_fix_m` so the displacement is visible rather than implied. The
raw fix is never rewritten.

### When the fix is poor

`GNSS_ACCURACY_WARN_M` (default 10 m, env-configurable) is the line. Above it:

1. the API returns `poor_accuracy: true` with a human-readable
   `accuracy_warning`;
2. `mapping_confidence` is capped — a poor fix cannot be `HIGH`;
3. a `LARGE_GPS_DISPLACEMENT` QA issue is raised against the property;
4. the field UI colours the accuracy readout and offers **Retake** and
   **Adjust on map**;
5. the reviewer sees the accuracy, the source and whether anyone corrected it,
   before deciding.

Nothing about a poor fix is hidden, and nothing about it is fatal — it is
recorded, flagged, and put in front of a human.

`verify_demo.sh` asserts all of this as data invariants: every captured fix has
an accuracy and a source, every adjusted survey names who adjusted it and when,
and no survey with an accuracy worse than the threshold carries `HIGH`
confidence.

### Extending to better hardware

`location_source` is the extension point. A Bluetooth GNSS puck or an RTK
receiver reports through the same endpoint with a different `source` and a much
smaller `accuracy_m`; the thresholds, the warning path and the storage are
unchanged. **RTK is not implemented** — no correction stream, no NTRIP client,
no fix-type parsing. The vocabulary is there so adding it later is an integration,
not a migration.

In a desktop browser without geolocation permission, **Simulate fix** provides a
clearly labelled development coordinate. It writes `location_source = SIMULATED`,
so simulated data can never be mistaken for a field capture — in the database or
on the screen.

---

## The field survey screen

`/survey/field` follows one fixed order, because a surveyor at a gate should
never have to work out what to do next:

| # | Section | Control | Stored as |
|---|---|---|---|
| 1 | Property information | editable owner / contact / address / type fields | `properties` |
| 2 | Device location | **Fetch location** → Use / Retake / Adjust on map | `property_surveys.captured_*` |
| 3 | Entrance | **Mark entrance** → tap → drag → Confirm / Reset | `property_entrances` · `POINT` |
| 4 | Frontage | **Draw frontage** → Undo last point / Finish line / Reset | `property_frontages` · `LINESTRING` |
| 5 | Service zone | **Draw service zone** → Undo / Close polygon / Reset | `property_service_zones` · `POLYGON` |
| 6 | Frontage evidence | **Capture frontage photo** (camera) or upload | `property_photos` |
| 7 | Survey quality | confidence · source class · anomalies · notes | `property_surveys` |
| 8 | Actions | Save draft · Submit for review | — |

### Drawing

One tool is active at a time — starting any tool disables the other two, and a
mode bar above the map says which is live and what a tap will do. The legend is
always visible: **Entrance = Point · Frontage = Line · Service zone = Polygon**.

Every shape stays adjustable until submission. Finishing a line or closing a
polygon immediately puts draggable handles on its vertices; dragging one moves
the coordinate that will be sent to PostGIS, not a decoration on top of it.
Dragging a polygon's first vertex moves the closing vertex with it, so an edit
can never leave the ring open.

The browser exchanges plain GeoJSON with the API in `[longitude, latitude]`
order. The backend converts it with `ST_GeomFromGeoJSON` and stores it in the
existing geometry tables — the geometry model did not change, and version,
source, `created_by`, `verified` state and history all behave exactly as before.

### Automatic vs entered

Filled in by the system, never typed: WASTRAQ property id, surveyor identity,
assignment id, creation and update timestamps, geometry version, capture time.
Entered by the surveyor: owner name, phone, email, house number, street,
locality, PIN code, property type, service entity type, mapping confidence,
anomalies and notes.

### Validation before submit

`GET /survey/api/surveys/{id}/readiness` returns live blockers and warnings, and
the submit endpoint re-checks them server-side — the browser's opinion is never
what decides. Every number is measured by PostGIS on the stored rows:

**Blockers** (submission refused, 422 with the list):

- entrance, frontage, service zone and a frontage photo all present
- every geometry passes `ST_IsValid`
- frontage has ≥ 2 distinct points (`ST_RemoveRepeatedPoints` + `ST_NPoints`)
- service zone has ≥ 3 distinct vertices and `ST_Area(geometry::geography) > 0`
- the entrance is within `ENTRANCE_PROXIMITY_MAX_M` (default 20 m) of its own
  frontage or service zone

**Warnings** (recorded and shown to the reviewer, never a silent pass):

- entrance between `ENTRANCE_PROXIMITY_OK_M` (5 m) and the hard limit
- entrance outside its own service zone
- service zone larger than `MAX_SERVICE_ZONE_AREA_M2` — that is the plot, not
  the collection area

Warnings become `GEOMETRY_NEEDS_REVIEW` QA issues at submit time, so they reach
the reviewer rather than dying in a toast the surveyor already dismissed. All
four thresholds are environment-configurable in `backend/app/config.py`.

### Photos

**Capture frontage photo** opens the device camera (`capture="environment"`) and
stores `capture_method = DEVICE_CAMERA`. **Upload test file** exists for desktop
work and stores `UPLOADED_FILE`. Seeded demonstration images are `SEED_SAMPLE`.
The distinction is a stored column, not a UI label, so it can be audited later.
Each photo also carries its SHA-256 and, when a fix has been recorded, the
capture coordinates.

## The survey workflow

```
  SUPERVISOR                SURVEYOR                     REVIEWER
  ──────────                ────────                     ────────
  create assignment
  (area + route +
   surveyor + due date)
        │
        └─ seeds NOT_SURVEYED rows
                              │
                              ├─ 1 capture location  ──► raw fix stored forever
                              ├─ 2 draw entrance / frontage / service zone
                              ├─ 3 photograph the frontage (+ gate, house number)
                              ├─ 4 record confidence, source class, anomalies
                              └─ 5 submit
                                     │  blocked unless entrance + frontage +
                                     │  zone exist, are valid, and confidence
                                     │  and source class are set
                                     ▼
                                                        review queue
                                                          │
                          ┌───────────────────────────────┼──────────────┐
                          ▼                               ▼              ▼
                    RETURN FOR CORRECTION             APPROVE          REJECT
                          │                               │              │
                    back to the surveyor         verification_status   dropped
                    with reviewer notes          = VERIFIED_FOR_        from the
                                                   OPERATION            programme
                                                 geometry marked
                                                   verified
```

**Approval is the only path to operational clearance.** Submitting sets
`verification_status = FIELD_SURVEYED` and nothing more. Only `APPROVE` sets
`VERIFIED_FOR_OPERATION` and flips `verified = TRUE` on the entrance, frontage
and service zone. A returned or rejected survey removes that clearance again.

### Reviewer interface

`/survey/review` puts everything needed for one decision on one screen: the
property, the surveyor, the submission time, the frontage photo, the geometry on
a map with the **raw GNSS fix drawn separately from the final entrance**, the
device accuracy, the confidence, the anomalies, the open QA issues, and the
geometry history. Notes are required when returning or rejecting — a surveyor
cannot act on "no".

---

## GIS QA checks

`POST /survey/api/qa/run` runs eleven checks in SQL across the whole authority.
Each one either opens an issue or auto-resolves an issue that no longer holds.

| Check | Severity | Finds |
|---|---|---|
| `MISSING_ENTRANCE` | HIGH | surveyed property with no entrance point |
| `MISSING_FRONTAGE` | MEDIUM | no frontage line |
| `MISSING_SERVICE_ZONE` | HIGH | no service-zone polygon — cannot be associated at all |
| `INVALID_GEOMETRY` | CRITICAL | `ST_IsValid` fails (self-intersection, bad ring) |
| `SERVICE_ZONE_OVERLAP` | HIGH | two zones claim the same ground — the ambiguity source |
| `ENTRANCE_WRONG_SIDE` | MEDIUM | entrance not inside its own service zone |
| `LOW_MAPPING_CONFIDENCE` | LOW | surveyor themselves flagged it |
| `LARGE_GPS_DISPLACEMENT` | MEDIUM | device fix worse than the threshold, or entrance far from it |
| `PROPERTY_OUTSIDE_ASSIGNED_AREA` | MEDIUM | property is not inside the admin unit it claims |
| `PROPERTY_ROUTE_MISMATCH` | LOW | route disagrees with the assignment's route |
| `DUPLICATE_PROPERTY` | MEDIUM | two properties at effectively the same address/point |

Plus `MANUAL_REVIEW_REQUIRED`, raised by a reviewer returning a survey.

These are all data conditions, checked against PostGIS. None of them is a
heuristic score, and none of them is computed in the browser.

---

## Pages

The interface is three views. Assignments, the full-screen map and GIS QA are
fully built and still served — they are simply not in the primary navigation,
because a 16-property pilot does not need them and their presence made the
system look more complicated than it is.

| URL | Who | What |
|---|---|---|
| `/dashboard` | operations | the live lane demo — **unchanged in scope** |
| `/survey` | supervisor | **city overview**: real counts, filters, map of surveyed properties, scale path |
| `/survey/map` | anyone | every property plotted, coloured by survey status, filterable, with a detail drawer |
| `/survey/assignments` | supervisor | assignment list with live progress; create a new assignment |
| `/survey/field` | surveyor | **field survey** — location, geometry, photos, quality, submit |
| `/survey/review` | reviewer | **review queue** — photo, geometry, GPS accuracy, confidence, notes, approve / return |
| `/survey/qa` | GIS admin | detected problems, filters, run-checks, acknowledge/resolve |

---

## API

Read side, `/survey/api`:

```
GET  /admin-units                 ?unit_type=&parent_id=
GET  /admin-units/tree            recursive rollups, full_path, depth
GET  /admin-units/geojson         ?unit_type=WARD
GET  /users                       ?role=&active=
GET  /assignments                 ?status=&assigned_to=&admin_unit_id=
GET  /assignments/{id}            + the properties in it
GET  /properties                  paged; filters: unit, ward, zone, route,
                                  surveyor, status, confidence, has_qa, q
GET  /properties/geojson          the same filters, as a FeatureCollection
GET  /properties/{id}/survey      property + survey + geometry + photos
                                  + qa_issues + history + thresholds
GET  /surveys                     ?survey_status=&review_status=&surveyor_id=
GET  /qa-issues                   ?status=&severity=&issue_type=&ward_id=
GET  /analytics/overview          ?admin_unit_id=
GET  /analytics/surveyors
```

Write side:

```
POST   /assignments
POST   /properties/{id}/survey            start or resume
PATCH  /surveys/{id}                      save a draft
POST   /surveys/{id}/location             capture a device fix
POST   /surveys/{id}/location/adjust      move the entrance, keep the fix
PUT    /surveys/{id}/geometry             entrance | frontage | service_zone
DELETE /surveys/{id}/geometry/{kind}
POST   /surveys/{id}/photos               multipart; hashed, size-capped
GET    /photos/{id}/file                  path-guarded
GET    /surveys/{id}/readiness            blockers, live
POST   /surveys/{id}/submit               422 with blockers if not ready
POST   /surveys/{id}/review               APPROVE | CORRECTION_REQUIRED | REJECT
POST   /qa/run
PATCH  /qa-issues/{id}
```

Everything is visible and clickable at `/docs`.

---

## Offline sync — future

Field surveying in a dense ward means dead spots. The honest position:

**Not implemented.** There is no service worker, no local queue, no conflict
resolution, no background sync. The field page needs a connection. The UI says
so, in a labelled placeholder, rather than pretending otherwise.

**What is already offline-ready** is the data model, which is the part that is
expensive to change later:

- Every survey action is **idempotent on a stable id** (`survey_id`,
  `property_id`), so replaying a queued action twice is harmless.
- Geometry is exchanged as **GeoJSON**, which serialises to a local store
  unchanged.
- `property_geometry_history` gives every feature a **monotonic `version`**, which
  is what a conflict resolver needs to detect a stale write.
- The device capture block already records `captured_at` **separately from**
  `created_at`, so an action performed at 10:05 and synced at 14:20 keeps both
  times.
- Photos carry a **SHA-256**, so an interrupted upload can be retried and
  de-duplicated.

Adding offline later is a client-side queue plus a `version`-aware replay
endpoint. It is not a schema migration.

---

## Demonstration data

`database/survey_seed.sql` is generated by `scripts/generate_survey_seed.py`
with a fixed seed, so it is byte-identical on every machine. It contains:

- 1 city, 3 zones, 8 wards, 20 route areas
- 15 users (8 surveyors, 3 reviewers, 2 supervisors, 1 GIS admin, 1 admin)
- 20 assignments
- ~1,300 properties spread across the wards, with a realistic mix of survey states
- ~600 sets of entrance / frontage / service-zone geometry
- ~1,200 photo rows and ~330 QA issues

**All names, house numbers, addresses and employee IDs are invented.** No real
personal data is used anywhere. The photo rows point at the 16 real frontage
images as stand-in samples, and each row says so in its `notes`.

The seeded properties sit in real Mysuru wards but more than a kilometre from
the demo lane, so nothing they contain can affect a lane lookup.

---

## Running it

```bash
./scripts/upgrade_dashboards.sh
```

Backs up the database, applies `database/survey_schema.sql` and
`database/survey_seed.sql` (both idempotent, both in transactions), builds
`.venv` on Python 3.11, installs dependencies, restarts the backend, and runs
the full verification. It refuses to proceed if the 16-property lane is not
loaded, and aborts if the lane count ever changes.

**Re-running is the recovery path.** If the migration has already been applied —
six survey tables present and 500+ city properties — the script skips the
backup, the schema and the seed completely and goes straight to the step that
failed. Nothing is written to the database on such a run. `FORCE_DB=1` forces
the migration to be re-applied anyway.

### The Python environment

This module is why the interpreter had to be pinned. It adds a multipart photo
upload, and FastAPI imports `python-multipart` when it **builds** that route, not
when the route is first called — so a missing dependency is a start-up crash, not
a runtime 500.

That interacted badly with exact version pins. `psycopg-binary==3.2.3` has no
wheel for newer CPython; on a Mac whose Homebrew `python3` had rolled forward to
3.14, pip abandoned the whole resolution at that line, `python-multipart` was
never installed, and the backend then died at boot pointing at multipart — an
error three steps removed from the cause.

The fix has two halves, and both matter:

- `backend/requirements.txt` uses **ranges**, not exact pins, so pip can pick a
  version that has a wheel.
- `scripts/py_env.sh` + `scripts/setup_python_env.sh` **choose the interpreter**
  (3.11, installing it via Homebrew if necessary), rebuild `.venv` when it is on
  the wrong Python, and finish by importing `backend/app/main.py` — which builds
  every route, so the exact failure mode above is now caught by the setup script
  rather than by the running server.

`verify_demo.sh` also runs its Python helpers through `.venv/bin/python` rather
than the system `python3`, so tests that need `psycopg` or `numpy` cannot fail
for the wrong reason.

Verification of the survey module specifically:

```bash
./scripts/verify_demo.sh                 # everything, sections 1–12
python3 scripts/test_survey_api.py -v    # the workflow, step by step
```

`test_survey_api.py` creates its own throwaway property in a throwaway admin
unit, drives the entire workflow over HTTP — assign, capture, warn on a poor
fix, correct on the map, draw, block an incomplete submit, submit, return for
correction, resubmit, approve — asserts the invariants at each step, and deletes
its fixtures afterwards, including when a step fails.


---

## Scale path

The overview shows one card that answers the only question a Team Lead really
has about a pilot:

```
Pilot Lane  →  Route  →  Ward  →  Zone  →  City
```

Each level reports what is **configured** and what actually **holds surveyed
properties**, both computed live from the database — nothing on that card is
asserted in the markup. A level with data is marked *live*; a level that exists
but is empty is marked *ready*, which is the honest claim: the schema, the
queries and the UI already handle it, and loading a second route or a whole ward
adds rows, not code.

The 16 pilot properties roll up through all five levels today, because the one
route area they sit in is inside a real ward, inside a real zone, inside the
city. That is the whole demonstration, and it takes about ninety seconds:

1. **Overview** — 16 real properties, all verified, plotted on the map. Filter
   by ward or route; the counts follow.
2. **Field survey** — open one property, fetch GPS, mark the entrance, draw the
   frontage and service zone, photograph it, submit.
3. **Review queue** — the submission appears with its photo, geometry, GPS
   accuracy and confidence. Approve it, and the property becomes
   `VERIFIED_FOR_OPERATION` on the live lane dashboard.

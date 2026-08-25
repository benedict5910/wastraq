# Property Registration / Property Master

`http://127.0.0.1:8000/property-registration`

One sentence for the Team Lead:

> **Property Registration stores the administrative record. Field Survey
> captures the physical GIS truth.**

Those used to be the same screen, which meant a clerk with a phone number to
type had to open a map-drawing interface, and a surveyor standing at a gate had
to scroll past owner-contact fields. Splitting them is the whole change.

```
PROPERTY REGISTRATION   who owns it, what kind of premises, which route,
        |               one indicative phone fix
        v
FIELD SURVEY            entrance point, frontage line, service-zone polygon,
        |               frontage photo
        v
REVIEW                  a human approves the geometry
        |
        v
VERIFIED_FOR_OPERATION  the property can now be matched to a picker position
        |
        v
OPERATIONS              collection events, segregation status, evidence
```

---

## One property master

Both screens write the **same `properties` row**. There is no second table, no
"registration record" that later gets reconciled with a "survey record". They
are separated by which columns they may write:

| | Registration | Field survey |
|---|---|---|
| owner, phone, email | ✅ | ✅ |
| house number, street, locality, pincode | ✅ | ✅ |
| property type, service entity type | ✅ | ✅ |
| route, administrative unit | ✅ | ❌ |
| active / inactive | ✅ | ❌ |
| `captured_latitude` / `_longitude` / `_accuracy_m` | ✅ | ❌ |
| `property_entrances` / `_frontages` / `_service_zones` | ❌ | ✅ |
| `verification_status` | ❌ | ❌ *(reviewer only)* |

The allow-lists are `ADMIN_EDITABLE` and `SURVEY_EDITABLE` in
`backend/app/property_master.py`, and both PATCH endpoints go through the same
`apply_property_update()`. One implementation, two permission sets — so an
audit-trail fix or an address-composition fix can only ever be made once.

---

## The two coordinates are deliberately different things

`properties.captured_*` is **where the clerk was standing** when they registered
the property. `property_entrances.geometry` is **the approved entrance**.

They are stored in different tables on purpose. Collapsing them would let an
unreviewed phone fix become the thing that decides which property a picker
collected from — which is precisely the failure mode the core rule of this
project exists to prevent.

Registration writes no geometry at all. The verification runs a query proving
it:

```sql
SELECT count(*) FROM information_schema.columns
 WHERE table_name IN ('property_entrances','property_frontages','property_service_zones')
   AND column_name LIKE 'captured_%';   -- must be 0
```

---

## What a new property looks like

`POST /properties` returns a row in **`PENDING_SURVEY`** with:

- a server-generated `property_id` in the existing `PROP-nnn` format
  (minted inside the INSERT's transaction, serialised on an advisory lock, so
  two clerks registering at the same moment cannot collide)
- `mapping_confidence = 0` — nothing has been mapped yet
- no entrance, no frontage, no service zone, no photo
- `created_by`, `created_at`, `updated_at` filled in automatically

It cannot become `VERIFIED_FOR_OPERATION` from this endpoint. Only
`app.survey.actions.review_survey` sets that, and only after a reviewer approves
real geometry.

---

## Poor GPS

A registration fix worse than `REGISTRATION_ACCURACY_WARN_M` (default 25 m):

- **is recorded**, accuracy and all — it is not discarded and not rounded away
- is reported as `POOR` with the message *"Low GPS accuracy — location may need
  field correction."*
- does **not** block registration, and does **not** mark anything verified
- offers retake / save as approximate / leave it to the survey

The threshold is configuration (`app/config.py`), not a number typed into the
page — the UI reads it from `/properties/master/vocabulary`.

---

## Duplicate detection

Runs before saving a new property and on demand for an existing one. It matches
on authority property ID, house number + street, contact phone, owner name, and
distance from the captured point (`::geography`, so metres are metres).

It **warns**. It never blocks a legitimate new property and it never merges
records automatically — the clerk gets *Open existing* and *Continue creating
new*, and that is the whole interaction.

Scoring, from `_score()`:

| signal | contribution |
|---|---|
| same authority property ID | 0.95 |
| same house number **and** street | 0.85 |
| same contact phone | 0.70 |
| same house number alone | 0.55 |
| same owner name | 0.45 |
| distance | `+0.05 … +0.45`, and at ≤ 5 m at least 0.60 |

Proximity alone is normally a hint rather than a verdict — on a dense lane
neighbouring doors are a few metres apart, and flagging every neighbour would
train the clerk to click through the warning. Landing essentially *on* an
existing property's reference point is different in kind, so that alone crosses
the threshold.

`≥ 0.55` reports `POSSIBLE_DUPLICATE`; anything else is `CLEAR` and the nearby
properties are shown as context.

---

## Vocabularies were widened, never replaced

`property_type` and `service_entity_type` are two different questions — *what
kind of building is this* and *what does the vehicle service here*. A gated
community can be one common collection point.

| | registration values | legacy values, still accepted |
|---|---|---|
| `property_type` | INDEPENDENT_HOUSE, APARTMENT, GATED_COMMUNITY, SHOP, COMMERCIAL_BUILDING, OFFICE, MARKET, HOTEL, SCHOOL, HOSPITAL, INDUSTRIAL, VACANT_PROPERTY, OTHER | RESIDENTIAL, COMMERCIAL, MIXED, INSTITUTIONAL, VACANT |
| `service_entity_type` | INDIVIDUAL_PROPERTY, BUILDING, COMMERCIAL_COMPLEX, COMMON_COLLECTION_POINT, COMMUNITY_COLLECTION_POINT, OTHER | SINGLE_HOUSEHOLD, MULTI_HOUSEHOLD, APARTMENT_BLOCK, SHOP, RESTAURANT, OFFICE, INSTITUTION, BULK_GENERATOR |

The 16 pilot rows use the legacy values, so those stay legal and **no data was
rewritten**. The create form offers only the current list; the edit form keeps a
row's existing value selectable so opening the form cannot silently change it.
On display the `(legacy)` suffix is stripped — it is guidance for the dropdown,
not a property of the building.

---

## verification_status belongs to the reviewer

Exactly one thing may clear a property for operation: a reviewer approving its
field survey. `review_survey()` does two writes for that — it sets
`property_surveys.survey_status = 'APPROVED'` **and**
`properties.verification_status = 'VERIFIED_FOR_OPERATION'`.

Nothing on the registration path may write that column, in either direction. It
cannot promote a property past a review it has not had, and it cannot demote one
that has been cleared. `ADMIN_EDITABLE` and `SURVEY_EDITABLE` omit it, and
`SYSTEM_OWNED` in `property_master.py` makes that a rule rather than a
convention — `apply_property_update()` strips those columns whatever allow-list
its caller passes, so widening an allow-list later cannot quietly open the hole.

### The one-time reconciliation

The 16 pilot properties never went through that API. Their survey rows were
written directly by the seed — `survey_status = 'APPROVED'`, `review_status =
'APPROVED'`, with a reviewer id, a `reviewed_at` and review notes — but the
seed never performed the *second* write. So the property rows sat at
`FIELD_SURVEYED` while their own survey rows said they had been approved and
reviewed. The database had been telling two stories about the same property
since the lane was first loaded, which is why the operations dashboard showed
0 verified for a fully surveyed lane.

`database/reconcile_verification_status.sql` applies the missing write. It is
driven entirely by approval records already in the database:

- it only touches rows whose **own** current survey is `APPROVED`, with a
  non-null `reviewer_id` and `reviewed_at` and `review_status = 'APPROVED'`
- it only ever moves `FIELD_SURVEYED` → `VERIFIED_FOR_OPERATION`; `DISPUTED`,
  `UNVERIFIED` and `PENDING_SURVEY` are left alone
- it never moves anything down
- it attributes the change to the reviewer who approved it, not to whoever ran
  the script
- it touches no geometry, no photo, no event, no evidence
- it aborts if a row it promoted turns out not to have an approved, reviewed
  survey
- it is idempotent: the second run reports 0

Two verification checks keep the two halves in step from now on: no property
with an approved, reviewed survey may sit outside `VERIFIED_FOR_OPERATION`, and
nothing may be `VERIFIED_FOR_OPERATION` without one.

---

## Audit trail

`property_change_log` is the administrative counterpart to
`property_geometry_history`: one row per changed field, with old and new value,
who and when. `CREATED`, `UPDATED`, `LOCATION_CAPTURED`, `DEACTIVATED`,
`REACTIVATED`.

A no-op edit writes nothing. Editing an owner's phone number never touches an
approved service zone — the update only ever writes columns from the caller's
allow-list.

Properties are **deactivated, never deleted**: collection events reference them
and history has to stay readable.

---

## API

| | |
|---|---|
| `GET /properties/master` | search / filter the master table |
| `GET /properties/master/summary` | the five headline counts |
| `GET /properties/master/vocabulary` | dropdown contents, routes, admin units, next id, thresholds |
| `POST /properties` | register (201) |
| `PATCH /properties/{id}` | edit the administrative record |
| `POST /properties/duplicate-check` | pre-save check for a property that does not exist yet |
| `GET /properties/{id}/possible-duplicates` | same check for one already on file |
| `POST /properties/{id}/capture-location` | store the registration reference fix |
| `GET /properties/{id}/survey-status` | survey + GIS state, and what is missing |
| `GET /properties/{id}/history` | the administrative change log |
| `GET /properties/{id}/master` | one row of `v_property_master` |

`routes/property_registry.py` is included **before** `routes/properties.py` in
`main.py`. Both live on `/properties`, and FastAPI resolves in registration
order — the other way round, `/properties/master` would be swallowed by
`/properties/{property_id}`. `verify_demo.sh` asserts it is not.

Every filter parameter carries an explicit `::text` / `::bool` cast. psycopg
turns a query into a server-side `PREPARE` after five executions, and at that
point PostgreSQL has to infer each parameter's type from the query text alone; a
bare `$1 IS NULL` cannot be inferred and the endpoint starts failing on the
*sixth* call. The tests call each endpoint seven times in a row for exactly that
reason.

---

## Scale

Nothing here was seeded. Sixteen properties is what the database honestly holds.

The same Property Master serves a ward or a city — `administrative_units` is a
self-referencing hierarchy, the filters resolve a unit or any of its ancestors,
and the indexes are already in place. The difference between 16 rows and 160,000
is how many rows there are, not how the system works.

---

## Files

```
database/property_master.sql              additive, idempotent migration
database/reconcile_verification_status.sql  the seed's missing second write
backend/app/property_master.py            vocabularies, ids, updates, duplicates
backend/app/routes/property_registry.py   the endpoints
backend/app/static/property-registration.html   the page
scripts/add_property_master_dashboard.sh  one-command install
scripts/test_property_master.py           the registration workflow, end to end
```

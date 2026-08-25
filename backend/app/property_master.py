"""Property Master - shared registration logic.

Registration (this module) and field survey (`app.survey`) write to the
SAME `properties` row. They are separated by responsibility, not by table:

    registration  ->  who owns it, where it is on paper, what kind of
                      premises it is, and one indicative phone fix
    field survey  ->  the entrance point, the frontage line, the service
                      zone polygon and the frontage photo

Everything both screens need lives here so neither one owns it: the
vocabularies, the id generator, the update-with-audit path and the
duplicate detector. `app.survey.actions.edit_property` delegates to
`apply_property_update` rather than carrying a second copy of it.
"""

from __future__ import annotations

from typing import Any

from .config import settings
from .database import execute, fetch_all, fetch_one, get_conn

# ===========================================================================
# Vocabularies
#
# `legacy=True` means the value predates the registration dashboard. It is
# still valid in the database and still renders with a proper label, but it
# is not offered when creating a new property - otherwise the list would
# grow every time the vocabulary is refined.
# ===========================================================================
PROPERTY_TYPES: list[dict[str, Any]] = [
    {"value": "INDEPENDENT_HOUSE",   "label": "Independent house"},
    {"value": "APARTMENT",           "label": "Apartment"},
    {"value": "GATED_COMMUNITY",     "label": "Gated community"},
    {"value": "SHOP",                "label": "Shop"},
    {"value": "COMMERCIAL_BUILDING", "label": "Commercial building"},
    {"value": "OFFICE",              "label": "Office"},
    {"value": "MARKET",              "label": "Market"},
    {"value": "HOTEL",               "label": "Hotel"},
    {"value": "SCHOOL",              "label": "School"},
    {"value": "HOSPITAL",            "label": "Hospital"},
    {"value": "INDUSTRIAL",          "label": "Industrial"},
    {"value": "VACANT_PROPERTY",     "label": "Vacant property"},
    {"value": "OTHER",               "label": "Other"},
    {"value": "RESIDENTIAL",         "label": "Residential (legacy)",    "legacy": True},
    {"value": "COMMERCIAL",          "label": "Commercial (legacy)",     "legacy": True},
    {"value": "MIXED",               "label": "Mixed use (legacy)",      "legacy": True},
    {"value": "INSTITUTIONAL",       "label": "Institutional (legacy)",  "legacy": True},
    {"value": "VACANT",              "label": "Vacant (legacy)",         "legacy": True},
]

SERVICE_ENTITY_TYPES: list[dict[str, Any]] = [
    {"value": "INDIVIDUAL_PROPERTY",        "label": "Individual property"},
    {"value": "BUILDING",                   "label": "Building"},
    {"value": "COMMERCIAL_COMPLEX",         "label": "Commercial complex"},
    {"value": "COMMON_COLLECTION_POINT",    "label": "Common collection point"},
    {"value": "COMMUNITY_COLLECTION_POINT", "label": "Community collection point"},
    {"value": "OTHER",                      "label": "Other"},
    {"value": "SINGLE_HOUSEHOLD",  "label": "Single household (legacy)",  "legacy": True},
    {"value": "MULTI_HOUSEHOLD",   "label": "Multi household (legacy)",   "legacy": True},
    {"value": "APARTMENT_BLOCK",   "label": "Apartment block (legacy)",   "legacy": True},
    {"value": "SHOP",              "label": "Shop (legacy)",              "legacy": True},
    {"value": "RESTAURANT",        "label": "Restaurant (legacy)",        "legacy": True},
    {"value": "OFFICE",            "label": "Office (legacy)",            "legacy": True},
    {"value": "INSTITUTION",       "label": "Institution (legacy)",       "legacy": True},
    {"value": "BULK_GENERATOR",    "label": "Bulk generator (legacy)",    "legacy": True},
]

LOCATION_SOURCES = ["DEVICE_GEOLOCATION", "MANUAL_MAP_PICK", "MANUAL_ENTRY",
                    "IMPORTED", "SEED_SAMPLE"]

PROPERTY_TYPE_VALUES = {t["value"] for t in PROPERTY_TYPES}
SERVICE_ENTITY_VALUES = {t["value"] for t in SERVICE_ENTITY_TYPES}

# The registration screen may set these. property_id, verification_status
# and every timestamp are absent on purpose: a clerk typing an identifier
# is a data-entry bug waiting to happen, and letting registration set
# verification_status would route around the reviewer.
ADMIN_EDITABLE = (
    "authority_property_id", "house_number", "owner_name", "owner_phone",
    "owner_email", "street_name", "locality", "pincode", "formatted_address",
    "property_type", "service_entity_type", "route_id", "admin_unit_id",
    "active", "inactive_reason",
)

# The subset the field survey screen may set. Narrower: no route or
# administrative re-linkage from a phone in the field.
SURVEY_EDITABLE = (
    "house_number", "owner_name", "owner_phone", "owner_email",
    "street_name", "locality", "pincode", "formatted_address",
    "property_type", "service_entity_type",
)

ADDRESS_PARTS = ("house_number", "street_name", "locality", "pincode")

# Columns no caller of apply_property_update() may write, whatever allow-list
# it passes. Belt as well as braces: ADMIN_EDITABLE and SURVEY_EDITABLE
# already omit these, but an allow-list is a convention and this is a rule.
# verification_status in particular is the reviewer's output - a property that
# has been cleared for operation must not be downgradable by someone editing
# an owner's phone number, and a registration must not be able to promote
# itself past the review it has not had.
SYSTEM_OWNED = frozenset({
    "property_id", "verification_status", "mapping_confidence",
    "created_at", "updated_at", "created_by",
    "captured_latitude", "captured_longitude", "captured_accuracy_m",
    "captured_at", "location_source",
})


# ===========================================================================
# Identifiers
# ===========================================================================
def next_property_id() -> str:
    """PROP-017 after PROP-016.

    Server-side and inside the same transaction as the INSERT (see
    `create_property`), so two clerks registering at once cannot mint the
    same id. The width follows the existing pilot ids rather than
    introducing a second format.
    """
    row = fetch_one(
        """
        SELECT COALESCE(MAX(NULLIF(regexp_replace(property_id, '^PROP-', ''), '')::int), 0) AS n
        FROM properties WHERE property_id ~ '^PROP-[0-9]+$'
        """
    )
    return f"PROP-{int((row or {}).get('n') or 0) + 1:03d}"


def compose_address(parts: dict[str, Any]) -> str | None:
    joined = ", ".join(str(parts[k]) for k in ADDRESS_PARTS if parts.get(k))
    return joined or None


# ===========================================================================
# Audit trail
# ===========================================================================
def log_change(property_id: str, action: str, actor: str | None = None,
               field: str | None = None, old: Any = None, new: Any = None,
               note: str | None = None) -> None:
    execute(
        """
        INSERT INTO property_change_log
            (property_id, changed_by, action, field_name, old_value, new_value, note)
        VALUES (%(pid)s, %(by)s, %(action)s, %(field)s, %(old)s, %(new)s, %(note)s)
        """,
        {"pid": property_id, "by": actor, "action": action, "field": field,
         "old": None if old is None else str(old),
         "new": None if new is None else str(new), "note": note},
    )


def change_history(property_id: str, limit: int = 100) -> list[dict[str, Any]]:
    return fetch_all(
        """
        SELECT change_id, changed_at, changed_by, action, field_name,
               old_value, new_value, note
        FROM property_change_log
        WHERE property_id = %(pid)s
        ORDER BY changed_at DESC, change_id DESC
        LIMIT %(limit)s
        """,
        {"pid": property_id, "limit": limit},
    )


# ===========================================================================
# Create / update
# ===========================================================================
def create_property(fields: dict[str, Any], actor: str | None = None) -> dict[str, Any]:
    """Insert a new property in PENDING_SURVEY.

    It does NOT become VERIFIED_FOR_OPERATION here, and it cannot: only
    `app.survey.actions.review_survey` sets that, and only after a
    reviewer has approved real geometry.
    """
    data = {k: v for k, v in fields.items()
            if k in ADMIN_EDITABLE and k not in SYSTEM_OWNED and v is not None}
    data.setdefault("property_type", "OTHER")
    if not data.get("formatted_address"):
        addr = compose_address(data)
        if addr:
            data["formatted_address"] = addr

    data["created_by"] = actor
    data["updated_by"] = actor
    data["verification_status"] = "PENDING_SURVEY"
    data["mapping_confidence"] = 0.0  # nothing has been mapped yet

    # Registration reference fix, if the clerk captured one. The indicative
    # map centroid starts out equal to it and is later overwritten by the
    # surveyed entrance - the two columns stay separate on purpose.
    has_fix = fields.get("captured_latitude") is not None \
        and fields.get("captured_longitude") is not None
    if has_fix:
        data["captured_latitude"] = fields["captured_latitude"]
        data["captured_longitude"] = fields["captured_longitude"]
        data["captured_accuracy_m"] = fields.get("captured_accuracy_m")
        data["location_source"] = fields.get("location_source") or "DEVICE_GEOLOCATION"
        data["latitude"] = fields["captured_latitude"]
        data["longitude"] = fields["captured_longitude"]

    # id generation and INSERT share one transaction, serialised on an
    # advisory lock, so two clerks registering at the same moment cannot
    # mint the same number. (MAX(...) cannot be row-locked with FOR UPDATE.)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (861_001,))
        cur.execute(
            """
            SELECT COALESCE(MAX(NULLIF(regexp_replace(property_id, '^PROP-', ''), '')::int), 0) AS n
            FROM properties WHERE property_id ~ '^PROP-[0-9]+$'
            """
        )
        nxt = int((cur.fetchone() or {}).get("n") or 0) + 1
        data["property_id"] = f"PROP-{nxt:03d}"

        cols = list(data)
        col_sql = ", ".join(cols + (["captured_at"] if has_fix else []))
        val_sql = ", ".join([f"%({k})s" for k in cols] + (["now()"] if has_fix else []))
        cur.execute(
            f"INSERT INTO properties ({col_sql}) VALUES ({val_sql}) RETURNING *", data)
        row = cur.fetchone()
        conn.commit()

    assert row is not None
    log_change(row["property_id"], "CREATED", actor,
               note="Registered through the Property Master")
    if row.get("captured_latitude") is not None:
        log_change(row["property_id"], "LOCATION_CAPTURED", actor,
                   field="captured_point",
                   new=f"{row['captured_latitude']:.6f}, {row['captured_longitude']:.6f}"
                       f" (±{row.get('captured_accuracy_m') or '?'} m)")
    return row


def apply_property_update(property_id: str, fields: dict[str, Any],
                          allowed: tuple[str, ...], actor: str | None = None,
                          ) -> dict[str, Any]:
    """Update administrative attributes and record what changed.

    Never touches geometry. Editing an owner's phone number on a property
    that has an approved service zone leaves that zone exactly as it was -
    which is why this only ever writes columns from `allowed`.
    """
    current = fetch_one("SELECT * FROM properties WHERE property_id = %s", (property_id,))
    if not current:
        raise LookupError(property_id)

    data = {k: v for k, v in fields.items()
            if k in allowed and k not in SYSTEM_OWNED and v is not None}
    if not data:
        raise ValueError("Nothing to update")

    # Rebuild the display address when its parts moved but the caller did
    # not send a formatted_address of its own.
    if "formatted_address" not in data and any(k in data for k in ADDRESS_PARTS):
        merged = {k: current.get(k) for k in ADDRESS_PARTS}
        merged.update({k: v for k, v in data.items() if k in ADDRESS_PARTS})
        addr = compose_address(merged)
        if addr:
            data["formatted_address"] = addr

    changed = {k: v for k, v in data.items() if current.get(k) != v}
    if not changed:
        return current

    params = dict(changed, pid=property_id, actor=actor)
    cols = ", ".join(f"{k} = %({k})s" for k in changed)
    row = execute(
        f"UPDATE properties SET {cols}, updated_at = now(), updated_by = %(actor)s "
        f"WHERE property_id = %(pid)s RETURNING *",
        params,
    )
    for field, new in changed.items():
        log_change(property_id, "UPDATED", actor, field=field,
                   old=current.get(field), new=new)
    assert row is not None
    return row


def capture_location(property_id: str, latitude: float, longitude: float,
                     accuracy_m: float | None, source: str,
                     actor: str | None = None) -> dict[str, Any]:
    """Store the registration reference fix.

    This is NOT survey geometry. It is written to properties.captured_*
    and nowhere else: property_entrances / property_frontages /
    property_service_zones are only ever written by the survey workflow,
    so a phone fix can never become the thing that decides which property
    a picker collected from.
    """
    row = execute(
        """
        UPDATE properties
           SET captured_latitude   = %(lat)s,
               captured_longitude  = %(lon)s,
               captured_accuracy_m = %(acc)s,
               captured_at         = now(),
               location_source     = %(src)s,
               -- The indicative map centroid follows the registration fix
               -- only while there is no surveyed entrance to beat it.
               latitude  = COALESCE(latitude,  %(lat)s),
               longitude = COALESCE(longitude, %(lon)s),
               updated_at = now(),
               updated_by = %(actor)s
         WHERE property_id = %(pid)s
        RETURNING *
        """,
        {"pid": property_id, "lat": latitude, "lon": longitude,
         "acc": accuracy_m, "src": source, "actor": actor},
    )
    if not row:
        raise LookupError(property_id)
    log_change(property_id, "LOCATION_CAPTURED", actor, field="captured_point",
               new=f"{latitude:.6f}, {longitude:.6f} (±{accuracy_m or '?'} m, {source})")
    return row


def accuracy_verdict(accuracy_m: float | None) -> dict[str, Any]:
    """Poor accuracy warns; it never blocks and it never verifies."""
    warn = settings.REGISTRATION_ACCURACY_WARN_M
    if accuracy_m is None:
        return {"level": "UNKNOWN", "warn_threshold_m": warn,
                "message": "The device did not report an accuracy figure."}
    if accuracy_m > warn:
        return {"level": "POOR", "warn_threshold_m": warn,
                "message": "Low GPS accuracy - location may need field correction."}
    if accuracy_m > settings.GNSS_ACCURACY_WARN_M:
        return {"level": "FAIR", "warn_threshold_m": warn,
                "message": "Usable as a reference point. The field survey will refine it."}
    return {"level": "GOOD", "warn_threshold_m": warn,
            "message": "Good fix for a reference point."}


# ===========================================================================
# Duplicate detection
#
# Warns, never blocks, never merges. Two flats can share a house number and
# two neighbours can stand 4 m apart, so a confident-looking match is still
# only ever shown to a human with an "open the existing one" and a
# "carry on, this is new" button.
# ===========================================================================
_DUPLICATE_SQL = """
WITH probe AS (
    SELECT lower(nullif(btrim(%(authority)s::text), '')) AS authority,
           lower(nullif(btrim(%(house)s::text),     '')) AS house,
           lower(nullif(btrim(%(street)s::text),    '')) AS street,
           lower(nullif(btrim(%(owner)s::text),     '')) AS owner,
           lower(nullif(btrim(%(phone)s::text),     '')) AS phone,
           %(lat)s::double precision                     AS lat,
           %(lon)s::double precision                     AS lon,
           %(radius)s::double precision                  AS radius,
           %(exclude)s::text                             AS exclude_id
),
cand AS (
    SELECT p.property_id, p.house_number, p.street_name, p.owner_name,
           p.owner_phone, p.authority_property_id, p.formatted_address,
           p.property_type, p.route_id, p.verification_status, p.active,
           (probe.authority IS NOT NULL
                AND lower(p.authority_property_id) = probe.authority) AS match_authority,
           (probe.house  IS NOT NULL AND lower(p.house_number) = probe.house)  AS match_house,
           (probe.street IS NOT NULL AND lower(p.street_name)  = probe.street) AS match_street,
           (probe.owner  IS NOT NULL AND lower(p.owner_name)   = probe.owner)  AS match_owner,
           (probe.phone  IS NOT NULL AND lower(p.owner_phone)  = probe.phone)  AS match_phone,
           CASE WHEN probe.lat IS NOT NULL AND probe.lon IS NOT NULL
                     AND COALESCE(p.captured_latitude, p.latitude) IS NOT NULL
                THEN ST_Distance(
                        ST_SetSRID(ST_MakePoint(probe.lon, probe.lat), 4326)::geography,
                        ST_SetSRID(ST_MakePoint(
                            COALESCE(p.captured_longitude, p.longitude),
                            COALESCE(p.captured_latitude,  p.latitude)), 4326)::geography)
                END AS distance_m,
           probe.radius AS radius
    FROM properties p, probe
    WHERE (probe.exclude_id IS NULL OR p.property_id <> probe.exclude_id)
      AND (
            lower(p.authority_property_id) = probe.authority
         OR lower(p.house_number)          = probe.house
         OR lower(p.owner_name)            = probe.owner
         OR lower(p.owner_phone)           = probe.phone
         -- Cheap bounding box first so the exact geography distance is
         -- only computed for plausible neighbours.
         OR (probe.lat IS NOT NULL
             AND COALESCE(p.captured_latitude, p.latitude)
                 BETWEEN probe.lat - probe.radius / 111320.0
                     AND probe.lat + probe.radius / 111320.0
             AND COALESCE(p.captured_longitude, p.longitude)
                 BETWEEN probe.lon - probe.radius
                         / (111320.0 * GREATEST(cos(radians(probe.lat)), 0.01))
                     AND probe.lon + probe.radius
                         / (111320.0 * GREATEST(cos(radians(probe.lat)), 0.01)))
          )
)
SELECT * FROM cand
WHERE match_authority OR match_house OR match_owner OR match_phone
   OR (distance_m IS NOT NULL AND distance_m <= radius)
ORDER BY match_authority DESC, match_house DESC,
         COALESCE(distance_m, 1e9), property_id
LIMIT %(limit)s
"""


# Within this many metres of an existing property's reference point, the two
# records are close enough to be worth one question regardless of what the
# attributes say.
NEAR_DUPLICATE_M = 5.0


def _score(c: dict[str, Any], radius: float) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = 0.0
    if c["match_authority"]:
        score = max(score, 0.95)
        reasons.append(f"Same authority property ID ({c['authority_property_id']})")
    if c["match_house"] and c["match_street"]:
        score = max(score, 0.85)
        reasons.append(f"Same house number on {c['street_name']}")
    elif c["match_house"]:
        score = max(score, 0.55)
        reasons.append(f"Same house number ({c['house_number']})")
    if c["match_phone"]:
        score = max(score, 0.70)
        reasons.append("Same contact phone number")
    if c["match_owner"]:
        score = max(score, 0.45)
        reasons.append(f"Same owner name ({c['owner_name']})")
    d = c.get("distance_m")
    if d is not None and d <= radius:
        # 0 m -> +0.45, at the radius -> +0.05. Proximity alone is normally a
        # hint rather than a verdict: on a dense lane neighbouring doors are
        # a few metres apart, and flagging every neighbour would train the
        # clerk to click through the warning.
        score = min(1.0, score + 0.05 + 0.40 * (1 - d / radius))
        reasons.append(f"{d:.0f} m from the captured location")
        # ...but landing essentially ON an existing property's reference
        # point is different in kind from standing next door, so it alone is
        # enough to ask the question even when nothing else matches.
        if d <= NEAR_DUPLICATE_M:
            score = max(score, 0.60)
    return round(min(score, 0.99), 2), reasons


def find_duplicates(*, authority_property_id: str | None = None,
                    house_number: str | None = None, street_name: str | None = None,
                    owner_name: str | None = None, owner_phone: str | None = None,
                    latitude: float | None = None, longitude: float | None = None,
                    exclude_property_id: str | None = None,
                    radius_m: float | None = None, limit: int = 10) -> dict[str, Any]:
    radius = float(radius_m or settings.DUPLICATE_RADIUS_M)
    rows = fetch_all(_DUPLICATE_SQL, {
        "authority": authority_property_id, "house": house_number,
        "street": street_name, "owner": owner_name, "phone": owner_phone,
        "lat": latitude, "lon": longitude, "radius": radius,
        "exclude": exclude_property_id, "limit": limit,
    })
    out = []
    for r in rows:
        score, reasons = _score(r, radius)
        r.pop("radius", None)
        d = r.pop("distance_m", None)
        out.append({
            "property_id": r["property_id"],
            "house_number": r["house_number"],
            "street_name": r["street_name"],
            "owner_name": r["owner_name"],
            "formatted_address": r["formatted_address"],
            "property_type": r["property_type"],
            "route_id": r["route_id"],
            "verification_status": r["verification_status"],
            "active": r["active"],
            "distance_m": None if d is None else round(float(d), 1),
            "similarity": score,
            "reasons": reasons,
        })
    out.sort(key=lambda c: -c["similarity"])
    return {
        "radius_m": radius,
        "candidates": out,
        # Advice, not enforcement. The caller decides.
        "decision": "POSSIBLE_DUPLICATE" if out and out[0]["similarity"] >= 0.55 else "CLEAR",
        "note": "Duplicate detection warns only. Registration is never blocked "
                "and records are never merged automatically.",
    }

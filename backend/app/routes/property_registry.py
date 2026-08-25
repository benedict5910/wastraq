"""Property Registration / Property Master - write and search endpoints.

Mounted on the SAME `/properties` prefix as `routes.properties`, and
included ahead of it in `main.py` so the literal `/properties/master*`
paths win over `/properties/{property_id}`.

Division of labour, restated because it is the whole point of this file:

    Property Registration  stores the administrative record.
    Field Survey           captures the physical GIS truth.

So nothing here writes property_entrances, property_frontages or
property_service_zones, and nothing here can set VERIFIED_FOR_OPERATION.
Sending a property to survey is a deep link into the existing survey
screen, not a second copy of the survey logic.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..config import settings
from ..database import execute, fetch_all, fetch_one
from ..property_master import (
    ADMIN_EDITABLE,
    LOCATION_SOURCES,
    PROPERTY_TYPE_VALUES,
    PROPERTY_TYPES,
    SERVICE_ENTITY_TYPES,
    SERVICE_ENTITY_VALUES,
    accuracy_verdict,
    apply_property_update,
    capture_location,
    change_history,
    create_property,
    find_duplicates,
    log_change,
    next_property_id,
)

router = APIRouter(prefix="/properties", tags=["property master"])


# ===========================================================================
# Schemas
# ===========================================================================
class PropertyCreate(BaseModel):
    """Everything a clerk can type. No property_id: the server mints it."""
    authority_property_id: str | None = None
    house_number: str | None = None
    owner_name: str | None = None
    owner_phone: str | None = None
    owner_email: str | None = None
    street_name: str | None = None
    locality: str | None = None
    pincode: str | None = None
    formatted_address: str | None = None
    property_type: str = "OTHER"
    service_entity_type: str | None = None
    route_id: str | None = None
    admin_unit_id: str | None = None
    # Registration reference fix - NOT survey geometry.
    captured_latitude: float | None = Field(None, ge=-90, le=90)
    captured_longitude: float | None = Field(None, ge=-180, le=180)
    captured_accuracy_m: float | None = Field(None, ge=0)
    location_source: Literal["DEVICE_GEOLOCATION", "MANUAL_MAP_PICK", "MANUAL_ENTRY",
                             "IMPORTED", "SEED_SAMPLE"] | None = None
    created_by: str | None = None


class PropertyUpdate(BaseModel):
    authority_property_id: str | None = None
    house_number: str | None = None
    owner_name: str | None = None
    owner_phone: str | None = None
    owner_email: str | None = None
    street_name: str | None = None
    locality: str | None = None
    pincode: str | None = None
    formatted_address: str | None = None
    property_type: str | None = None
    service_entity_type: str | None = None
    route_id: str | None = None
    admin_unit_id: str | None = None
    active: bool | None = None
    inactive_reason: str | None = None
    updated_by: str | None = None


class LocationCapture(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    accuracy_m: float | None = Field(None, ge=0)
    source: Literal["DEVICE_GEOLOCATION", "MANUAL_MAP_PICK", "MANUAL_ENTRY",
                    "IMPORTED", "SEED_SAMPLE"] = "DEVICE_GEOLOCATION"
    captured_by: str | None = None


class DuplicateProbe(BaseModel):
    authority_property_id: str | None = None
    house_number: str | None = None
    street_name: str | None = None
    owner_name: str | None = None
    owner_phone: str | None = None
    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)
    exclude_property_id: str | None = None
    radius_m: float | None = Field(None, gt=0, le=2000)


def _validate_vocabulary(property_type: str | None, service_entity_type: str | None) -> None:
    if property_type is not None and property_type not in PROPERTY_TYPE_VALUES:
        raise HTTPException(422, f"Unknown property_type {property_type!r}")
    if service_entity_type is not None and service_entity_type not in SERVICE_ENTITY_VALUES:
        raise HTTPException(422, f"Unknown service_entity_type {service_entity_type!r}")


def _exists(property_id: str) -> None:
    if not fetch_one("SELECT 1 AS ok FROM properties WHERE property_id = %s", (property_id,)):
        raise HTTPException(404, f"Unknown property {property_id}")


# ===========================================================================
# Master list
#
# Every parameter carries an explicit ::text / ::bool cast. psycopg turns a
# query into a server-side PREPARE after five executions, and at that point
# PostgreSQL has to infer each parameter's type from the query text alone -
# a bare `$1 IS NULL` cannot be inferred and the endpoint starts failing on
# the sixth call. The casts are load-bearing, not decoration.
# ===========================================================================
_MASTER_FILTERS = """
    (%(q)s::text IS NULL
        OR m.property_id          ILIKE %(q)s
        OR m.authority_property_id ILIKE %(q)s
        OR m.owner_name           ILIKE %(q)s
        OR m.owner_phone          ILIKE %(q)s
        OR m.house_number         ILIKE %(q)s
        OR m.street_name          ILIKE %(q)s
        OR m.locality             ILIKE %(q)s
        OR m.formatted_address    ILIKE %(q)s)
AND (%(property_type)s::text       IS NULL OR m.property_type = %(property_type)s)
AND (%(service_entity_type)s::text IS NULL OR m.service_entity_type = %(service_entity_type)s)
AND (%(verification_status)s::text IS NULL OR m.verification_status = %(verification_status)s)
AND (%(survey_status)s::text       IS NULL OR m.survey_status = %(survey_status)s)
AND (%(route_id)s::text            IS NULL OR m.route_id = %(route_id)s)
AND (%(admin_unit_id)s::text       IS NULL OR m.admin_unit_id = %(admin_unit_id)s
        OR m.ward_id = %(admin_unit_id)s OR m.zone_id = %(admin_unit_id)s)
AND (%(active)s::bool              IS NULL OR m.active = %(active)s)
"""


def _master_params(**kw) -> dict[str, Any]:
    p = {k: kw.get(k) for k in
         ("property_type", "service_entity_type", "verification_status",
          "survey_status", "route_id", "admin_unit_id", "active")}
    q = kw.get("q")
    p["q"] = f"%{q.strip()}%" if q and q.strip() else None
    return p


@router.get("/master")
def property_master(
    q: str | None = Query(None, description="Property ID, authority ID, owner, phone, "
                                            "house number, street or address"),
    property_type: str | None = None,
    service_entity_type: str | None = None,
    verification_status: str | None = None,
    survey_status: str | None = None,
    route_id: str | None = None,
    admin_unit_id: str | None = None,
    active: bool | None = None,
    limit: int = Query(100, ge=1, le=2000),
    offset: int = Query(0, ge=0),
):
    """The Property Master table. Real rows only - nothing is synthesised."""
    params = _master_params(
        q=q, property_type=property_type, service_entity_type=service_entity_type,
        verification_status=verification_status, survey_status=survey_status,
        route_id=route_id, admin_unit_id=admin_unit_id, active=active)
    total = fetch_one(
        f"SELECT count(*) AS n FROM v_property_master m WHERE {_MASTER_FILTERS}", params)
    rows = fetch_all(
        f"""
        SELECT m.* FROM v_property_master m
        WHERE {_MASTER_FILTERS}
        ORDER BY m.last_activity_at DESC, m.property_id
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        dict(params, limit=limit, offset=offset),
    )
    return {"total": (total or {}).get("n", 0), "limit": limit, "offset": offset,
            "items": rows}


@router.get("/master/summary")
def property_master_summary(route_id: str | None = None, admin_unit_id: str | None = None):
    """The five headline counts. Every one of them is a COUNT(*) - if the
    number is 16 it is because there are 16 rows."""
    params = _master_params(route_id=route_id, admin_unit_id=admin_unit_id)
    row = fetch_one(
        f"""
        SELECT count(*)                                                    AS total,
               count(*) FILTER (WHERE m.active)                            AS active,
               count(*) FILTER (WHERE NOT m.active)                        AS inactive,
               count(*) FILTER (WHERE m.verification_status IN
                        ('VERIFIED_FOR_OPERATION','FIELD_VERIFIED'))       AS verified,
               count(*) FILTER (WHERE m.active AND m.survey_status IN
                        ('NOT_SURVEYED','IN_PROGRESS','CORRECTION_REQUIRED')) AS pending_survey,
               count(*) FILTER (WHERE m.survey_status = 'SUBMITTED')       AS pending_review,
               count(*) FILTER (WHERE m.has_entrance AND m.has_frontage
                                  AND m.has_service_zone)                  AS fully_mapped,
               count(*) FILTER (WHERE m.captured_latitude IS NOT NULL)     AS with_captured_location
        FROM v_property_master m
        WHERE {_MASTER_FILTERS}
        """,
        params,
    ) or {}
    row["source"] = "COUNT(*) over the properties table - no synthetic rows"
    return row


@router.get("/master/vocabulary")
def property_master_vocabulary():
    """Dropdown contents. Routes and administrative units come from the
    database, so a deployment with a different jurisdiction shape needs no
    code change."""
    return {
        "property_types": PROPERTY_TYPES,
        "service_entity_types": SERVICE_ENTITY_TYPES,
        "location_sources": LOCATION_SOURCES,
        "verification_statuses": ["PENDING_SURVEY", "UNVERIFIED", "FIELD_SURVEYED",
                                  "FIELD_VERIFIED", "VERIFIED_FOR_OPERATION", "DISPUTED"],
        "survey_statuses": ["NOT_SURVEYED", "IN_PROGRESS", "SUBMITTED", "APPROVED",
                            "CORRECTION_REQUIRED", "REJECTED"],
        "routes": fetch_all(
            "SELECT DISTINCT route_id FROM properties "
            "WHERE route_id IS NOT NULL ORDER BY route_id"),
        "admin_units": fetch_all(
            """
            SELECT admin_unit_id, name, unit_type, parent_id
            FROM administrative_units WHERE active
            ORDER BY unit_type, name
            """),
        "next_property_id": next_property_id(),
        "thresholds": {
            "duplicate_radius_m": settings.DUPLICATE_RADIUS_M,
            "registration_accuracy_warn_m": settings.REGISTRATION_ACCURACY_WARN_M,
            "gnss_accuracy_warn_m": settings.GNSS_ACCURACY_WARN_M,
        },
    }


# ===========================================================================
# Create / update
# ===========================================================================
@router.post("", status_code=201)
def register_property(req: PropertyCreate):
    """Register a new property.

    It lands in PENDING_SURVEY with no geometry. It becomes
    VERIFIED_FOR_OPERATION only when a reviewer approves a field survey -
    there is no path from this endpoint to that state.
    """
    _validate_vocabulary(req.property_type, req.service_entity_type)
    # A brand-new route_id is legitimate (the first property on a new round),
    # so route is not validated against existing rows. An admin_unit_id is a
    # foreign key and a typo there would silently orphan the property.
    if req.admin_unit_id and not fetch_one(
            "SELECT 1 AS ok FROM administrative_units WHERE admin_unit_id = %s",
            (req.admin_unit_id,)):
        raise HTTPException(422, f"Unknown admin_unit_id {req.admin_unit_id!r}")

    fields = req.model_dump(exclude_none=True)
    actor = fields.pop("created_by", None)
    row = create_property(fields, actor)

    dupes = find_duplicates(
        authority_property_id=req.authority_property_id, house_number=req.house_number,
        street_name=req.street_name, owner_name=req.owner_name,
        owner_phone=req.owner_phone, latitude=req.captured_latitude,
        longitude=req.captured_longitude, exclude_property_id=row["property_id"])

    return {
        "property": row,
        "next_step": "FIELD_SURVEY",
        "survey_url": f"/survey/field?property={row['property_id']}",
        "accuracy": accuracy_verdict(req.captured_accuracy_m),
        # Reported after the fact as well as before, so a clerk who skipped
        # the pre-save check still sees it.
        "possible_duplicates": dupes["candidates"],
    }


@router.patch("/{property_id}")
def update_property(property_id: str, req: PropertyUpdate):
    """Edit the administrative record.

    Geometry is untouched. A property with an approved service zone keeps
    that zone when its owner's phone number changes - this endpoint only
    ever writes columns in ADMIN_EDITABLE.
    """
    _exists(property_id)
    _validate_vocabulary(req.property_type, req.service_entity_type)
    fields = req.model_dump(exclude_none=True)
    actor = fields.pop("updated_by", None)
    try:
        row = apply_property_update(property_id, fields, ADMIN_EDITABLE, actor)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, f"Unknown property {property_id}") from exc

    if req.active is False:
        log_change(property_id, "DEACTIVATED", actor, note=req.inactive_reason)
    elif req.active is True:
        # exclude_none drops a null inactive_reason, so clear it explicitly -
        # otherwise a reactivated property keeps saying why it was retired.
        row = execute("UPDATE properties SET inactive_reason = NULL "
                      "WHERE property_id = %s RETURNING *", (property_id,)) or row
        log_change(property_id, "REACTIVATED", actor)
    return row


# ===========================================================================
# Registration location
# ===========================================================================
@router.post("/{property_id}/capture-location")
def property_capture_location(property_id: str, req: LocationCapture):
    """Store the registration reference fix on the property.

    Deliberately does not create an entrance. A poor fix is recorded and
    flagged, never rejected, and never treated as verification.
    """
    _exists(property_id)
    row = capture_location(property_id, req.latitude, req.longitude,
                           req.accuracy_m, req.source, req.captured_by)
    verdict = accuracy_verdict(req.accuracy_m)
    return {
        "property_id": property_id,
        "captured_latitude": row["captured_latitude"],
        "captured_longitude": row["captured_longitude"],
        "captured_accuracy_m": row["captured_accuracy_m"],
        "captured_at": row["captured_at"],
        "location_source": row["location_source"],
        "verification_status": row["verification_status"],
        "accuracy": verdict,
        "note": "Reference location only. The entrance, frontage and service "
                "zone are captured in the field survey and reviewed there.",
    }


# ===========================================================================
# Duplicate detection
# ===========================================================================
@router.post("/duplicate-check")
def duplicate_check(req: DuplicateProbe):
    """Pre-save check for a property that does not exist yet."""
    return find_duplicates(**req.model_dump())


@router.get("/{property_id}/possible-duplicates")
def possible_duplicates(property_id: str, radius_m: float | None = Query(None, gt=0, le=2000)):
    """Same check, for a property already on file."""
    row = fetch_one(
        """
        SELECT authority_property_id, house_number, street_name, owner_name, owner_phone,
               COALESCE(captured_latitude, latitude)   AS lat,
               COALESCE(captured_longitude, longitude) AS lon
        FROM properties WHERE property_id = %s
        """,
        (property_id,),
    )
    if not row:
        raise HTTPException(404, f"Unknown property {property_id}")
    return find_duplicates(
        authority_property_id=row["authority_property_id"],
        house_number=row["house_number"], street_name=row["street_name"],
        owner_name=row["owner_name"], owner_phone=row["owner_phone"],
        latitude=row["lat"], longitude=row["lon"],
        exclude_property_id=property_id, radius_m=radius_m)


# ===========================================================================
# Survey state seen from the registration side
# ===========================================================================
@router.get("/{property_id}/survey-status")
def property_survey_status(property_id: str):
    """What the field survey has produced for this property, and what is
    still missing. Read-only: the survey screen owns the writing."""
    row = fetch_one("SELECT * FROM v_property_master WHERE property_id = %s", (property_id,))
    if not row:
        raise HTTPException(404, f"Unknown property {property_id}")

    missing = [name for name, present in (
        ("entrance", row["has_entrance"]),
        ("frontage", row["has_frontage"]),
        ("service_zone", row["has_service_zone"]),
        ("frontage_photo", row["has_frontage_photo"]),
    ) if not present]

    return {
        "property_id": property_id,
        "verification_status": row["verification_status"],
        "survey_status": row["survey_status"],
        "survey_id": row["survey_id"],
        "surveyor_id": row["surveyor_id"],
        "surveyor_name": row["surveyor_name"],
        "submitted_at": row["submitted_at"],
        "reviewed_at": row["reviewed_at"],
        "review_status": row["review_status"],
        "open_qa_issues": row["open_qa_issues"],
        "gis": {
            "entrance": row["has_entrance"],
            "frontage": row["has_frontage"],
            "service_zone": row["has_service_zone"],
            "frontage_photo": row["has_frontage_photo"],
        },
        "missing": missing,
        "ready_for_operation": row["verification_status"] == "VERIFIED_FOR_OPERATION",
        "survey_url": f"/survey/field?property={property_id}",
        "captured_location": (
            None if row["captured_latitude"] is None else {
                "latitude": row["captured_latitude"],
                "longitude": row["captured_longitude"],
                "accuracy_m": row["captured_accuracy_m"],
                "captured_at": row["captured_at"],
                "source": row["location_source"],
                "accuracy_verdict": accuracy_verdict(row["captured_accuracy_m"]),
            }),
    }


@router.get("/{property_id}/history")
def property_history(property_id: str, limit: int = Query(100, ge=1, le=500)):
    """Administrative change log. The GIS counterpart is
    property_geometry_history, exposed by the survey module."""
    _exists(property_id)
    return {"property_id": property_id, "items": change_history(property_id, limit)}


@router.get("/{property_id}/master")
def property_master_detail(property_id: str):
    """One row of v_property_master. Used by the detail panel."""
    row = fetch_one("SELECT * FROM v_property_master WHERE property_id = %s", (property_id,))
    if not row:
        raise HTTPException(404, f"Unknown property {property_id}")
    return row

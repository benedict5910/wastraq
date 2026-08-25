"""Survey module - the write side: the survey workflow itself.

    open property -> start survey -> capture location -> photos
    -> entrance / frontage / service zone -> confidence -> submit
    -> reviewer approves -> verification_status = VERIFIED_FOR_OPERATION

Rules enforced here rather than in the UI:

* A submission is refused unless entrance, frontage and service zone all
  exist and PostGIS reports the polygon valid.
* The raw device fix is never overwritten by a map correction. Both are
  kept, along with who moved it and when.
* A GNSS fix worse than the configured threshold cannot silently become
  verified geometry: it downgrades mapping confidence and raises a QA issue.
* Approving is the ONLY thing that sets VERIFIED_FOR_OPERATION.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Literal

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..database import execute, fetch_all, fetch_one
from ..property_master import (
    PROPERTY_TYPE_VALUES,
    SERVICE_ENTITY_VALUES,
    SURVEY_EDITABLE,
    apply_property_update,
)
from . import qa_checks

router = APIRouter(prefix="/survey/api", tags=["survey-actions"])

GEOM_KINDS = {
    "entrance":     ("property_entrances", "entrance_id", "ENT", "Point"),
    "frontage":     ("property_frontages", "frontage_id", "FRONT", "LineString"),
    "service_zone": ("property_service_zones", "zone_id", "SZ", "Polygon"),
}


# ===========================================================================
# request models
# ===========================================================================
class AssignmentCreate(BaseModel):
    admin_unit_id: str
    route_id: str | None = None
    assigned_to: str | None = None
    assigned_by: str | None = None
    due_date: str | None = None
    include_properties: bool = Field(
        True, description="Create NOT_SURVEYED rows for every property in the unit.")


class PropertyEdit(BaseModel):
    """The fields a surveyor may change on site.

    Deliberately does NOT include property_id, route_id, admin_unit_id,
    verification_status or any timestamp. Those are system-owned: a surveyor
    typing an identifier is a data-entry bug waiting to happen, and letting
    the field screen set verification_status would route around the reviewer.
    """
    owner_name: str | None = None
    owner_phone: str | None = None
    owner_email: str | None = None
    house_number: str | None = None
    street_name: str | None = None
    locality: str | None = None
    pincode: str | None = None
    formatted_address: str | None = None
    property_type: str | None = None
    # Validated against app.property_master.SERVICE_ENTITY_VALUES rather than
    # a Literal, so the registration and survey screens can never drift onto
    # two different vocabularies.
    service_entity_type: str | None = None
    updated_by: str | None = None


class SurveyStart(BaseModel):
    surveyor_id: str
    assignment_id: str | None = None


class SurveyDraft(BaseModel):
    mapping_confidence: Literal["HIGH", "MEDIUM", "LOW"] | None = None
    source_class: Literal["VERIFIED_FIELD_SURVEY", "AUTHORITY_GIS", "THIRD_PARTY_MAP",
                          "APPROXIMATE_GEOCODE", "UNVERIFIED"] | None = None
    notes: str | None = None
    anomaly_type: list[str] | None = None


class LocationCapture(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    accuracy_m: float | None = Field(None, ge=0, le=10000)
    source: Literal["DEVICE_GNSS", "MANUAL_MAP", "SIMULATED",
                    "EXTERNAL_BLUETOOTH_GNSS", "RTK_GNSS"] = "DEVICE_GNSS"
    device: str | None = None
    captured_by: str | None = None
    set_entrance: bool = Field(
        True, description="Also place the entrance point here (it can be moved afterwards).")


class LocationAdjust(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    adjusted_by: str | None = None


class GeometryUpdate(BaseModel):
    kind: Literal["entrance", "frontage", "service_zone"]
    geojson: dict[str, Any]
    road_side: Literal["NORTH", "SOUTH", "EAST", "WEST"] | None = None
    updated_by: str | None = None


class SubmitRequest(BaseModel):
    surveyor_id: str | None = None
    notes: str | None = None


class ReviewRequest(BaseModel):
    action: Literal["APPROVE", "CORRECTION_REQUIRED", "REJECT"]
    reviewer_id: str
    review_notes: str | None = None


class QAIssueUpdate(BaseModel):
    status: Literal["OPEN", "ACKNOWLEDGED", "RESOLVED", "WONT_FIX"]
    resolved_by: str | None = None


# ===========================================================================
# helpers
# ===========================================================================
def _next_id(table: str, column: str, prefix: str, width: int = 6) -> str:
    row = fetch_one(
        f"""
        SELECT COALESCE(MAX(NULLIF(regexp_replace({column}, '^{prefix}-', ''), '')::int), 0) AS n
        FROM {table} WHERE {column} ~ '^{prefix}-[0-9]+$'
        """
    )
    return f"{prefix}-{int((row or {}).get('n') or 0) + 1:0{width}d}"


def _survey_or_404(survey_id: str) -> dict:
    row = fetch_one("SELECT * FROM property_surveys WHERE survey_id = %s", (survey_id,))
    if not row:
        raise HTTPException(404, f"Unknown survey {survey_id}")
    return row


def _touch(survey_id: str) -> None:
    execute("UPDATE property_surveys SET updated_at = now() WHERE survey_id = %s", (survey_id,))


# ===========================================================================
# assignments
# ===========================================================================
@router.post("/assignments", status_code=201)
def create_assignment(req: AssignmentCreate):
    unit = fetch_one("SELECT * FROM administrative_units WHERE admin_unit_id = %s",
                     (req.admin_unit_id,))
    if not unit:
        raise HTTPException(404, f"Unknown administrative unit {req.admin_unit_id}")
    if req.assigned_to and not fetch_one(
            "SELECT 1 AS ok FROM survey_users WHERE user_id = %s AND role = 'SURVEYOR'",
            (req.assigned_to,)):
        raise HTTPException(404, f"Unknown surveyor {req.assigned_to}")

    aid = _next_id("survey_assignments", "assignment_id", "ASG", 4)
    props = fetch_all(
        """
        WITH RECURSIVE sub AS (
            SELECT admin_unit_id FROM administrative_units WHERE admin_unit_id = %s
            UNION ALL
            SELECT c.admin_unit_id FROM administrative_units c JOIN sub ON c.parent_id = sub.admin_unit_id
        )
        SELECT p.property_id FROM properties p WHERE p.admin_unit_id IN (SELECT admin_unit_id FROM sub)
          AND (%s::text IS NULL OR p.route_id = %s)
        ORDER BY p.property_id
        """,
        (req.admin_unit_id, req.route_id, req.route_id),
    )

    execute(
        """
        INSERT INTO survey_assignments (assignment_id, admin_unit_id, route_id, assigned_to,
            assigned_by, status, total_properties, due_date)
        VALUES (%s, %s, %s, %s, %s, 'NOT_STARTED', %s, %s)
        """,
        (aid, req.admin_unit_id, req.route_id, req.assigned_to, req.assigned_by,
         len(props), req.due_date),
    )

    created = 0
    if req.include_properties:
        seq = int(_next_id("property_surveys", "survey_id", "SRV").split("-")[1])
        for p in props:
            exists = fetch_one(
                "SELECT 1 AS ok FROM property_surveys WHERE property_id = %s AND assignment_id = %s",
                (p["property_id"], aid))
            if exists:
                continue
            execute(
                """
                INSERT INTO property_surveys (survey_id, property_id, assignment_id,
                    surveyor_id, survey_status)
                VALUES (%s, %s, %s, %s, 'NOT_SURVEYED')
                ON CONFLICT (property_id, assignment_id) DO NOTHING
                """,
                (f"SRV-{seq:06d}", p["property_id"], aid, req.assigned_to),
            )
            seq += 1
            created += 1

    return {"assignment_id": aid, "properties_in_scope": len(props),
            "survey_rows_created": created,
            "assignment": fetch_one("SELECT * FROM v_assignment_progress WHERE assignment_id = %s",
                                    (aid,))}


# ===========================================================================
# survey lifecycle
# ===========================================================================
@router.patch("/properties/{property_id}")
def edit_property(property_id: str, req: PropertyEdit):
    """Update the surveyor-editable attributes of a property.

    Same write path as the Property Master's PATCH /properties/{id}, with a
    narrower allow-list: no route or administrative re-linkage from a phone
    in the field. One implementation, two permission sets - see
    app.property_master.apply_property_update.
    """
    if not fetch_one("SELECT 1 AS ok FROM properties WHERE property_id = %s", (property_id,)):
        raise HTTPException(404, f"Unknown property {property_id}")

    if req.property_type is not None and req.property_type not in PROPERTY_TYPE_VALUES:
        raise HTTPException(422, f"Unknown property_type {req.property_type!r}")
    if req.service_entity_type is not None \
            and req.service_entity_type not in SERVICE_ENTITY_VALUES:
        raise HTTPException(422, f"Unknown service_entity_type {req.service_entity_type!r}")

    fields = req.model_dump(exclude_none=True)
    actor = fields.pop("updated_by", None)
    try:
        return apply_property_update(property_id, fields, SURVEY_EDITABLE, actor)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, f"Unknown property {property_id}") from exc


@router.post("/properties/{property_id}/survey", status_code=201)
def start_survey(property_id: str, req: SurveyStart):
    """Start (or resume) the survey for a property."""
    if not fetch_one("SELECT 1 AS ok FROM properties WHERE property_id = %s", (property_id,)):
        raise HTTPException(404, f"Unknown property {property_id}")
    if not fetch_one("SELECT 1 AS ok FROM survey_users WHERE user_id = %s", (req.surveyor_id,)):
        raise HTTPException(404, f"Unknown user {req.surveyor_id}")

    existing = fetch_one(
        """
        SELECT * FROM property_surveys
        WHERE property_id = %s AND (%s::text IS NULL OR assignment_id = %s)
        ORDER BY created_at DESC LIMIT 1
        """,
        (property_id, req.assignment_id, req.assignment_id),
    )
    if existing:
        if existing["survey_status"] in ("NOT_SURVEYED", "CORRECTION_REQUIRED"):
            row = execute(
                """
                UPDATE property_surveys
                   SET survey_status = 'IN_PROGRESS',
                       surveyor_id = COALESCE(%s, surveyor_id),
                       survey_started_at = COALESCE(survey_started_at, now()),
                       updated_at = now()
                 WHERE survey_id = %s RETURNING *
                """,
                (req.surveyor_id, existing["survey_id"]),
            )
            return row
        return existing

    sid = _next_id("property_surveys", "survey_id", "SRV")
    return execute(
        """
        INSERT INTO property_surveys (survey_id, property_id, assignment_id, surveyor_id,
            survey_status, survey_started_at)
        VALUES (%s, %s, %s, %s, 'IN_PROGRESS', now()) RETURNING *
        """,
        (sid, property_id, req.assignment_id, req.surveyor_id),
    )


@router.patch("/surveys/{survey_id}")
def save_draft(survey_id: str, req: SurveyDraft):
    s = _survey_or_404(survey_id)
    if s["survey_status"] == "APPROVED":
        raise HTTPException(409, "This survey is already approved; start a re-survey instead.")
    return execute(
        """
        UPDATE property_surveys
           SET mapping_confidence = COALESCE(%s, mapping_confidence),
               source_class       = COALESCE(%s, source_class),
               notes              = COALESCE(%s, notes),
               anomaly_type       = COALESCE(%s, anomaly_type),
               survey_status      = CASE WHEN survey_status = 'NOT_SURVEYED'
                                         THEN 'IN_PROGRESS' ELSE survey_status END,
               survey_started_at  = COALESCE(survey_started_at, now()),
               updated_at         = now()
         WHERE survey_id = %s RETURNING *
        """,
        (req.mapping_confidence, req.source_class, req.notes, req.anomaly_type, survey_id),
    )


# ---------------------------------------------------------------------------
# location capture
# ---------------------------------------------------------------------------
@router.post("/surveys/{survey_id}/location")
def capture_location(survey_id: str, req: LocationCapture):
    """Record a device GNSS fix.

    The raw fix is stored permanently in captured_* / captured_point. If
    `set_entrance` is true the entrance point is *seeded* from it - the
    surveyor can still move the entrance afterwards, and doing so does not
    alter the recorded fix.
    """
    s = _survey_or_404(survey_id)
    poor = (req.accuracy_m is not None
            and req.accuracy_m > settings.GNSS_ACCURACY_WARN_M)

    row = execute(
        """
        UPDATE property_surveys
           SET captured_latitude   = %s,
               captured_longitude  = %s,
               captured_point      = ST_SetSRID(ST_MakePoint(%s, %s), 4326),
               location_accuracy_m = %s,
               location_source     = %s,
               capture_device      = %s,
               captured_at         = now(),
               captured_by         = COALESCE(%s, surveyor_id),
               manually_adjusted   = FALSE,
               survey_status       = CASE WHEN survey_status IN ('NOT_SURVEYED')
                                          THEN 'IN_PROGRESS' ELSE survey_status END,
               survey_started_at   = COALESCE(survey_started_at, now()),
               -- a poor fix cannot be HIGH confidence, whatever was set before
               mapping_confidence  = CASE WHEN %s AND mapping_confidence = 'HIGH'
                                          THEN 'MEDIUM' ELSE mapping_confidence END,
               updated_at          = now()
         WHERE survey_id = %s RETURNING *
        """,
        (req.latitude, req.longitude, req.longitude, req.latitude, req.accuracy_m,
         req.source, req.device, req.captured_by, poor, survey_id),
    )

    if req.set_entrance:
        _upsert_geometry(
            s["property_id"], "entrance",
            {"type": "Point", "coordinates": [req.longitude, req.latitude]},
            survey_id=survey_id, updated_by=(req.captured_by or s.get("surveyor_id")),
            source=("FIELD_SURVEY" if not poor else "FIELD_SURVEY_LOW_ACCURACY"),
            road_side=None)

    warning = None
    if poor:
        warning = (f"Reported accuracy {req.accuracy_m} m is worse than the "
                   f"{settings.GNSS_ACCURACY_WARN_M:g} m threshold. Move the point on the map "
                   f"or retake the fix; mapping confidence has been capped at MEDIUM.")
        _raise_qa(s["property_id"], survey_id, "LARGE_GPS_DISPLACEMENT", "MEDIUM",
                  f"Device reported {req.accuracy_m} m accuracy at capture time.")

    return {"survey": row, "accuracy_warning": warning,
            "threshold_m": settings.GNSS_ACCURACY_WARN_M,
            "poor_accuracy": poor}


@router.post("/surveys/{survey_id}/location/adjust")
def adjust_location(survey_id: str, req: LocationAdjust):
    """Move the entrance on the map. The original GNSS fix is left intact."""
    s = _survey_or_404(survey_id)
    _upsert_geometry(s["property_id"], "entrance",
                     {"type": "Point", "coordinates": [req.longitude, req.latitude]},
                     survey_id=survey_id, updated_by=req.adjusted_by,
                     source="MANUAL_MAP_ADJUSTMENT", road_side=None)
    row = execute(
        """
        UPDATE property_surveys
           SET manually_adjusted = TRUE,
               adjustment_timestamp = now(),
               adjusted_by = COALESCE(%s, surveyor_id),
               updated_at = now()
         WHERE survey_id = %s RETURNING *
        """,
        (req.adjusted_by, survey_id),
    )
    displacement = None
    if s.get("captured_latitude") is not None:
        d = fetch_one(
            """
            SELECT ROUND(ST_Distance(
                     ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                     ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography)::numeric, 2) AS m
            """,
            (s["captured_longitude"], s["captured_latitude"], req.longitude, req.latitude),
        )
        displacement = float((d or {}).get("m") or 0)
    return {"survey": row, "moved_from_gnss_fix_m": displacement}


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------
def _upsert_geometry(property_id: str, kind: str, geojson: dict, *, survey_id: str | None,
                     updated_by: str | None, source: str, road_side: str | None) -> dict:
    table, id_col, prefix, want = GEOM_KINDS[kind]
    if geojson.get("type") != want:
        raise HTTPException(422, f"{kind} must be a GeoJSON {want}, got {geojson.get('type')!r}")

    gj = json.dumps(geojson)
    valid = fetch_one(
        "SELECT ST_IsValid(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)) AS ok, "
        "       ST_IsValidReason(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)) AS why", (gj, gj))
    if valid and valid.get("ok") is False:
        raise HTTPException(422, f"Invalid {kind} geometry: {valid.get('why')}")

    existing = fetch_one(f"SELECT * FROM {table} WHERE property_id = %s LIMIT 1", (property_id,))
    if existing:
        # the BEFORE UPDATE trigger snapshots the old row into history
        extra = ", road_side = %s" if kind == "frontage" and road_side else ""
        params: list[Any] = [gj, source, survey_id, updated_by]
        if extra:
            params.append(road_side)
        params.append(existing[id_col])
        return execute(
            f"""
            UPDATE {table}
               SET geometry = ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326),
                   source = %s, survey_id = %s, created_by = COALESCE(%s, created_by),
                   verified = FALSE, verified_by = NULL, verified_at = NULL,
                   version = version + 1, updated_at = now(){extra}
             WHERE {id_col} = %s RETURNING {id_col}, version, verified, source
            """,
            tuple(params),
        )

    new_id = _next_geo_id(table, id_col, prefix)
    if kind == "frontage":
        return execute(
            f"""
            INSERT INTO {table} ({id_col}, property_id, geometry, road_side, verified, source,
                version, survey_id, created_by)
            VALUES (%s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), %s, FALSE, %s, 1, %s, %s)
            RETURNING {id_col}, version, verified, source
            """,
            (new_id, property_id, gj, road_side or "NORTH", source, survey_id, updated_by),
        )
    return execute(
        f"""
        INSERT INTO {table} ({id_col}, property_id, geometry, verified, source, version,
            survey_id, created_by)
        VALUES (%s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), FALSE, %s, 1, %s, %s)
        RETURNING {id_col}, version, verified, source
        """,
        (new_id, property_id, gj, source, survey_id, updated_by),
    )


def _next_geo_id(table: str, id_col: str, prefix: str) -> str:
    """Survey-created geometry uses an -S suffixed range so it never collides
    with the generated demo-lane ids (ENT-001) or the seed (ENT-C00001)."""
    row = fetch_one(
        f"""
        SELECT COALESCE(MAX(NULLIF(regexp_replace({id_col}, '^{prefix}-S', ''), '')::int), 0) AS n
        FROM {table} WHERE {id_col} ~ '^{prefix}-S[0-9]+$'
        """
    )
    return f"{prefix}-S{int((row or {}).get('n') or 0) + 1:05d}"


@router.put("/surveys/{survey_id}/geometry")
def update_geometry(survey_id: str, req: GeometryUpdate):
    s = _survey_or_404(survey_id)
    if s["survey_status"] == "APPROVED":
        raise HTTPException(409, "Approved geometry is locked; start a re-survey to change it.")
    result = _upsert_geometry(s["property_id"], req.kind, req.geojson, survey_id=survey_id,
                              updated_by=(req.updated_by or s.get("surveyor_id")),
                              source="FIELD_SURVEY_DRAWN", road_side=req.road_side)
    _touch(survey_id)
    return {"kind": req.kind, "saved": result}


@router.delete("/surveys/{survey_id}/geometry/{kind}")
def clear_geometry(survey_id: str, kind: str):
    if kind not in GEOM_KINDS:
        raise HTTPException(422, f"Unknown geometry kind {kind}")
    s = _survey_or_404(survey_id)
    if s["survey_status"] == "APPROVED":
        raise HTTPException(409, "Approved geometry is locked.")
    table, id_col, _, _ = GEOM_KINDS[kind]
    # the BEFORE DELETE trigger writes the old row to property_geometry_history
    execute(f"DELETE FROM {table} WHERE property_id = %s", (s["property_id"],))
    _touch(survey_id)
    return {"cleared": kind, "property_id": s["property_id"]}


# ---------------------------------------------------------------------------
# photos
# ---------------------------------------------------------------------------
@router.post("/surveys/{survey_id}/photos", status_code=201)
async def upload_photo(survey_id: str, photo_type: str = "FRONTAGE",
                       captured_by: str | None = None,
                       capture_method: str = "UPLOADED_FILE",
                       latitude: float | None = None, longitude: float | None = None,
                       file: UploadFile = File(...)):
    """Store one evidence photo.

    capture_method separates a picture taken on the device camera at the gate
    from a file dragged in on a laptop. They are not equivalent evidence, so
    the distinction is a stored column rather than a label in the UI that
    nobody could audit afterwards.
    """
    if photo_type not in ("FRONTAGE", "HOUSE_NUMBER", "GATE", "ENTRANCE", "CONTEXT",
                          "DISPUTE", "OTHER"):
        raise HTTPException(422, f"Unsupported photo_type {photo_type}")
    if capture_method not in ("DEVICE_CAMERA", "UPLOADED_FILE"):
        raise HTTPException(422, f"Unsupported capture_method {capture_method}")
    s = _survey_or_404(survey_id)

    data = await file.read()
    if not data:
        raise HTTPException(422, "Empty upload")
    if len(data) > 12 * 1024 * 1024:
        raise HTTPException(413, "Photo larger than 12 MB")

    digest = hashlib.sha256(data).hexdigest()
    ext = os.path.splitext(file.filename or "")[1].lower() or ".jpg"
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".heic"):
        ext = ".jpg"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{s['property_id']}_{photo_type}_{digest[:12]}{ext}")

    os.makedirs(settings.SURVEY_UPLOAD_DIR, exist_ok=True)
    path = os.path.join(settings.SURVEY_UPLOAD_DIR, safe)
    with open(path, "wb") as fh:
        fh.write(data)

    pid = _next_id("property_photos", "photo_id", "PHOTO-S", 5)
    row = execute(
        """
        INSERT INTO property_photos (photo_id, property_id, survey_id, photo_type, file_path,
            captured_at, captured_by, verified, sha256, bytes,
            capture_method, capture_latitude, capture_longitude)
        VALUES (%s, %s, %s, %s, %s, now(), %s, FALSE, %s, %s, %s, %s, %s)
        ON CONFLICT (property_id, COALESCE(survey_id, '')) WHERE photo_type = 'FRONTAGE'
        DO UPDATE SET file_path = EXCLUDED.file_path, sha256 = EXCLUDED.sha256,
                      bytes = EXCLUDED.bytes, captured_at = now(),
                      capture_method = EXCLUDED.capture_method,
                      capture_latitude = EXCLUDED.capture_latitude,
                      capture_longitude = EXCLUDED.capture_longitude
        RETURNING *
        """,
        (pid, s["property_id"], survey_id, photo_type, path,
         captured_by or s.get("surveyor_id"), digest, len(data),
         capture_method, latitude, longitude),
    )
    _touch(survey_id)
    return row


@router.get("/photos/{photo_id}/file")
def photo_file(photo_id: str):
    row = fetch_one("SELECT file_path FROM property_photos WHERE photo_id = %s", (photo_id,))
    if not row:
        raise HTTPException(404, f"Unknown photo {photo_id}")
    path = os.path.realpath(os.path.expanduser(row["file_path"]))
    roots = [os.path.realpath(settings.PHOTO_DIR), os.path.realpath(settings.SURVEY_UPLOAD_DIR)]
    if not any(path == r or path.startswith(r + os.sep) for r in roots):
        raise HTTPException(403, "Photo path is outside the configured photo directories")
    if not os.path.isfile(path):
        raise HTTPException(404, f"Photo file not on disk: {row['file_path']}")
    return FileResponse(path)


# ---------------------------------------------------------------------------
# submit / review
# ---------------------------------------------------------------------------
def _geometry_report(property_id: str, survey_id: str | None = None) -> dict:
    """Everything the submission gate needs, measured by PostGIS in one pass.

    Every number here comes from the database, not from the browser: the
    front end can be wrong, out of date, or simply not the thing that was
    saved. ST_NPoints / ST_Area / ST_IsValid / ST_Distance are asked about
    the rows that are actually stored.

    Distances and areas are metre-based via ::geography - degrees are not
    metres, and a 5 m proximity rule written in degrees would be meaningless
    at this latitude.
    """
    return fetch_one(
        """
        WITH e AS (SELECT geometry FROM property_entrances     WHERE property_id = %(p)s LIMIT 1),
             f AS (SELECT geometry FROM property_frontages     WHERE property_id = %(p)s LIMIT 1),
             z AS (SELECT geometry FROM property_service_zones WHERE property_id = %(p)s LIMIT 1)
        SELECT
          (SELECT count(*) FROM e)                                   AS has_entrance,
          (SELECT count(*) FROM f)                                   AS has_frontage,
          (SELECT count(*) FROM z)                                   AS has_zone,
          (SELECT ST_IsValid(geometry)      FROM e)                  AS entrance_valid,
          (SELECT ST_IsValid(geometry)      FROM f)                  AS frontage_valid,
          (SELECT ST_IsValid(geometry)      FROM z)                  AS zone_valid,
          (SELECT ST_IsValidReason(geometry) FROM z)                 AS zone_reason,
          (SELECT ST_IsValidReason(geometry) FROM f)                 AS frontage_reason,
          (SELECT ST_NPoints(geometry)      FROM f)                  AS frontage_points,
          (SELECT ST_NPoints(ST_RemoveRepeatedPoints(geometry)) FROM f)
                                                                     AS frontage_unique_points,
          (SELECT ST_NPoints(geometry)      FROM z)                  AS zone_points,
          -- a closed ring repeats its first vertex, so unique vertices = n - 1
          (SELECT GREATEST(ST_NPoints(ST_RemoveRepeatedPoints(geometry)) - 1, 0) FROM z)
                                                                     AS zone_unique_points,
          (SELECT ROUND(ST_Area(geometry::geography)::numeric, 2)  FROM z) AS zone_area_m2,
          (SELECT ROUND(ST_Length(geometry::geography)::numeric, 2) FROM f) AS frontage_length_m,
          (SELECT ROUND(ST_Distance(e.geometry::geography, f.geometry::geography)::numeric, 2)
             FROM e, f)                                              AS entrance_to_frontage_m,
          (SELECT ROUND(ST_Distance(e.geometry::geography, z.geometry::geography)::numeric, 2)
             FROM e, z)                                              AS entrance_to_zone_m,
          (SELECT ST_Within(e.geometry, z.geometry) FROM e, z)       AS entrance_in_zone,
          (SELECT count(*) FROM property_photos
            WHERE property_id = %(p)s AND photo_type = 'FRONTAGE'
              AND (%(s)s::text IS NULL OR survey_id = %(s)s OR survey_id IS NULL)) AS frontage_photos
        """,
        {"p": property_id, "s": survey_id},
    ) or {}


def _submission_blockers(property_id: str, survey_id: str | None = None) -> list[str]:
    """Hard stops. Anything in this list means the survey cannot be submitted."""
    r = _geometry_report(property_id, survey_id)
    b: list[str] = []

    if not r.get("has_entrance"):
        b.append("entrance point is missing - use Mark Entrance")
    if not r.get("has_frontage"):
        b.append("frontage line is missing - use Draw Frontage")
    if not r.get("has_zone"):
        b.append("service zone is missing - use Draw Service Zone")
    if not r.get("frontage_photos"):
        b.append("frontage photo is missing - use Capture Frontage Photo")

    for label, key in (("entrance", "entrance_valid"),
                       ("frontage line", "frontage_valid"),
                       ("service-zone polygon", "zone_valid")):
        if r.get(key) is False:
            reason = r.get("zone_reason" if key == "zone_valid" else "frontage_reason") or ""
            b.append(f"{label} is not valid geometry" + (f" ({reason})" if reason else ""))

    fp = r.get("frontage_unique_points")
    if r.get("has_frontage") and fp is not None and fp < 2:
        b.append(f"frontage needs at least 2 distinct points (has {fp})")

    zp = r.get("zone_unique_points")
    if r.get("has_zone") and zp is not None and zp < 3:
        b.append(f"service zone needs at least 3 distinct vertices (has {zp})")

    area = r.get("zone_area_m2")
    if r.get("has_zone") and area is not None and float(area) <= settings.MIN_SERVICE_ZONE_AREA_M2:
        b.append(f"service-zone area is {area} m2 - it must enclose real ground")

    # Proximity: a hard stop only when it is implausible, never for a merely
    # generous plot. The soft band is a warning (see _submission_warnings).
    dists = [d for d in (r.get("entrance_to_frontage_m"), r.get("entrance_to_zone_m"))
             if d is not None]
    if dists and min(float(d) for d in dists) > settings.ENTRANCE_PROXIMITY_MAX_M:
        b.append(
            f"entrance is {min(float(d) for d in dists):g} m from its own frontage/service zone "
            f"(limit {settings.ENTRANCE_PROXIMITY_MAX_M:g} m) - is it on the right property?")
    return b


def _submission_warnings(property_id: str, survey_id: str | None = None) -> list[str]:
    """Soft flags. These are surfaced and recorded, but never block a submit -
    the reviewer decides. Silently accepting them is what we are avoiding;
    silently refusing them would be just as wrong."""
    r = _geometry_report(property_id, survey_id)
    w: list[str] = []

    dists = [float(d) for d in (r.get("entrance_to_frontage_m"), r.get("entrance_to_zone_m"))
             if d is not None]
    if dists:
        near = min(dists)
        if settings.ENTRANCE_PROXIMITY_OK_M < near <= settings.ENTRANCE_PROXIMITY_MAX_M:
            w.append(f"entrance sits {near:g} m from its frontage/service zone "
                     f"(usually under {settings.ENTRANCE_PROXIMITY_OK_M:g} m)")
    if r.get("has_entrance") and r.get("has_zone") and r.get("entrance_in_zone") is False:
        w.append("entrance is outside its own service zone")

    area = r.get("zone_area_m2")
    if area is not None and float(area) > settings.MAX_SERVICE_ZONE_AREA_M2:
        w.append(f"service zone is {area} m2 - that looks like the whole plot, "
                 f"not the collection area")
    return w


@router.get("/surveys/{survey_id}/readiness")
def submission_readiness(survey_id: str):
    s = _survey_or_404(survey_id)
    blockers = list(_submission_blockers(s["property_id"], survey_id))
    if not s.get("mapping_confidence"):
        blockers.append("mapping confidence is not set")
    if not s.get("source_class"):
        blockers.append("source class is not set")
    return {
        "survey_id": survey_id,
        "ready": not blockers,
        "blockers": blockers,
        "warnings": _submission_warnings(s["property_id"], survey_id),
        "geometry": _geometry_report(s["property_id"], survey_id),
        "thresholds": {
            "entrance_proximity_ok_m": settings.ENTRANCE_PROXIMITY_OK_M,
            "entrance_proximity_max_m": settings.ENTRANCE_PROXIMITY_MAX_M,
            "min_service_zone_area_m2": settings.MIN_SERVICE_ZONE_AREA_M2,
            "max_service_zone_area_m2": settings.MAX_SERVICE_ZONE_AREA_M2,
            "gnss_accuracy_warn_m": settings.GNSS_ACCURACY_WARN_M,
        },
    }


@router.post("/surveys/{survey_id}/submit")
def submit_survey(survey_id: str, req: SubmitRequest):
    s = _survey_or_404(survey_id)
    if s["survey_status"] == "SUBMITTED":
        raise HTTPException(409, "Already submitted and awaiting review.")
    if s["survey_status"] == "APPROVED":
        raise HTTPException(409, "Already approved.")

    blockers = list(_submission_blockers(s["property_id"], survey_id))
    if not s.get("mapping_confidence"):
        blockers.append("mapping confidence is not set")
    if not s.get("source_class"):
        blockers.append("source class is not set")
    if blockers:
        raise HTTPException(422, {"error": "Survey is not ready to submit", "blockers": blockers})

    # Soft flags do not stop the submit, but they must reach the reviewer as
    # QA issues rather than dying in a toast the surveyor already dismissed.
    warnings = _submission_warnings(s["property_id"], survey_id)
    for text in warnings:
        _raise_qa(s["property_id"], survey_id, "GEOMETRY_NEEDS_REVIEW", "MEDIUM", text)

    row = execute(
        """
        UPDATE property_surveys
           SET survey_status = 'SUBMITTED',
               review_status = 'PENDING',
               surveyor_id   = COALESCE(%s, surveyor_id),
               notes         = COALESCE(%s, notes),
               survey_completed_at = COALESCE(survey_completed_at, now()),
               submitted_at  = now(),
               updated_at    = now()
         WHERE survey_id = %s RETURNING *
        """,
        (req.surveyor_id, req.notes, survey_id),
    )
    execute(
        """
        UPDATE properties SET verification_status = 'FIELD_SURVEYED'
         WHERE property_id = %s AND verification_status <> 'VERIFIED_FOR_OPERATION'
        """,
        (s["property_id"],),
    )
    _sync_assignment(s.get("assignment_id"))
    return {"survey": row, "review_queue": "/survey/review", "warnings": warnings}


@router.post("/surveys/{survey_id}/review")
def review_survey(survey_id: str, req: ReviewRequest):
    s = _survey_or_404(survey_id)
    if s["survey_status"] not in ("SUBMITTED", "APPROVED", "CORRECTION_REQUIRED", "REJECTED"):
        raise HTTPException(409, f"Survey is {s['survey_status']}, nothing to review yet.")
    if not fetch_one("SELECT 1 AS ok FROM survey_users WHERE user_id = %s AND role IN "
                     "('REVIEWER','GIS_ADMIN','SUPERVISOR','ADMIN')", (req.reviewer_id,)):
        raise HTTPException(403, f"{req.reviewer_id} is not allowed to review surveys")

    new_status = {"APPROVE": "APPROVED",
                  "CORRECTION_REQUIRED": "CORRECTION_REQUIRED",
                  "REJECT": "REJECTED"}[req.action]
    review_status = {"APPROVE": "APPROVED",
                     "CORRECTION_REQUIRED": "CORRECTION_REQUIRED",
                     "REJECT": "REJECTED"}[req.action]

    if req.action == "APPROVE":
        blockers = _submission_blockers(s["property_id"], survey_id)
        if blockers:
            raise HTTPException(422, {"error": "Cannot approve", "blockers": blockers})

    row = execute(
        """
        UPDATE property_surveys
           SET survey_status = %s, review_status = %s, reviewer_id = %s,
               reviewed_at = now(), review_notes = %s, updated_at = now()
         WHERE survey_id = %s RETURNING *
        """,
        (new_status, review_status, req.reviewer_id, req.review_notes, survey_id),
    )

    if req.action == "APPROVE":
        # Approval is the only path to operational clearance.
        execute("UPDATE properties SET verification_status = 'VERIFIED_FOR_OPERATION' "
                "WHERE property_id = %s", (s["property_id"],))
        for table in ("property_entrances", "property_frontages", "property_service_zones"):
            execute(
                f"UPDATE {table} SET verified = TRUE, verified_by = %s, verified_at = now() "
                f"WHERE property_id = %s", (req.reviewer_id, s["property_id"]))
        execute(
            "UPDATE property_qa_issues SET status='RESOLVED', resolved_at=now(), resolved_by=%s "
            "WHERE property_id = %s AND status='OPEN' AND issue_type = 'MANUAL_REVIEW_REQUIRED'",
            (req.reviewer_id, s["property_id"]))
    else:
        execute("UPDATE properties SET verification_status = 'FIELD_SURVEYED' "
                "WHERE property_id = %s AND verification_status = 'VERIFIED_FOR_OPERATION'",
                (s["property_id"],))
        if req.action == "CORRECTION_REQUIRED":
            _raise_qa(s["property_id"], survey_id, "MANUAL_REVIEW_REQUIRED", "MEDIUM",
                      req.review_notes or "Reviewer returned the survey for correction.")

    _sync_assignment(s.get("assignment_id"))
    return {"survey": row,
            "property": fetch_one("SELECT property_id, verification_status FROM properties "
                                  "WHERE property_id = %s", (s["property_id"],))}


def _sync_assignment(assignment_id: str | None) -> None:
    if not assignment_id:
        return
    execute(
        """
        UPDATE survey_assignments a
           SET total_properties = v.total_properties,
               surveyed_count   = v.surveyed_count,
               verified_count   = v.verified_count,
               status = CASE
                   WHEN v.total_properties > 0 AND v.verified_count = v.total_properties THEN 'COMPLETED'
                   WHEN v.surveyed_count = 0 THEN 'NOT_STARTED'
                   WHEN v.outstanding_count = 0 THEN 'SUBMITTED'
                   ELSE 'IN_PROGRESS' END,
               completed_at = CASE
                   WHEN v.total_properties > 0 AND v.verified_count = v.total_properties
                   THEN COALESCE(a.completed_at, now()) ELSE NULL END
          FROM v_assignment_progress v
         WHERE v.assignment_id = a.assignment_id AND a.assignment_id = %s
        """,
        (assignment_id,),
    )


# ---------------------------------------------------------------------------
# QA
# ---------------------------------------------------------------------------
def _raise_qa(property_id: str, survey_id: str | None, issue_type: str,
              severity: str, description: str) -> None:
    seq = fetch_one(
        "SELECT COALESCE(MAX(NULLIF(regexp_replace(issue_id,'^QA-A?','' ),'')::int),0) AS n "
        "FROM property_qa_issues WHERE issue_id ~ '^QA-A?[0-9]+$'")
    execute(
        """
        INSERT INTO property_qa_issues (issue_id, property_id, survey_id, issue_type,
            severity, status, description)
        VALUES (%s, %s, %s, %s, %s, 'OPEN', %s)
        ON CONFLICT (property_id, issue_type) WHERE status = 'OPEN'
        DO UPDATE SET description = EXCLUDED.description, severity = EXCLUDED.severity
        """,
        (f"QA-A{int((seq or {}).get('n') or 0) + 1:06d}", property_id, survey_id,
         issue_type, severity, description),
    )


@router.post("/qa/run")
def run_qa_checks():
    """Re-run every automated GIS check across the whole authority."""
    return qa_checks.run_all()


@router.patch("/qa-issues/{issue_id}")
def update_qa_issue(issue_id: str, req: QAIssueUpdate):
    row = execute(
        """
        UPDATE property_qa_issues
           SET status = %s,
               resolved_by = CASE WHEN %s IN ('RESOLVED','WONT_FIX') THEN %s ELSE resolved_by END,
               resolved_at = CASE WHEN %s IN ('RESOLVED','WONT_FIX') THEN now() ELSE NULL END
         WHERE issue_id = %s RETURNING *
        """,
        (req.status, req.status, req.resolved_by, req.status, issue_id),
    )
    if not row:
        raise HTTPException(404, f"Unknown QA issue {issue_id}")
    return row

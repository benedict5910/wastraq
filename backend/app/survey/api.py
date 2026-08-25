"""Survey module - read endpoints.

Everything here is answered from PostgreSQL/PostGIS. Nothing is invented in
the API layer: if a number appears on a survey dashboard it came out of a
query, and if it cannot be computed the field is null rather than guessed.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from ..config import settings
from ..database import fetch_all, fetch_one

router = APIRouter(prefix="/survey/api", tags=["survey"])


# ===========================================================================
# Administrative hierarchy
# ===========================================================================
@router.get("/admin-units")
def list_admin_units(
    unit_type: str | None = Query(None, description="CITY | ZONE | WARD | DISTRICT | ROUTE_AREA"),
    parent_id: str | None = None,
    active: bool | None = None,
):
    clauses, params = [], []
    if unit_type:
        clauses.append("unit_type = %s")
        params.append(unit_type)
    if parent_id:
        clauses.append("parent_id = %s")
        params.append(parent_id)
    if active is not None:
        clauses.append("active = %s")
        params.append(active)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return fetch_all(
        f"""
        SELECT admin_unit_id, name, unit_type, parent_id, authority_code, active,
               (SELECT count(*) FROM properties p WHERE p.admin_unit_id = au.admin_unit_id)
                   AS direct_properties
        FROM administrative_units au
        {where}
        ORDER BY unit_type, name
        """,
        tuple(params) or None,
    )


@router.get("/admin-units/tree")
def admin_unit_tree():
    """The whole hierarchy with property/survey rollups at every level."""
    return fetch_all(
        """
        WITH RECURSIVE descend AS (
            SELECT admin_unit_id AS root, admin_unit_id AS node FROM administrative_units
            UNION ALL
            SELECT d.root, c.admin_unit_id
            FROM descend d JOIN administrative_units c ON c.parent_id = d.node
        ),
        roll AS (
            SELECT d.root,
                   count(v.property_id)                                              AS properties,
                   count(*) FILTER (WHERE v.survey_status = 'APPROVED')              AS verified,
                   count(*) FILTER (WHERE v.survey_status = 'SUBMITTED')             AS pending_review,
                   count(*) FILTER (WHERE v.survey_status = 'CORRECTION_REQUIRED')   AS correction_required,
                   count(*) FILTER (WHERE v.survey_status = 'IN_PROGRESS')           AS in_progress,
                   count(*) FILTER (WHERE v.survey_status = 'NOT_SURVEYED')          AS not_surveyed
            FROM descend d
            LEFT JOIN v_survey_property_status v ON v.admin_unit_id = d.node
            GROUP BY d.root
        )
        SELECT t.admin_unit_id, t.name, t.unit_type, t.parent_id, t.depth, t.full_path,
               t.authority_code, t.active,
               COALESCE(r.properties, 0)           AS properties,
               COALESCE(r.verified, 0)             AS verified,
               COALESCE(r.pending_review, 0)       AS pending_review,
               COALESCE(r.correction_required, 0)  AS correction_required,
               COALESCE(r.in_progress, 0)          AS in_progress,
               COALESCE(r.not_surveyed, 0)         AS not_surveyed
        FROM v_admin_unit_tree t
        LEFT JOIN roll r ON r.root = t.admin_unit_id
        ORDER BY t.depth, t.name
        """
    )


@router.get("/admin-units/geojson")
def admin_units_geojson(unit_type: str = Query("WARD")):
    rows = fetch_all(
        """
        SELECT au.admin_unit_id, au.name, au.unit_type, au.parent_id, au.authority_code,
               ST_AsGeoJSON(au.geometry)::json AS geom,
               (SELECT count(*) FROM v_survey_property_status v
                 WHERE v.ward_id = au.admin_unit_id OR v.admin_unit_id = au.admin_unit_id) AS properties
        FROM administrative_units au
        WHERE au.unit_type = %s AND au.geometry IS NOT NULL
        ORDER BY au.name
        """,
        (unit_type,),
    )
    return _fc(rows, "admin_unit_id")


# ===========================================================================
# Users
# ===========================================================================
@router.get("/users")
def list_users(role: str | None = None, active: bool | None = True):
    clauses, params = [], []
    if role:
        clauses.append("role = %s")
        params.append(role)
    if active is not None:
        clauses.append("active = %s")
        params.append(active)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return fetch_all(
        f"SELECT user_id, name, employee_id, role, email, phone, active, assigned_authority "
        f"FROM survey_users {where} ORDER BY role, name",
        tuple(params) or None,
    )


# ===========================================================================
# Assignments
# ===========================================================================
@router.get("/assignments")
def list_assignments(
    status: str | None = None,
    assigned_to: str | None = None,
    admin_unit_id: str | None = None,
    limit: int = Query(200, ge=1, le=2000),
):
    clauses, params = [], []
    if status:
        clauses.append("status = %s")
        params.append(status)
    if assigned_to:
        clauses.append("assigned_to = %s")
        params.append(assigned_to)
    if admin_unit_id:
        clauses.append("admin_unit_id = %s")
        params.append(admin_unit_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    return fetch_all(
        f"SELECT * FROM v_assignment_progress {where} ORDER BY assignment_id LIMIT %s",
        tuple(params),
    )


@router.get("/assignments/{assignment_id}")
def get_assignment(assignment_id: str):
    row = fetch_one("SELECT * FROM v_assignment_progress WHERE assignment_id = %s",
                    (assignment_id,))
    if not row:
        raise HTTPException(404, f"Unknown assignment {assignment_id}")
    row["properties"] = fetch_all(
        """
        SELECT v.property_id, v.house_number, v.owner_name, v.formatted_address,
               v.survey_status, v.survey_mapping_confidence, v.latitude, v.longitude,
               v.open_qa_issues, v.survey_id
        FROM v_survey_property_status v
        WHERE v.assignment_id = %s OR v.admin_unit_id = %s
        ORDER BY v.property_id
        """,
        (assignment_id, row["admin_unit_id"]),
    )
    return row


# ===========================================================================
# Survey property list / map layer
# ===========================================================================
_PROP_FILTERS = """
    (%(admin_unit_id)s::text IS NULL OR v.admin_unit_id = %(admin_unit_id)s
        OR v.ward_id = %(admin_unit_id)s OR v.zone_id = %(admin_unit_id)s)
AND (%(ward_id)s::text     IS NULL OR v.ward_id = %(ward_id)s)
AND (%(zone_id)s::text     IS NULL OR v.zone_id = %(zone_id)s)
AND (%(route_id)s::text    IS NULL OR v.route_id = %(route_id)s)
AND (%(surveyor_id)s::text IS NULL OR v.surveyor_id = %(surveyor_id)s)
AND (%(status)s::text      IS NULL OR v.survey_status = %(status)s)
AND (%(confidence)s::text  IS NULL OR v.survey_mapping_confidence = %(confidence)s)
AND (%(has_qa)s::bool      IS NULL OR (v.open_qa_issues > 0) = %(has_qa)s)
AND (%(q)s::text           IS NULL OR v.property_id ILIKE %(q)s OR v.owner_name ILIKE %(q)s
        OR v.house_number ILIKE %(q)s OR v.formatted_address ILIKE %(q)s)
"""


def _prop_params(**kw) -> dict[str, Any]:
    p = {k: kw.get(k) for k in
         ("admin_unit_id", "ward_id", "zone_id", "route_id", "surveyor_id",
          "status", "confidence", "has_qa")}
    q = kw.get("q")
    p["q"] = f"%{q}%" if q else None
    return p


@router.get("/properties")
def survey_properties(
    admin_unit_id: str | None = None, ward_id: str | None = None,
    zone_id: str | None = None, route_id: str | None = None,
    surveyor_id: str | None = None, status: str | None = None,
    confidence: str | None = None, has_qa: bool | None = None,
    q: str | None = None,
    limit: int = Query(200, ge=1, le=5000), offset: int = Query(0, ge=0),
):
    params = _prop_params(admin_unit_id=admin_unit_id, ward_id=ward_id, zone_id=zone_id,
                          route_id=route_id, surveyor_id=surveyor_id, status=status,
                          confidence=confidence, has_qa=has_qa, q=q)
    total = fetch_one(
        f"SELECT count(*) AS n FROM v_survey_property_status v WHERE {_PROP_FILTERS}", params)
    params2 = dict(params, limit=limit, offset=offset)
    rows = fetch_all(
        f"""
        SELECT v.* FROM v_survey_property_status v
        WHERE {_PROP_FILTERS}
        ORDER BY v.property_id
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        params2,
    )
    return {"total": (total or {}).get("n", 0), "limit": limit, "offset": offset, "items": rows}


@router.get("/properties/geojson")
def survey_properties_geojson(
    admin_unit_id: str | None = None, ward_id: str | None = None,
    zone_id: str | None = None, route_id: str | None = None,
    surveyor_id: str | None = None, status: str | None = None,
    confidence: str | None = None, has_qa: bool | None = None,
    q: str | None = None, limit: int = Query(3000, ge=1, le=8000),
):
    """Point layer for the city map. Points, not polygons: at city zoom the
    service-zone polygons are sub-pixel and would just cost bandwidth."""
    params = dict(_prop_params(admin_unit_id=admin_unit_id, ward_id=ward_id, zone_id=zone_id,
                               route_id=route_id, surveyor_id=surveyor_id, status=status,
                               confidence=confidence, has_qa=has_qa, q=q), limit=limit)
    rows = fetch_all(
        f"""
        SELECT v.property_id, v.house_number, v.owner_name, v.route_id, v.survey_status,
               v.survey_mapping_confidence, v.ward_name, v.zone_name, v.surveyor_name,
               v.open_qa_issues, v.has_entrance, v.has_frontage, v.has_service_zone,
               v.latitude, v.longitude
        FROM v_survey_property_status v
        WHERE {_PROP_FILTERS} AND v.latitude IS NOT NULL AND v.longitude IS NOT NULL
        ORDER BY v.property_id
        LIMIT %(limit)s
        """,
        params,
    )
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "id": r["property_id"],
             "geometry": {"type": "Point",
                          "coordinates": [float(r.pop("longitude")), float(r.pop("latitude"))]},
             "properties": r}
            for r in rows
        ],
    }


@router.get("/properties/{property_id}/survey")
def property_survey_detail(property_id: str):
    """Everything the field/review screens need for one property."""
    prop = fetch_one("SELECT * FROM v_survey_property_status WHERE property_id = %s",
                     (property_id,))
    if not prop:
        raise HTTPException(404, f"Unknown property {property_id}")

    survey = None
    if prop.get("survey_id"):
        survey = fetch_one(
            """
            SELECT s.*,
                   ST_AsGeoJSON(s.captured_point)::json AS captured_point_geojson,
                   su.name AS surveyor_name, rv.name AS reviewer_name
            FROM property_surveys s
            LEFT JOIN survey_users su ON su.user_id = s.surveyor_id
            LEFT JOIN survey_users rv ON rv.user_id = s.reviewer_id
            WHERE s.survey_id = %s
            """,
            (prop["survey_id"],),
        )

    return {
        "property": prop,
        "survey": survey,
        "geometry": _property_geometry(property_id),
        "photos": fetch_all(
            """
            SELECT photo_id, property_id, survey_id, photo_type, file_path,
                   captured_at, captured_by, verified, notes, sha256, bytes,
                   capture_method, capture_latitude, capture_longitude
            FROM property_photos WHERE property_id = %s
            ORDER BY captured_at DESC NULLS LAST, photo_id
            """,
            (property_id,),
        ),
        "qa_issues": fetch_all(
            """
            SELECT q.*, u.name AS resolved_by_name
            FROM property_qa_issues q
            LEFT JOIN survey_users u ON u.user_id = q.resolved_by
            WHERE q.property_id = %s ORDER BY q.detected_at DESC
            """,
            (property_id,),
        ),
        "history": fetch_all(
            """
            SELECT history_id, geometry_kind, feature_id, version, source, verified,
                   operation, changed_by, changed_at
            FROM property_geometry_history WHERE property_id = %s
            ORDER BY changed_at DESC LIMIT 25
            """,
            (property_id,),
        ),
        "thresholds": {
            "gnss_accuracy_warn_m": settings.GNSS_ACCURACY_WARN_M,
            "entrance_proximity_ok_m": settings.ENTRANCE_PROXIMITY_OK_M,
            "entrance_proximity_max_m": settings.ENTRANCE_PROXIMITY_MAX_M,
            "min_service_zone_area_m2": settings.MIN_SERVICE_ZONE_AREA_M2,
            "max_service_zone_area_m2": settings.MAX_SERVICE_ZONE_AREA_M2,
        },
        "vocabulary": {
            "property_type": ["RESIDENTIAL", "COMMERCIAL", "MIXED", "INSTITUTIONAL",
                              "INDUSTRIAL", "VACANT"],
            "service_entity_type": ["SINGLE_HOUSEHOLD", "MULTI_HOUSEHOLD", "APARTMENT_BLOCK",
                                    "SHOP", "RESTAURANT", "OFFICE", "INSTITUTION",
                                    "BULK_GENERATOR", "COMMON_COLLECTION_POINT"],
            "mapping_confidence": ["HIGH", "MEDIUM", "LOW"],
            "source_class": ["VERIFIED_FIELD_SURVEY", "AUTHORITY_GIS", "THIRD_PARTY_MAP",
                             "APPROXIMATE_GEOCODE", "UNVERIFIED"],
        },
    }


def _property_geometry(property_id: str) -> dict:
    ent = fetch_one(
        "SELECT entrance_id, verified, source, version, ST_AsGeoJSON(geometry)::json AS geom "
        "FROM property_entrances WHERE property_id = %s LIMIT 1", (property_id,))
    fro = fetch_one(
        "SELECT frontage_id, road_side, verified, source, version, "
        "ST_AsGeoJSON(geometry)::json AS geom, "
        "ROUND(ST_Length(geometry::geography)::numeric, 1) AS length_m, "
        "ST_NPoints(geometry) AS n_points "
        "FROM property_frontages WHERE property_id = %s LIMIT 1", (property_id,))
    zon = fetch_one(
        "SELECT zone_id, verified, source, version, ST_AsGeoJSON(geometry)::json AS geom, "
        "ROUND(ST_Area(geometry::geography)::numeric, 1) AS area_m2, "
        "GREATEST(ST_NPoints(geometry) - 1, 0) AS n_points "
        "FROM property_service_zones WHERE property_id = %s LIMIT 1", (property_id,))
    return {"entrance": ent, "frontage": fro, "service_zone": zon}


@router.get("/surveys")
def list_surveys(
    survey_status: str | None = None,
    review_status: str | None = None,
    surveyor_id: str | None = None,
    assignment_id: str | None = None,
    limit: int = Query(200, ge=1, le=2000),
):
    clauses, params = [], []
    for col, val in (("s.survey_status", survey_status), ("s.review_status", review_status),
                     ("s.surveyor_id", surveyor_id), ("s.assignment_id", assignment_id)):
        if val:
            clauses.append(f"{col} = %s")
            params.append(val)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    return fetch_all(
        f"""
        SELECT s.survey_id, s.property_id, s.assignment_id, s.survey_status, s.review_status,
               s.mapping_confidence, s.source_class, s.location_accuracy_m,
               s.manually_adjusted, s.submitted_at, s.reviewed_at, s.anomaly_type,
               p.house_number, p.owner_name, p.formatted_address, p.route_id,
               su.name AS surveyor_name, rv.name AS reviewer_name,
               (SELECT count(*) FROM property_qa_issues q
                 WHERE q.property_id = s.property_id AND q.status='OPEN') AS open_qa_issues,
               (SELECT count(*) FROM property_photos ph WHERE ph.survey_id = s.survey_id) AS photo_count
        FROM property_surveys s
        JOIN properties p ON p.property_id = s.property_id
        LEFT JOIN survey_users su ON su.user_id = s.surveyor_id
        LEFT JOIN survey_users rv ON rv.user_id = s.reviewer_id
        {where}
        ORDER BY s.submitted_at DESC NULLS LAST, s.survey_id
        LIMIT %s
        """,
        tuple(params),
    )


# ===========================================================================
# QA
# ===========================================================================
@router.get("/qa-issues")
def list_qa_issues(
    status: str | None = "OPEN", severity: str | None = None,
    issue_type: str | None = None, ward_id: str | None = None,
    property_id: str | None = None,
    limit: int = Query(300, ge=1, le=3000),
):
    clauses, params = [], []
    for col, val in (("q.status", status), ("q.severity", severity),
                     ("q.issue_type", issue_type), ("q.property_id", property_id)):
        if val:
            clauses.append(f"{col} = %s")
            params.append(val)
    if ward_id:
        clauses.append("v.ward_id = %s")
        params.append(ward_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    return fetch_all(
        f"""
        SELECT q.issue_id, q.property_id, q.survey_id, q.issue_type, q.severity, q.status,
               q.description, q.detected_at, q.resolved_at, q.resolved_by,
               v.ward_name, v.zone_name, v.route_id, v.house_number, v.surveyor_name,
               v.survey_status
        FROM property_qa_issues q
        LEFT JOIN v_survey_property_status v ON v.property_id = q.property_id
        {where}
        ORDER BY CASE q.severity WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1
                                 WHEN 'MEDIUM' THEN 2 ELSE 3 END,
                 q.detected_at DESC
        LIMIT %s
        """,
        tuple(params),
    )


# ===========================================================================
# Analytics
# ===========================================================================
@router.get("/analytics/scale")
def analytics_scale():
    """The pilot-to-city story, computed - not asserted.

    Every level reports what is actually configured and what actually holds
    surveyed properties. A level that exists but is empty is reported as
    "ready" rather than being padded with invented rows: the honest claim is
    that the schema and the UI already handle it, not that data is there.
    """
    levels = fetch_all(
        """
        SELECT a.unit_type,
               count(*)                                              AS units,
               count(*) FILTER (WHERE d.properties > 0)              AS units_with_properties,
               COALESCE(sum(d.properties), 0)                        AS properties,
               COALESCE(sum(d.verified), 0)                          AS verified
        FROM administrative_units a
        LEFT JOIN LATERAL (
            -- properties in this unit OR anywhere beneath it
            WITH RECURSIVE sub AS (
                SELECT a.admin_unit_id
                UNION ALL
                SELECT c.admin_unit_id FROM administrative_units c
                  JOIN sub ON c.parent_id = sub.admin_unit_id
            )
            SELECT count(v.property_id)                                       AS properties,
                   count(*) FILTER (WHERE v.survey_status = 'APPROVED')       AS verified
            FROM v_survey_property_status v
            WHERE v.admin_unit_id IN (SELECT admin_unit_id FROM sub)
        ) d ON TRUE
        GROUP BY a.unit_type
        """)
    order = {"CITY": 0, "ZONE": 1, "DISTRICT": 1, "WARD": 2, "ROUTE_AREA": 3}
    levels.sort(key=lambda r: order.get(r["unit_type"], 9))

    routes = fetch_all(
        """
        SELECT route_id,
               count(*)                                                AS properties,
               count(*) FILTER (WHERE survey_status = 'APPROVED')      AS verified
        FROM v_survey_property_status
        WHERE route_id IS NOT NULL
        GROUP BY route_id ORDER BY properties DESC, route_id
        """)

    unplaced = fetch_one(
        "SELECT count(*) AS n FROM properties WHERE admin_unit_id IS NULL") or {}

    return {
        "levels": levels,
        "routes": routes,
        "pilot_route_id": settings.DEMO_ROUTE_ID,
        "properties_without_admin_unit": int(unplaced.get("n") or 0),
        # The path shown in the UI. Present in the schema whether or not any
        # given level currently holds data.
        "path": ["Pilot Lane", "Route", "Ward", "Zone", "City"],
    }


@router.get("/analytics/overview")
def analytics_overview(admin_unit_id: str | None = None):
    params = {"unit": admin_unit_id}
    scope = ("AND (v.admin_unit_id = %(unit)s OR v.ward_id = %(unit)s OR v.zone_id = %(unit)s)"
             if admin_unit_id else "")
    totals = fetch_one(
        f"""
        SELECT
            count(*)                                                          AS total_properties,
            count(*) FILTER (WHERE v.survey_status <> 'NOT_SURVEYED')         AS surveyed,
            count(*) FILTER (WHERE v.survey_status = 'APPROVED')              AS verified,
            count(*) FILTER (WHERE v.survey_status = 'SUBMITTED')             AS pending_review,
            count(*) FILTER (WHERE v.survey_status = 'CORRECTION_REQUIRED')   AS correction_required,
            count(*) FILTER (WHERE v.survey_status = 'REJECTED')              AS rejected,
            count(*) FILTER (WHERE v.survey_status = 'IN_PROGRESS')           AS in_progress,
            count(*) FILTER (WHERE v.survey_status = 'NOT_SURVEYED')          AS not_surveyed,
            count(*) FILTER (WHERE v.has_entrance AND v.has_frontage AND v.has_service_zone)
                                                                              AS fully_mapped,
            count(*) FILTER (WHERE v.open_qa_issues > 0)                      AS with_open_qa
        FROM v_survey_property_status v
        WHERE TRUE {scope}
        """,
        params,
    ) or {}
    total = max(int(totals.get("total_properties") or 0), 1)
    totals["surveyed_pct"] = round(100.0 * int(totals.get("surveyed") or 0) / total, 1)
    totals["verified_pct"] = round(100.0 * int(totals.get("verified") or 0) / total, 1)
    totals["mapped_pct"] = round(100.0 * int(totals.get("fully_mapped") or 0) / total, 1)

    return {
        "totals": totals,
        "by_status": fetch_all(
            f"SELECT v.survey_status AS status, count(*) AS n FROM v_survey_property_status v "
            f"WHERE TRUE {scope} GROUP BY 1 ORDER BY 2 DESC", params),
        "by_confidence": fetch_all(
            f"SELECT COALESCE(v.survey_mapping_confidence,'NOT_SET') AS confidence, count(*) AS n "
            f"FROM v_survey_property_status v WHERE TRUE {scope} GROUP BY 1 ORDER BY 2 DESC",
            params),
        "by_zone": fetch_all(
            """
            SELECT COALESCE(v.zone_name,'Unassigned') AS zone_name, v.zone_id,
                   count(*) AS properties,
                   count(*) FILTER (WHERE v.survey_status = 'APPROVED') AS verified,
                   count(*) FILTER (WHERE v.survey_status = 'SUBMITTED') AS pending_review,
                   count(*) FILTER (WHERE v.survey_status = 'NOT_SURVEYED') AS not_surveyed
            FROM v_survey_property_status v GROUP BY 1, 2 ORDER BY properties DESC
            """),
        "by_ward": fetch_all(
            """
            SELECT COALESCE(v.ward_name,'Unassigned') AS ward_name, v.ward_id, v.zone_name,
                   count(*) AS properties,
                   count(*) FILTER (WHERE v.survey_status = 'APPROVED') AS verified,
                   count(*) FILTER (WHERE v.survey_status = 'SUBMITTED') AS pending_review,
                   count(*) FILTER (WHERE v.survey_status = 'CORRECTION_REQUIRED') AS correction_required,
                   count(*) FILTER (WHERE v.survey_status = 'NOT_SURVEYED') AS not_surveyed,
                   count(*) FILTER (WHERE v.open_qa_issues > 0) AS with_open_qa
            FROM v_survey_property_status v GROUP BY 1, 2, 3 ORDER BY properties DESC
            """),
        "qa_by_type": fetch_all(
            "SELECT issue_type, severity, count(*) AS n FROM property_qa_issues "
            "WHERE status = 'OPEN' GROUP BY 1, 2 ORDER BY n DESC"),
        "gnss": fetch_one(
            """
            SELECT count(*) FILTER (WHERE location_accuracy_m IS NOT NULL)            AS captures,
                   ROUND(AVG(location_accuracy_m)::numeric, 1)                        AS avg_accuracy_m,
                   count(*) FILTER (WHERE location_accuracy_m > %s)                   AS poor_fixes,
                   count(*) FILTER (WHERE manually_adjusted)                          AS manually_adjusted
            FROM property_surveys
            """,
            (settings.GNSS_ACCURACY_WARN_M,)),
    }


@router.get("/analytics/surveyors")
def analytics_surveyors():
    return fetch_all("SELECT * FROM v_surveyor_performance ORDER BY approved DESC, name")


# ---------------------------------------------------------------------------
def _fc(rows: list[dict], id_key: str) -> dict:
    return {"type": "FeatureCollection",
            "features": [{"type": "Feature", "id": r[id_key],
                          "geometry": r.pop("geom"), "properties": r} for r in rows]}

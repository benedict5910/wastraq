"""Automated GIS quality checks.

Every check is a real query against PostGIS. Nothing here reports a problem
it has not actually measured, and each check writes at most one OPEN issue
per (property, issue_type) - the partial unique index enforces that, so
re-running is safe and does not pile up duplicates.
"""

from __future__ import annotations

from ..config import settings
from ..database import execute, fetch_all, fetch_one

# Each entry: (issue_type, severity, description template, SQL returning
# property_id [, survey_id] [, detail]).
CHECKS: list[tuple[str, str, str, str]] = [
    (
        "MISSING_ENTRANCE", "HIGH",
        "Survey reached review without an entrance point.",
        """
        SELECT s.property_id, s.survey_id, NULL::text AS detail
        FROM property_surveys s
        WHERE s.survey_status IN ('SUBMITTED','APPROVED')
          AND NOT EXISTS (SELECT 1 FROM property_entrances e WHERE e.property_id = s.property_id)
        """,
    ),
    (
        "MISSING_FRONTAGE", "MEDIUM",
        "Survey reached review without a frontage line.",
        """
        SELECT s.property_id, s.survey_id, NULL::text AS detail
        FROM property_surveys s
        WHERE s.survey_status IN ('SUBMITTED','APPROVED')
          AND NOT EXISTS (SELECT 1 FROM property_frontages f WHERE f.property_id = s.property_id)
        """,
    ),
    (
        "MISSING_SERVICE_ZONE", "HIGH",
        "Survey reached review without a service zone - the property cannot be associated.",
        """
        SELECT s.property_id, s.survey_id, NULL::text AS detail
        FROM property_surveys s
        WHERE s.survey_status IN ('SUBMITTED','APPROVED')
          AND NOT EXISTS (SELECT 1 FROM property_service_zones z WHERE z.property_id = s.property_id)
        """,
    ),
    (
        "INVALID_GEOMETRY", "CRITICAL",
        "PostGIS reports the service-zone polygon as invalid.",
        """
        SELECT z.property_id,
               (SELECT survey_id FROM property_surveys s
                 WHERE s.property_id = z.property_id ORDER BY created_at DESC LIMIT 1) AS survey_id,
               ST_IsValidReason(z.geometry) AS detail
        FROM property_service_zones z
        WHERE NOT ST_IsValid(z.geometry)
        """,
    ),
    (
        "SERVICE_ZONE_OVERLAP", "HIGH",
        "Service zone overlaps a neighbouring zone; association would be ambiguous.",
        """
        SELECT a.property_id,
               (SELECT survey_id FROM property_surveys s
                 WHERE s.property_id = a.property_id ORDER BY created_at DESC LIMIT 1) AS survey_id,
               'overlaps ' || b.property_id || ' by ' ||
                 ROUND(ST_Area(ST_Intersection(a.geometry, b.geometry)::geography)::numeric, 1)
                 || ' m2' AS detail
        FROM property_service_zones a
        JOIN property_service_zones b
          ON a.zone_id <> b.zone_id AND ST_Overlaps(a.geometry, b.geometry)
        """,
    ),
    (
        "ENTRANCE_WRONG_SIDE", "MEDIUM",
        "Entrance point does not lie inside its own service zone.",
        """
        SELECT e.property_id,
               (SELECT survey_id FROM property_surveys s
                 WHERE s.property_id = e.property_id ORDER BY created_at DESC LIMIT 1) AS survey_id,
               ROUND(ST_Distance(e.geometry::geography, z.geometry::geography)::numeric, 2)
                 || ' m outside the zone' AS detail
        FROM property_entrances e
        JOIN property_service_zones z ON z.property_id = e.property_id
        WHERE NOT ST_Within(e.geometry, z.geometry)
        """,
    ),
    (
        "LOW_MAPPING_CONFIDENCE", "LOW",
        "Surveyor recorded LOW mapping confidence.",
        """
        SELECT s.property_id, s.survey_id, NULL::text AS detail
        FROM property_surveys s
        WHERE s.mapping_confidence = 'LOW'
          AND s.survey_status IN ('SUBMITTED','APPROVED','CORRECTION_REQUIRED')
        """,
    ),
    (
        "LARGE_GPS_DISPLACEMENT", "MEDIUM",
        "Device GNSS accuracy is worse than the configured threshold.",
        """
        SELECT s.property_id, s.survey_id,
               s.location_accuracy_m || ' m reported accuracy' AS detail
        FROM property_surveys s
        WHERE s.location_accuracy_m IS NOT NULL
          AND s.location_accuracy_m > %(accuracy)s
          AND NOT s.manually_adjusted
        """,
    ),
    (
        "PROPERTY_OUTSIDE_ASSIGNED_AREA", "MEDIUM",
        "Property coordinate falls outside the administrative unit it is assigned to.",
        """
        SELECT p.property_id,
               (SELECT survey_id FROM property_surveys s
                 WHERE s.property_id = p.property_id ORDER BY created_at DESC LIMIT 1) AS survey_id,
               'outside ' || au.name AS detail
        FROM properties p
        JOIN administrative_units au ON au.admin_unit_id = p.admin_unit_id
        WHERE au.geometry IS NOT NULL AND p.latitude IS NOT NULL AND p.longitude IS NOT NULL
          AND NOT ST_Within(ST_SetSRID(ST_MakePoint(p.longitude, p.latitude), 4326), au.geometry)
        """,
    ),
    (
        "PROPERTY_ROUTE_MISMATCH", "LOW",
        "Property route_id does not match the authority code of its route area.",
        """
        SELECT p.property_id,
               (SELECT survey_id FROM property_surveys s
                 WHERE s.property_id = p.property_id ORDER BY created_at DESC LIMIT 1) AS survey_id,
               p.route_id || ' vs ' || au.authority_code AS detail
        FROM properties p
        JOIN administrative_units au ON au.admin_unit_id = p.admin_unit_id
        WHERE au.unit_type = 'ROUTE_AREA' AND au.authority_code IS NOT NULL
          AND p.route_id IS DISTINCT FROM au.authority_code
        """,
    ),
    (
        "DUPLICATE_PROPERTY", "MEDIUM",
        "Another property is mapped within 1 m on the same route.",
        """
        SELECT a.property_id,
               (SELECT survey_id FROM property_surveys s
                 WHERE s.property_id = a.property_id ORDER BY created_at DESC LIMIT 1) AS survey_id,
               'within 1 m of ' || b.property_id AS detail
        FROM properties a JOIN properties b
          ON a.property_id < b.property_id AND a.route_id = b.route_id
         AND a.latitude IS NOT NULL AND b.latitude IS NOT NULL
         AND ST_DWithin(ST_SetSRID(ST_MakePoint(a.longitude, a.latitude), 4326)::geography,
                        ST_SetSRID(ST_MakePoint(b.longitude, b.latitude), 4326)::geography, 1.0)
        """,
    ),
]


def run_all(limit_per_check: int = 500) -> dict:
    """Run every check, upsert OPEN issues, auto-resolve ones that no longer hold."""
    params = {"accuracy": settings.GNSS_ACCURACY_WARN_M}
    summary: dict[str, dict] = {}
    seq = _next_issue_seq()

    for issue_type, severity, description, sql in CHECKS:
        found = fetch_all(f"SELECT * FROM ({sql}) c LIMIT {int(limit_per_check)}",
                          params if "%(accuracy)s" in sql else None)
        opened = 0
        seen = set()
        for row in found:
            pid = row["property_id"]
            if pid in seen:
                continue
            seen.add(pid)
            detail = row.get("detail")
            text = description + (f" ({detail})" if detail else "")
            res = execute(
                """
                INSERT INTO property_qa_issues
                    (issue_id, property_id, survey_id, issue_type, severity, status, description)
                VALUES (%s, %s, %s, %s, %s, 'OPEN', %s)
                ON CONFLICT (property_id, issue_type) WHERE status = 'OPEN'
                DO UPDATE SET description = EXCLUDED.description,
                              severity    = EXCLUDED.severity,
                              survey_id   = COALESCE(EXCLUDED.survey_id, property_qa_issues.survey_id)
                RETURNING (xmax = 0) AS inserted
                """,
                (f"QA-A{seq:06d}", pid, row.get("survey_id"), issue_type, severity, text),
            )
            if res and res.get("inserted"):
                opened += 1
                seq += 1

        # anything previously open for this check that no longer matches is resolved
        still = {r["property_id"] for r in found}
        open_now = fetch_all(
            "SELECT issue_id, property_id FROM property_qa_issues "
            "WHERE issue_type = %s AND status = 'OPEN'", (issue_type,))
        cleared = 0
        for r in open_now:
            if r["property_id"] not in still:
                execute(
                    "UPDATE property_qa_issues SET status='RESOLVED', resolved_at=now() "
                    "WHERE issue_id = %s", (r["issue_id"],))
                cleared += 1

        summary[issue_type] = {"matched": len(seen), "opened": opened, "auto_resolved": cleared}

    total = fetch_one("SELECT count(*) AS n FROM property_qa_issues WHERE status='OPEN'")
    return {"checks": summary, "open_issues": (total or {}).get("n", 0)}


def _next_issue_seq() -> int:
    row = fetch_one(
        "SELECT COALESCE(MAX(NULLIF(regexp_replace(issue_id, '^QA-A?', ''), '')::int), 0) AS n "
        "FROM property_qa_issues WHERE issue_id ~ '^QA-A?[0-9]+$'")
    return int((row or {}).get("n") or 0) + 1

-- =====================================================================
-- Phase 3 - property association as a plain SQL function.
-- Same logic the FastAPI layer runs, available directly in psql / QGIS.
--
--   SELECT * FROM wastraq_lookup_candidates(12.9700600, 77.5902765);
--   SELECT * FROM wastraq_lookup_property(12.9700600, 77.5902765);
--
-- Distances are METRES: the 4326 geometry is cast to `geography` so the
-- spheroid is measured properly instead of pretending degrees are metres.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Candidate list: containment first, then ST_DWithin proximity.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION wastraq_lookup_candidates(
    p_lat      DOUBLE PRECISION,
    p_lon      DOUBLE PRECISION,
    p_radius_m DOUBLE PRECISION DEFAULT 15.0
)
RETURNS TABLE (
    property_id          TEXT,
    zone_id              TEXT,
    inside               BOOLEAN,
    distance_m           DOUBLE PRECISION,
    boundary_margin_m    DOUBLE PRECISION,
    entrance_distance_m  DOUBLE PRECISION
)
LANGUAGE sql STABLE AS $$
    WITH pt AS (
        SELECT ST_SetSRID(ST_MakePoint(p_lon, p_lat), 4326) AS geom
    ),
    contained AS (
        SELECT z.property_id, z.zone_id, TRUE AS inside,
               0.0::double precision AS distance_m,
               ST_Distance(pt.geom::geography, ST_Boundary(z.geometry)::geography) AS boundary_margin_m
        FROM property_service_zones z, pt
        WHERE ST_Within(pt.geom, z.geometry)
    ),
    nearby AS (
        SELECT z.property_id, z.zone_id, FALSE AS inside,
               ST_Distance(pt.geom::geography, z.geometry::geography) AS distance_m,
               NULL::double precision AS boundary_margin_m
        FROM property_service_zones z, pt
        WHERE NOT EXISTS (SELECT 1 FROM contained)
          AND ST_DWithin(pt.geom::geography, z.geometry::geography, p_radius_m)
    ),
    merged AS (
        SELECT * FROM contained
        UNION ALL
        SELECT * FROM nearby
    )
    SELECT m.property_id, m.zone_id, m.inside, m.distance_m, m.boundary_margin_m,
           (SELECT ST_Distance(pt.geom::geography, e.geometry::geography)
              FROM property_entrances e
             WHERE e.property_id = m.property_id
             LIMIT 1) AS entrance_distance_m
    FROM merged m, pt
    ORDER BY m.inside DESC, m.distance_m ASC
    LIMIT 10;
$$;


-- ---------------------------------------------------------------------
-- Decision: AUTO_ASSOCIATED / AMBIGUOUS / NO_MATCH.
-- Never silently forces a property.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION wastraq_lookup_property(
    p_lat           DOUBLE PRECISION,
    p_lon           DOUBLE PRECISION,
    p_radius_m      DOUBLE PRECISION DEFAULT 15.0,
    p_auto_max_m    DOUBLE PRECISION DEFAULT 3.0,
    p_margin_m      DOUBLE PRECISION DEFAULT 2.0
)
RETURNS TABLE (
    decision        TEXT,
    property_id     TEXT,
    confidence      NUMERIC,
    candidate_count INTEGER,
    nearest_m       DOUBLE PRECISION,
    reason          TEXT
)
LANGUAGE sql STABLE AS $$
WITH cand AS (
    SELECT * FROM wastraq_lookup_candidates(p_lat, p_lon, p_radius_m)
),
ranked AS (
    SELECT c.*, ROW_NUMBER() OVER (ORDER BY c.inside DESC, c.distance_m ASC) AS rn
    FROM cand c
),
agg AS (
    SELECT
        COUNT(*)::int                             AS n_total,
        COUNT(*) FILTER (WHERE inside)::int       AS n_inside,
        MIN(distance_m)                           AS d1,
        (SELECT distance_m FROM ranked WHERE rn = 2) AS d2,
        (SELECT property_id FROM ranked WHERE rn = 1) AS best_property,
        (SELECT boundary_margin_m FROM ranked WHERE rn = 1) AS best_margin
    FROM ranked
),
calc AS (
    SELECT a.*,
           COALESCE(a.d2 - a.d1, 1e9) AS sep
    FROM agg a
),
scored AS (
    SELECT c.*,
           LEAST(0.99, 0.95 + LEAST(COALESCE(c.best_margin, 0) / 20.0, 0.04))::numeric(4,3) AS conf_inside,
           (GREATEST(0.0, 0.90 - (COALESCE(c.d1, 0) / GREATEST(p_auto_max_m, 0.001)) * 0.15)
            * (0.6 + 0.4 * LEAST(c.sep / GREATEST(p_margin_m, 0.001), 1.0)))::numeric(4,3) AS conf_near
    FROM calc c
)
SELECT
    CASE
        WHEN s.n_inside = 1 THEN 'AUTO_ASSOCIATED'
        WHEN s.n_inside > 1 THEN 'AMBIGUOUS'
        WHEN s.n_total = 0 THEN 'NO_MATCH'
        WHEN s.d1 > p_auto_max_m OR s.sep < p_margin_m THEN 'AMBIGUOUS'
        ELSE 'AUTO_ASSOCIATED'
    END::text AS decision,
    CASE
        WHEN s.n_inside = 1 THEN s.best_property
        WHEN s.n_inside > 1 THEN NULL
        WHEN s.n_total = 0 THEN NULL
        WHEN s.d1 > p_auto_max_m OR s.sep < p_margin_m THEN NULL
        ELSE s.best_property
    END::text AS property_id,
    CASE
        WHEN s.n_inside = 1 THEN s.conf_inside
        WHEN s.n_inside > 1 THEN 0.0::numeric
        WHEN s.n_total = 0 THEN 0.0::numeric
        ELSE s.conf_near
    END AS confidence,
    s.n_total AS candidate_count,
    s.d1 AS nearest_m,
    CASE
        WHEN s.n_inside = 1 THEN 'Point lies inside exactly one mapped service zone.'
        WHEN s.n_inside > 1 THEN format('Point lies inside %s overlapping service zones; fix the mapping in QGIS.', s.n_inside)
        WHEN s.n_total = 0 THEN format('No service zone within %s m of the position.', p_radius_m)
        WHEN s.d1 > p_auto_max_m OR s.sep < p_margin_m
            THEN format('Nearest zone %s m away, runner-up %s m further; outside auto limits.', round(s.d1::numeric,2), round(LEAST(s.sep,999999)::numeric,2))
        ELSE format('Outside all zones but %s m from a single clear nearest zone.', round(s.d1::numeric,2))
    END::text AS reason
FROM scored s;
$$;

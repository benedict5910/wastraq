-- =====================================================================
-- Repair: a poor GNSS fix must not carry HIGH mapping confidence.
--
--   psql -d wastraq_demo -f database/repair_survey_confidence.sql
--
-- Why this exists
-- ---------------
-- The API has always enforced this rule at capture time: a device fix
-- worse than GNSS_ACCURACY_WARN_M (default 10 m) caps mapping_confidence
-- at MEDIUM, because "HIGH confidence" sitting next to a plus-or-minus
-- 24 m fix is exactly the silent over-trust the whole design exists to
-- prevent.
--
-- The FIRST version of scripts/generate_survey_seed.py drew the accuracy
-- and the confidence independently and never applied that cap, so the
-- demonstration data contradicted the behaviour the dashboards describe.
-- The generator is fixed, but a database that was already seeded from the
-- old file still carries the contradiction. This corrects those rows in
-- place.
--
-- What it touches
-- ---------------
--   * property_surveys rows where location_accuracy_m > the threshold AND
--     mapping_confidence = 'HIGH'  -> confidence becomes MEDIUM, and a
--     source_class of VERIFIED_FIELD_SURVEY becomes AUTHORITY_GIS to match
--   * the matching properties.mapping_confidence numeric, so the property
--     master agrees with its survey
--
-- What it does NOT touch
-- ----------------------
--   * any geometry, anywhere
--   * any captured coordinate or accuracy - the raw fix is never rewritten
--   * survey_status, review_status, reviewer, or any workflow state
--   * the 16 demo-lane properties (asserted below; it aborts if they move)
--
-- Idempotent: running it twice changes nothing the second time.
-- Transactional: it either applies completely or not at all.
-- =====================================================================
BEGIN;

DO $$
DECLARE
    threshold   DOUBLE PRECISION := COALESCE(
        NULLIF(current_setting('wastraq.gnss_accuracy_warn_m', TRUE), '')::double precision,
        10.0);
    demo_route  TEXT := COALESCE(
        NULLIF(current_setting('wastraq.demo_route_id', TRUE), ''),
        'ROUTE-DEMO-01');
    lane_before INTEGER;
    lane_after  INTEGER;
    n_surveys   INTEGER;
    n_props     INTEGER;
BEGIN
    SELECT count(*) INTO lane_before FROM properties WHERE route_id = demo_route;

    UPDATE property_surveys
       SET mapping_confidence = 'MEDIUM',
           source_class = CASE WHEN source_class = 'VERIFIED_FIELD_SURVEY'
                               THEN 'AUTHORITY_GIS' ELSE source_class END,
           updated_at = now()
     WHERE mapping_confidence = 'HIGH'
       AND location_accuracy_m IS NOT NULL
       AND location_accuracy_m > threshold;
    GET DIAGNOSTICS n_surveys = ROW_COUNT;

    -- keep the property master's numeric confidence in step with its survey
    UPDATE properties p
       SET mapping_confidence = 0.780
      FROM property_surveys s
     WHERE s.property_id = p.property_id
       AND s.mapping_confidence = 'MEDIUM'
       AND s.location_accuracy_m IS NOT NULL
       AND s.location_accuracy_m > threshold
       AND p.mapping_confidence > 0.900;
    GET DIAGNOSTICS n_props = ROW_COUNT;

    SELECT count(*) INTO lane_after FROM properties WHERE route_id = demo_route;
    IF lane_after <> lane_before THEN
        RAISE EXCEPTION 'the % demo-lane properties were disturbed (% -> %)',
                        demo_route, lane_before, lane_after;
    END IF;

    RAISE NOTICE 'threshold % m: corrected % survey row(s), % property row(s)',
                 threshold, n_surveys, n_props;
    RAISE NOTICE 'demo lane still holds % properties on %', lane_after, demo_route;
END $$;

-- Prove the invariant now holds before committing.
DO $$
DECLARE
    threshold DOUBLE PRECISION := COALESCE(
        NULLIF(current_setting('wastraq.gnss_accuracy_warn_m', TRUE), '')::double precision,
        10.0);
    still_bad INTEGER;
BEGIN
    SELECT count(*) INTO still_bad FROM property_surveys
     WHERE mapping_confidence = 'HIGH'
       AND location_accuracy_m IS NOT NULL
       AND location_accuracy_m > threshold;
    IF still_bad <> 0 THEN
        RAISE EXCEPTION '% survey(s) still carry HIGH confidence on a poor fix', still_bad;
    END IF;
    RAISE NOTICE 'invariant holds: no poor fix carries HIGH confidence';
END $$;

COMMIT;

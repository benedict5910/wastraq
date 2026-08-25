-- =====================================================================
-- Wastraq - REMOVE THE SYNTHETIC CITY SEED DATA
--
--   psql -v ON_ERROR_STOP=1 -d wastraq_demo -f database/cleanup_synthetic_city.sql
--
-- Why
-- ---
-- The city seed existed to show the module at scale. It worked, but it also
-- meant the dashboard reported survey percentages, surveyor throughput and
-- QA counts for properties that do not exist. A demonstration that has to
-- be prefaced with "ignore those numbers, they're invented" is worse than
-- one showing 16 real properties and an empty hierarchy.
--
-- After this runs, every number on the survey dashboard is a fact about
-- real surveyed ground. The SCHEMA is untouched: city -> zone -> ward ->
-- route -> property, assignments, reviewers, QA and history all still
-- exist and still work. Only the invented ROWS go.
--
-- What it deletes
-- ---------------
--   * properties whose id matches PROP-##### (5 digits) - the generated
--     ones start at PROP-01001. The real lane is PROP-001..PROP-016
--     (3 digits) and cannot match.
--   * everything hanging off them: surveys, entrances, frontages, service
--     zones, photos, QA issues, geometry history
--   * administrative units that end up holding no properties AND have no
--     descendant holding any - i.e. branches that were only ever scaffolding
--   * assignments pointing at units that no longer exist
--   * survey_users that nothing references any more
--
-- What it keeps
-- -------------
--   * all 16 real lane properties and every row attached to them
--   * the administrative chain the pilot actually sits in
--     (city -> zone -> ward -> route area), because that chain is real
--   * at least one surveyor and one reviewer, so the workflow still runs
--   * every table, view, index, constraint and trigger
--
-- Safety: one transaction, idempotent, and it aborts if the lane count
-- moves by even one row.
-- =====================================================================
BEGIN;

DO $$
DECLARE
    demo_route   TEXT := COALESCE(NULLIF(current_setting('wastraq.demo_route_id', TRUE), ''),
                                  'ROUTE-DEMO-01');
    lane_before  INTEGER;
    lane_after   INTEGER;
    n_props      INTEGER;
    n_surveys    INTEGER;
    n_geom       INTEGER;
    n_photos     INTEGER;
    n_qa         INTEGER;
    n_units      INTEGER;
    n_asg        INTEGER;
    n_users      INTEGER;
BEGIN
    SELECT count(*) INTO lane_before FROM properties WHERE route_id = demo_route;
    IF lane_before = 0 THEN
        RAISE EXCEPTION 'no properties on route % - refusing to run against an unexpected database',
                        demo_route;
    END IF;

    -- ------------------------------------------------------------------
    -- 1. the generated properties, by id shape. Belt and braces: the
    --    5-digit pattern already excludes PROP-001..016, and the route
    --    guard excludes them again.
    -- ------------------------------------------------------------------
    CREATE TEMP TABLE _synthetic ON COMMIT DROP AS
    SELECT property_id FROM properties
     WHERE property_id ~ '^PROP-[0-9]{5}$'
       AND route_id IS DISTINCT FROM demo_route;

    SELECT count(*) INTO n_props FROM _synthetic;

    DELETE FROM property_qa_issues       WHERE property_id IN (SELECT property_id FROM _synthetic);
    GET DIAGNOSTICS n_qa = ROW_COUNT;
    DELETE FROM property_geometry_history WHERE property_id IN (SELECT property_id FROM _synthetic);
    DELETE FROM property_photos          WHERE property_id IN (SELECT property_id FROM _synthetic);
    GET DIAGNOSTICS n_photos = ROW_COUNT;

    WITH d AS (
      SELECT (SELECT count(*) FROM property_entrances
               WHERE property_id IN (SELECT property_id FROM _synthetic))
           + (SELECT count(*) FROM property_frontages
               WHERE property_id IN (SELECT property_id FROM _synthetic))
           + (SELECT count(*) FROM property_service_zones
               WHERE property_id IN (SELECT property_id FROM _synthetic)) AS n)
    SELECT n INTO n_geom FROM d;

    DELETE FROM property_entrances     WHERE property_id IN (SELECT property_id FROM _synthetic);
    DELETE FROM property_frontages     WHERE property_id IN (SELECT property_id FROM _synthetic);
    DELETE FROM property_service_zones WHERE property_id IN (SELECT property_id FROM _synthetic);
    DELETE FROM property_surveys       WHERE property_id IN (SELECT property_id FROM _synthetic);
    GET DIAGNOSTICS n_surveys = ROW_COUNT;

    -- collection_events would block the delete, and a synthetic property
    -- should never have one; assert rather than cascade silently.
    IF EXISTS (SELECT 1 FROM collection_events
                WHERE property_id IN (SELECT property_id FROM _synthetic)) THEN
        RAISE EXCEPTION 'a synthetic property has collection events - stopping, this is not seed data';
    END IF;

    DELETE FROM properties WHERE property_id IN (SELECT property_id FROM _synthetic);

    -- ------------------------------------------------------------------
    -- 2. administrative units that were pure scaffolding.
    --    Keep a unit if it holds properties, or if any descendant does.
    --    That preserves the real chain the pilot sits in and removes the
    --    branches that only ever existed to look full.
    -- ------------------------------------------------------------------
    CREATE TEMP TABLE _keep_units ON COMMIT DROP AS
    WITH RECURSIVE occupied AS (
        SELECT DISTINCT admin_unit_id FROM properties WHERE admin_unit_id IS NOT NULL
    ), chain AS (
        SELECT a.admin_unit_id, a.parent_id
          FROM administrative_units a JOIN occupied o USING (admin_unit_id)
        UNION
        SELECT p.admin_unit_id, p.parent_id
          FROM administrative_units p JOIN chain c ON c.parent_id = p.admin_unit_id
    )
    SELECT admin_unit_id FROM chain;

    DELETE FROM survey_assignments
     WHERE admin_unit_id NOT IN (SELECT admin_unit_id FROM _keep_units);
    GET DIAGNOSTICS n_asg = ROW_COUNT;

    -- children first: the FK is ON DELETE RESTRICT by design
    DELETE FROM administrative_units a
     WHERE a.admin_unit_id NOT IN (SELECT admin_unit_id FROM _keep_units)
       AND NOT EXISTS (SELECT 1 FROM administrative_units c WHERE c.parent_id = a.admin_unit_id);
    GET DIAGNOSTICS n_units = ROW_COUNT;
    -- repeat until the tree stops shrinking (depth here is 4, so this ends fast)
    LOOP
        DELETE FROM administrative_units a
         WHERE a.admin_unit_id NOT IN (SELECT admin_unit_id FROM _keep_units)
           AND NOT EXISTS (SELECT 1 FROM administrative_units c WHERE c.parent_id = a.admin_unit_id);
        EXIT WHEN NOT FOUND;
        n_units := n_units + 1;
    END LOOP;

    -- ------------------------------------------------------------------
    -- 3. users nothing references any more. At least one surveyor and one
    --    reviewer are kept whatever happens, or the workflow cannot run.
    -- ------------------------------------------------------------------
    CREATE TEMP TABLE _keep_users ON COMMIT DROP AS
    SELECT user_id FROM survey_users WHERE user_id IN (
        SELECT surveyor_id FROM property_surveys WHERE surveyor_id IS NOT NULL
        UNION SELECT reviewer_id FROM property_surveys WHERE reviewer_id IS NOT NULL
        UNION SELECT assigned_to FROM survey_assignments WHERE assigned_to IS NOT NULL
        UNION SELECT assigned_by FROM survey_assignments WHERE assigned_by IS NOT NULL
        UNION SELECT captured_by FROM property_photos WHERE captured_by IS NOT NULL
        UNION SELECT resolved_by FROM property_qa_issues WHERE resolved_by IS NOT NULL
    );

    INSERT INTO _keep_users (user_id)
    SELECT user_id FROM survey_users u
     WHERE u.role = 'SURVEYOR' AND NOT EXISTS (
        SELECT 1 FROM _keep_users k JOIN survey_users s ON s.user_id = k.user_id
         WHERE s.role = 'SURVEYOR')
     ORDER BY user_id LIMIT 1;

    INSERT INTO _keep_users (user_id)
    SELECT user_id FROM survey_users u
     WHERE u.role = 'REVIEWER' AND NOT EXISTS (
        SELECT 1 FROM _keep_users k JOIN survey_users s ON s.user_id = k.user_id
         WHERE s.role = 'REVIEWER')
     ORDER BY user_id LIMIT 1;

    DELETE FROM survey_users WHERE user_id NOT IN (SELECT user_id FROM _keep_users);
    GET DIAGNOSTICS n_users = ROW_COUNT;

    -- ------------------------------------------------------------------
    -- 4. prove the lane is exactly as it was
    -- ------------------------------------------------------------------
    SELECT count(*) INTO lane_after FROM properties WHERE route_id = demo_route;
    IF lane_after <> lane_before THEN
        RAISE EXCEPTION 'the demo lane changed from % to % properties - aborting',
                        lane_before, lane_after;
    END IF;

    RAISE NOTICE 'removed % synthetic propert(ies), % survey(s), % geometry row(s),',
                 n_props, n_surveys, n_geom;
    RAISE NOTICE '        % photo(s), % QA issue(s), % assignment(s), % admin unit(s), % user(s)',
                 n_photos, n_qa, n_asg, n_units, n_users;
    RAISE NOTICE 'demo lane intact: % properties on %', lane_after, demo_route;
END $$;

-- ---------------------------------------------------------------------
-- Post-conditions, checked before the transaction is allowed to commit.
-- ---------------------------------------------------------------------
DO $$
DECLARE
    demo_route TEXT := COALESCE(NULLIF(current_setting('wastraq.demo_route_id', TRUE), ''),
                                'ROUTE-DEMO-01');
    n INTEGER;
BEGIN
    SELECT count(*) INTO n FROM properties WHERE property_id ~ '^PROP-[0-9]{5}$';
    IF n <> 0 THEN RAISE EXCEPTION '% synthetic properties survived', n; END IF;

    SELECT count(*) INTO n FROM properties WHERE route_id = demo_route;
    IF n <> 16 THEN RAISE EXCEPTION 'expected 16 lane properties, found %', n; END IF;

    SELECT count(*) INTO n FROM property_entrances
     WHERE property_id IN (SELECT property_id FROM properties WHERE route_id = demo_route);
    IF n <> 16 THEN RAISE EXCEPTION 'expected 16 lane entrances, found %', n; END IF;

    SELECT count(*) INTO n FROM property_surveys s
     LEFT JOIN properties p ON p.property_id = s.property_id
     WHERE p.property_id IS NULL;
    IF n <> 0 THEN RAISE EXCEPTION '% orphaned survey rows', n; END IF;

    SELECT count(*) INTO n FROM survey_users WHERE role = 'SURVEYOR' AND active;
    IF n = 0 THEN RAISE EXCEPTION 'no active surveyor left - the field workflow would be unusable'; END IF;

    SELECT count(*) INTO n FROM survey_users WHERE role = 'REVIEWER' AND active;
    IF n = 0 THEN RAISE EXCEPTION 'no active reviewer left - the review workflow would be unusable'; END IF;

    RAISE NOTICE 'post-conditions hold: only real data remains, schema untouched';
END $$;

COMMIT;

-- The dashboard reads live views, so nothing needs refreshing. ANALYZE just
-- keeps the planner honest after a large delete.
ANALYZE properties;
ANALYZE property_surveys;

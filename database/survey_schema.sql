-- =====================================================================
-- Wastraq - CITY-SCALE SURVEY SCHEMA
--
-- Additive and idempotent. Safe to run repeatedly on a live database.
-- It never drops, truncates or rewrites the operational demo tables
-- (properties, pickers, collection_events, evidence) - it only adds new
-- tables and adds nullable columns to existing ones.
--
--   psql -v ON_ERROR_STOP=1 -d wastraq_demo -f database/survey_schema.sql
--
-- Design notes
-- ------------
-- * ONE property master. The 16 surveyed demo-lane properties and the
--   city-scale properties live in the same `properties` table; they are
--   separated by route_id / admin_unit_id, not by a parallel table.
-- * Administrative hierarchy is self-referencing so a deployment can use
--   CITY > ZONE > WARD > ROUTE_AREA, or any other jurisdiction shape,
--   without a schema change.
-- * Geometry is versioned. A trigger snapshots the previous row into
--   property_geometry_history on every UPDATE or DELETE, so a survey
--   correction can never silently destroy what was there before.
-- * The device GNSS fix and the final approved entrance are stored
--   SEPARATELY. A phone coordinate is an anchor, not ground truth.
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 1. administrative_units - flexible jurisdiction hierarchy
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS administrative_units (
    admin_unit_id  TEXT PRIMARY KEY,                       -- CITY-MYS, WARD-W12 ...
    name           TEXT NOT NULL,
    -- named unit_type rather than "type": "type" reads ambiguously in SQL
    unit_type      TEXT NOT NULL
                   CHECK (unit_type IN ('CITY','ZONE','WARD','DISTRICT','ROUTE_AREA')),
    parent_id      TEXT REFERENCES administrative_units(admin_unit_id)
                   ON UPDATE CASCADE ON DELETE RESTRICT,
    authority_code TEXT,
    geometry       GEOMETRY(MULTIPOLYGON, 4326),
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (parent_id IS DISTINCT FROM admin_unit_id)
);

CREATE INDEX IF NOT EXISTS idx_admin_units_parent ON administrative_units (parent_id);
CREATE INDEX IF NOT EXISTS idx_admin_units_type   ON administrative_units (unit_type);
CREATE INDEX IF NOT EXISTS idx_admin_units_geom   ON administrative_units USING GIST (geometry);


-- ---------------------------------------------------------------------
-- 2. survey_users - surveyors, reviewers, supervisors
--    Deliberately minimal: this is not an IAM system.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS survey_users (
    user_id            TEXT PRIMARY KEY,                   -- USR-001 ...
    name               TEXT NOT NULL,
    employee_id        TEXT UNIQUE,
    role               TEXT NOT NULL
                       CHECK (role IN ('SURVEYOR','REVIEWER','GIS_ADMIN','SUPERVISOR','ADMIN')),
    email              TEXT,
    phone              TEXT,
    active             BOOLEAN NOT NULL DEFAULT TRUE,
    assigned_authority TEXT REFERENCES administrative_units(admin_unit_id)
                       ON UPDATE CASCADE ON DELETE SET NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_survey_users_role ON survey_users (role) WHERE active;


-- ---------------------------------------------------------------------
-- 3. survey_assignments - a chunk of work handed to a surveyor
--
--    total/surveyed/verified counts are denormalised snapshots kept for
--    convenience. The dashboards read v_assignment_progress instead,
--    which recomputes them live from property_surveys.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS survey_assignments (
    assignment_id    TEXT PRIMARY KEY,                     -- ASG-0001 ...
    admin_unit_id    TEXT NOT NULL REFERENCES administrative_units(admin_unit_id)
                     ON UPDATE CASCADE ON DELETE RESTRICT,
    route_id         TEXT,
    assigned_to      TEXT REFERENCES survey_users(user_id)
                     ON UPDATE CASCADE ON DELETE SET NULL,
    assigned_by      TEXT REFERENCES survey_users(user_id)
                     ON UPDATE CASCADE ON DELETE SET NULL,
    status           TEXT NOT NULL DEFAULT 'NOT_STARTED'
                     CHECK (status IN ('NOT_STARTED','IN_PROGRESS','SUBMITTED','COMPLETED','ON_HOLD')),
    total_properties INTEGER NOT NULL DEFAULT 0,
    surveyed_count   INTEGER NOT NULL DEFAULT 0,
    verified_count   INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    due_date         DATE,
    completed_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_assignments_unit   ON survey_assignments (admin_unit_id);
CREATE INDEX IF NOT EXISTS idx_assignments_to     ON survey_assignments (assigned_to);
CREATE INDEX IF NOT EXISTS idx_assignments_status ON survey_assignments (status);


-- ---------------------------------------------------------------------
-- 4. property_surveys - one survey attempt against one property
--
--    Includes the device location-capture block. captured_* is what the
--    phone/GNSS reported; the authoritative entrance lives in
--    property_entrances and may have been moved on the map afterwards.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS property_surveys (
    survey_id            TEXT PRIMARY KEY,                 -- SRV-000001 ...
    property_id          TEXT NOT NULL REFERENCES properties(property_id)
                         ON UPDATE CASCADE ON DELETE CASCADE,
    assignment_id        TEXT REFERENCES survey_assignments(assignment_id)
                         ON UPDATE CASCADE ON DELETE SET NULL,
    surveyor_id          TEXT REFERENCES survey_users(user_id)
                         ON UPDATE CASCADE ON DELETE SET NULL,
    survey_status        TEXT NOT NULL DEFAULT 'NOT_SURVEYED'
                         CHECK (survey_status IN
                               ('NOT_SURVEYED','IN_PROGRESS','SUBMITTED',
                                'APPROVED','CORRECTION_REQUIRED','REJECTED')),
    survey_started_at    TIMESTAMPTZ,
    survey_completed_at  TIMESTAMPTZ,
    mapping_confidence   TEXT CHECK (mapping_confidence IN ('HIGH','MEDIUM','LOW')),
    source_class         TEXT CHECK (source_class IN
                               ('VERIFIED_FIELD_SURVEY','AUTHORITY_GIS','THIRD_PARTY_MAP',
                                'APPROXIMATE_GEOCODE','UNVERIFIED')),
    notes                TEXT,
    anomaly_type         TEXT[] NOT NULL DEFAULT '{}',     -- several anomalies per survey
    submitted_at         TIMESTAMPTZ,
    reviewer_id          TEXT REFERENCES survey_users(user_id)
                         ON UPDATE CASCADE ON DELETE SET NULL,
    reviewed_at          TIMESTAMPTZ,
    review_status        TEXT CHECK (review_status IN
                               ('PENDING','APPROVED','CORRECTION_REQUIRED','REJECTED')),
    review_notes         TEXT,

    -- --- device location capture -------------------------------------
    captured_latitude    DOUBLE PRECISION,
    captured_longitude   DOUBLE PRECISION,
    captured_point       GEOMETRY(POINT, 4326),            -- the raw GNSS fix, kept forever
    location_accuracy_m  DOUBLE PRECISION,
    location_source      TEXT CHECK (location_source IN
                               ('DEVICE_GNSS','MANUAL_MAP','SIMULATED',
                                'EXTERNAL_BLUETOOTH_GNSS','RTK_GNSS')),
    captured_at          TIMESTAMPTZ,
    captured_by          TEXT REFERENCES survey_users(user_id)
                         ON UPDATE CASCADE ON DELETE SET NULL,
    capture_device       TEXT,
    manually_adjusted    BOOLEAN NOT NULL DEFAULT FALSE,
    adjustment_timestamp TIMESTAMPTZ,
    adjusted_by          TEXT REFERENCES survey_users(user_id)
                         ON UPDATE CASCADE ON DELETE SET NULL,

    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- one survey row per property per assignment; re-surveys go under a
    -- new assignment so the earlier record is preserved
    UNIQUE (property_id, assignment_id)
);

CREATE INDEX IF NOT EXISTS idx_surveys_property   ON property_surveys (property_id);
CREATE INDEX IF NOT EXISTS idx_surveys_assignment ON property_surveys (assignment_id);
CREATE INDEX IF NOT EXISTS idx_surveys_surveyor   ON property_surveys (surveyor_id);
CREATE INDEX IF NOT EXISTS idx_surveys_status     ON property_surveys (survey_status);
CREATE INDEX IF NOT EXISTS idx_surveys_captured   ON property_surveys USING GIST (captured_point);
CREATE INDEX IF NOT EXISTS idx_surveys_recent     ON property_surveys (property_id, created_at DESC);


-- ---------------------------------------------------------------------
-- 5. Extend the operational tables (all additive, all nullable)
-- ---------------------------------------------------------------------
ALTER TABLE properties ADD COLUMN IF NOT EXISTS admin_unit_id TEXT;
DO $$ BEGIN
  ALTER TABLE properties ADD CONSTRAINT properties_admin_unit_fk
    FOREIGN KEY (admin_unit_id) REFERENCES administrative_units(admin_unit_id)
    ON UPDATE CASCADE ON DELETE SET NULL;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE INDEX IF NOT EXISTS idx_properties_admin_unit ON properties (admin_unit_id);
CREATE INDEX IF NOT EXISTS idx_properties_route      ON properties (route_id);

-- Widen the QA vocabulary on an already-installed database. CREATE TABLE
-- IF NOT EXISTS above is a no-op there, so the constraint has to be replaced
-- explicitly or the new issue types would violate it.
DO $$
BEGIN
  IF to_regclass('public.property_qa_issues') IS NOT NULL THEN
    ALTER TABLE property_qa_issues DROP CONSTRAINT IF EXISTS property_qa_issues_issue_type_check;
    ALTER TABLE property_qa_issues ADD  CONSTRAINT property_qa_issues_issue_type_check
      CHECK (issue_type IN (
        'SERVICE_ZONE_OVERLAP','ENTRANCE_WRONG_SIDE','MISSING_ENTRANCE',
        'MISSING_FRONTAGE','MISSING_SERVICE_ZONE','INVALID_GEOMETRY',
        'PROPERTY_ROUTE_MISMATCH','DUPLICATE_PROPERTY','LOW_MAPPING_CONFIDENCE',
        'LARGE_GPS_DISPLACEMENT','SHARED_GATE','COMMON_COLLECTION_POINT',
        'MANUAL_REVIEW_REQUIRED','PROPERTY_OUTSIDE_ASSIGNED_AREA',
        'GEOMETRY_NEEDS_REVIEW','ENTRANCE_FAR_FROM_FRONTAGE'));
  END IF;
END $$;

-- Contact and service-classification fields the surveyor fills in on site.
-- These are the only property columns the field screen lets a human edit;
-- everything else on that screen (ids, timestamps, surveyor) is populated by
-- the system, because a surveyor typing an identifier is a data-entry bug
-- waiting to happen.
ALTER TABLE properties ADD COLUMN IF NOT EXISTS owner_phone         TEXT;
ALTER TABLE properties ADD COLUMN IF NOT EXISTS owner_email         TEXT;
ALTER TABLE properties ADD COLUMN IF NOT EXISTS street_name         TEXT;
ALTER TABLE properties ADD COLUMN IF NOT EXISTS locality            TEXT;
ALTER TABLE properties ADD COLUMN IF NOT EXISTS pincode             TEXT;
ALTER TABLE properties ADD COLUMN IF NOT EXISTS service_entity_type TEXT;
ALTER TABLE properties ADD COLUMN IF NOT EXISTS updated_at          TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE properties DROP CONSTRAINT IF EXISTS properties_service_entity_type_check;
ALTER TABLE properties ADD  CONSTRAINT properties_service_entity_type_check
  CHECK (service_entity_type IS NULL OR service_entity_type IN
        ('SINGLE_HOUSEHOLD','MULTI_HOUSEHOLD','APARTMENT_BLOCK','SHOP',
         'RESTAURANT','OFFICE','INSTITUTION','BULK_GENERATOR','COMMON_COLLECTION_POINT'));

-- properties.verification_status gains the operational-clearance state
ALTER TABLE properties DROP CONSTRAINT IF EXISTS properties_verification_status_check;
ALTER TABLE properties ADD  CONSTRAINT properties_verification_status_check
  CHECK (verification_status IN
        ('UNVERIFIED','FIELD_SURVEYED','FIELD_VERIFIED','VERIFIED_FOR_OPERATION','DISPUTED'));

-- geometry provenance on all three geometry tables
DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['property_entrances','property_frontages','property_service_zones'] LOOP
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1', t);
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS created_by TEXT', t);
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS verified_by TEXT', t);
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ', t);
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS survey_id TEXT', t);
    EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()', t);
  END LOOP;
END $$;


-- ---------------------------------------------------------------------
-- 6. property_photos - survey evidence types + provenance
-- ---------------------------------------------------------------------
ALTER TABLE property_photos ADD COLUMN IF NOT EXISTS survey_id   TEXT;
ALTER TABLE property_photos ADD COLUMN IF NOT EXISTS captured_by TEXT;
ALTER TABLE property_photos ADD COLUMN IF NOT EXISTS sha256      TEXT;
ALTER TABLE property_photos ADD COLUMN IF NOT EXISTS bytes       BIGINT;

-- How the image actually arrived. A photo taken on the device camera at the
-- gate and a file dragged in from a laptop are not equivalent evidence, and
-- the difference has to survive into the database - not just be a label in
-- the UI that nobody can audit later.
ALTER TABLE property_photos ADD COLUMN IF NOT EXISTS capture_method TEXT;
ALTER TABLE property_photos ADD COLUMN IF NOT EXISTS capture_latitude  DOUBLE PRECISION;
ALTER TABLE property_photos ADD COLUMN IF NOT EXISTS capture_longitude DOUBLE PRECISION;
ALTER TABLE property_photos DROP CONSTRAINT IF EXISTS property_photos_capture_method_check;
ALTER TABLE property_photos ADD  CONSTRAINT property_photos_capture_method_check
  CHECK (capture_method IS NULL OR capture_method IN
        ('DEVICE_CAMERA','UPLOADED_FILE','SEED_SAMPLE'));

ALTER TABLE property_photos DROP CONSTRAINT IF EXISTS property_photos_photo_type_check;
ALTER TABLE property_photos ADD  CONSTRAINT property_photos_photo_type_check
  CHECK (photo_type IN ('FRONTAGE','HOUSE_NUMBER','GATE','ENTRANCE','CONTEXT','DISPUTE','OTHER'));

-- The old index allowed only ONE frontage photo per property ever, which
-- blocks a re-survey. Scope uniqueness to the survey instead.
DROP INDEX IF EXISTS idx_property_photos_one_frontage;
CREATE UNIQUE INDEX IF NOT EXISTS idx_property_photos_one_frontage_per_survey
    ON property_photos (property_id, COALESCE(survey_id, ''))
    WHERE photo_type = 'FRONTAGE';
CREATE INDEX IF NOT EXISTS idx_property_photos_survey ON property_photos (survey_id);


-- ---------------------------------------------------------------------
-- 7. property_geometry_history - nothing is overwritten silently
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS property_geometry_history (
    history_id    BIGSERIAL PRIMARY KEY,
    property_id   TEXT NOT NULL,
    geometry_kind TEXT NOT NULL
                  CHECK (geometry_kind IN ('ENTRANCE','FRONTAGE','SERVICE_ZONE')),
    feature_id    TEXT,                                    -- ENT-001 / FRONT-001 / SZ-001
    version       INTEGER,
    geometry      GEOMETRY(GEOMETRY, 4326),
    source        TEXT,
    verified      BOOLEAN,
    survey_id     TEXT,
    operation     TEXT NOT NULL CHECK (operation IN ('UPDATE','DELETE')),
    changed_by    TEXT,
    changed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_geom_history_property ON property_geometry_history (property_id, changed_at DESC);

CREATE OR REPLACE FUNCTION wq_snapshot_geometry() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    kind TEXT;
    fid  TEXT;
    j    JSONB;
BEGIN
    kind := CASE TG_TABLE_NAME
              WHEN 'property_entrances'     THEN 'ENTRANCE'
              WHEN 'property_frontages'     THEN 'FRONTAGE'
              WHEN 'property_service_zones' THEN 'SERVICE_ZONE'
            END;
    -- The three tables use different primary-key names. A CASE over
    -- OLD.<col> will not do: plpgsql resolves every branch against the
    -- actual record type, so OLD.entrance_id errors on the zones table.
    -- Going through jsonb keeps one trigger function for all three.
    j := to_jsonb(OLD);
    fid := COALESCE(j->>'entrance_id', j->>'frontage_id', j->>'zone_id');

    -- On UPDATE only record a change that actually altered the geometry
    -- or its trust markers; touching updated_at should not spam history.
    IF TG_OP = 'UPDATE'
       AND OLD.geometry IS NOT DISTINCT FROM NEW.geometry
       AND OLD.verified IS NOT DISTINCT FROM NEW.verified
       AND OLD.source   IS NOT DISTINCT FROM NEW.source THEN
        RETURN NEW;
    END IF;

    INSERT INTO property_geometry_history
        (property_id, geometry_kind, feature_id, version, geometry,
         source, verified, survey_id, operation, changed_by)
    VALUES
        (OLD.property_id, kind, fid, OLD.version, OLD.geometry,
         OLD.source, OLD.verified, OLD.survey_id, TG_OP, OLD.created_by);

    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;

DO $$
DECLARE t TEXT;
BEGIN
  FOREACH t IN ARRAY ARRAY['property_entrances','property_frontages','property_service_zones'] LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS trg_%s_history ON %I', t, t);
    EXECUTE format($f$CREATE TRIGGER trg_%s_history
                      BEFORE UPDATE OR DELETE ON %I
                      FOR EACH ROW EXECUTE FUNCTION wq_snapshot_geometry()$f$, t, t);
  END LOOP;
END $$;


-- ---------------------------------------------------------------------
-- 8. property_qa_issues - GIS quality assurance
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS property_qa_issues (
    issue_id    TEXT PRIMARY KEY,                          -- QA-000001 ...
    property_id TEXT REFERENCES properties(property_id)
                ON UPDATE CASCADE ON DELETE CASCADE,
    survey_id   TEXT REFERENCES property_surveys(survey_id)
                ON UPDATE CASCADE ON DELETE SET NULL,
    issue_type  TEXT NOT NULL CHECK (issue_type IN (
                    'SERVICE_ZONE_OVERLAP','ENTRANCE_WRONG_SIDE','MISSING_ENTRANCE',
                    'MISSING_FRONTAGE','MISSING_SERVICE_ZONE','INVALID_GEOMETRY',
                    'PROPERTY_ROUTE_MISMATCH','DUPLICATE_PROPERTY','LOW_MAPPING_CONFIDENCE',
                    'LARGE_GPS_DISPLACEMENT','SHARED_GATE','COMMON_COLLECTION_POINT',
                    'MANUAL_REVIEW_REQUIRED','PROPERTY_OUTSIDE_ASSIGNED_AREA',
                    'GEOMETRY_NEEDS_REVIEW','ENTRANCE_FAR_FROM_FRONTAGE')),
    severity    TEXT NOT NULL DEFAULT 'MEDIUM'
                CHECK (severity IN ('LOW','MEDIUM','HIGH','CRITICAL')),
    status      TEXT NOT NULL DEFAULT 'OPEN'
                CHECK (status IN ('OPEN','ACKNOWLEDGED','RESOLVED','WONT_FIX')),
    description TEXT,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    resolved_by TEXT REFERENCES survey_users(user_id)
                ON UPDATE CASCADE ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_qa_property ON property_qa_issues (property_id);
CREATE INDEX IF NOT EXISTS idx_qa_status   ON property_qa_issues (status, severity);
CREATE INDEX IF NOT EXISTS idx_qa_type     ON property_qa_issues (issue_type);
-- the same open issue is not recorded twice for the same property
CREATE UNIQUE INDEX IF NOT EXISTS idx_qa_open_unique
    ON property_qa_issues (property_id, issue_type) WHERE status = 'OPEN';


-- ---------------------------------------------------------------------
-- 9. Views the dashboards read
-- ---------------------------------------------------------------------

-- Full ancestry of every admin unit, so a query can filter by any level.
CREATE OR REPLACE VIEW v_admin_unit_tree AS
WITH RECURSIVE walk AS (
    SELECT admin_unit_id, name, unit_type, parent_id, authority_code, active,
           admin_unit_id AS root_id, 1 AS depth,
           ARRAY[admin_unit_id] AS path,
           name AS full_path
    FROM administrative_units WHERE parent_id IS NULL
    UNION ALL
    SELECT c.admin_unit_id, c.name, c.unit_type, c.parent_id, c.authority_code, c.active,
           w.root_id, w.depth + 1,
           w.path || c.admin_unit_id,
           w.full_path || ' / ' || c.name
    FROM administrative_units c JOIN walk w ON c.parent_id = w.admin_unit_id
)
SELECT * FROM walk;

-- The current survey for each property (latest row wins).
CREATE OR REPLACE VIEW v_property_current_survey AS
SELECT DISTINCT ON (s.property_id)
       s.*
FROM property_surveys s
ORDER BY s.property_id, s.created_at DESC, s.survey_id DESC;

-- One row per property with everything the survey dashboards need.
CREATE OR REPLACE VIEW v_survey_property_status AS
SELECT
    p.property_id,
    p.authority_property_id,
    p.house_number,
    p.owner_name,
    p.formatted_address,
    p.property_type,
    p.route_id,
    p.latitude,
    p.longitude,
    p.mapping_confidence      AS property_mapping_confidence,
    p.verification_status,
    p.admin_unit_id,
    au.name                   AS admin_unit_name,
    au.unit_type              AS admin_unit_type,
    ward.admin_unit_id        AS ward_id,
    ward.name                 AS ward_name,
    zone.admin_unit_id        AS zone_id,
    zone.name                 AS zone_name,
    cs.survey_id,
    cs.assignment_id,
    cs.surveyor_id,
    su.name                   AS surveyor_name,
    COALESCE(cs.survey_status, 'NOT_SURVEYED') AS survey_status,
    cs.mapping_confidence     AS survey_mapping_confidence,
    cs.source_class,
    cs.submitted_at,
    cs.reviewed_at,
    cs.review_status,
    cs.location_accuracy_m,
    cs.manually_adjusted,
    (e.entrance_id IS NOT NULL) AS has_entrance,
    (f.frontage_id IS NOT NULL) AS has_frontage,
    (z.zone_id     IS NOT NULL) AS has_service_zone,
    (SELECT count(*) FROM property_photos ph WHERE ph.property_id = p.property_id) AS photo_count,
    (SELECT count(*) FROM property_qa_issues q
      WHERE q.property_id = p.property_id AND q.status = 'OPEN')                   AS open_qa_issues,
    -- Appended, never inserted mid-list: CREATE OR REPLACE VIEW only allows
    -- new columns at the END, so re-running this file on a live database
    -- has to keep the existing order untouched.
    (SELECT count(*) FROM property_photos ph
      WHERE ph.property_id = p.property_id AND ph.photo_type = 'FRONTAGE')         AS frontage_photos,
    p.owner_phone,
    p.owner_email,
    p.street_name,
    p.locality,
    p.pincode,
    p.service_entity_type,
    p.updated_at              AS property_updated_at
FROM properties p
LEFT JOIN administrative_units au   ON au.admin_unit_id = p.admin_unit_id
LEFT JOIN administrative_units ward ON ward.admin_unit_id =
          CASE WHEN au.unit_type = 'WARD' THEN au.admin_unit_id ELSE au.parent_id END
LEFT JOIN administrative_units zone ON zone.admin_unit_id = ward.parent_id
LEFT JOIN v_property_current_survey cs ON cs.property_id = p.property_id
LEFT JOIN survey_users su           ON su.user_id = cs.surveyor_id
LEFT JOIN property_entrances e      ON e.property_id = p.property_id
LEFT JOIN property_frontages f      ON f.property_id = p.property_id
LEFT JOIN property_service_zones z  ON z.property_id = p.property_id;

-- Live assignment progress, recomputed rather than trusted from counters.
CREATE OR REPLACE VIEW v_assignment_progress AS
SELECT
    a.assignment_id,
    a.admin_unit_id,
    au.name  AS admin_unit_name,
    au.unit_type AS admin_unit_type,
    a.route_id,
    a.assigned_to,
    su.name  AS assigned_to_name,
    a.assigned_by,
    a.status,
    a.due_date,
    a.created_at,
    a.completed_at,
    COUNT(s.survey_id)                                                        AS survey_rows,
    COALESCE(NULLIF(a.total_properties, 0), COUNT(s.survey_id))               AS total_properties,
    COUNT(*) FILTER (WHERE s.survey_status IN
        ('SUBMITTED','APPROVED','CORRECTION_REQUIRED','REJECTED'))            AS surveyed_count,
    COUNT(*) FILTER (WHERE s.survey_status = 'APPROVED')                      AS verified_count,
    COUNT(*) FILTER (WHERE s.survey_status = 'SUBMITTED')                     AS pending_review_count,
    COUNT(*) FILTER (WHERE s.survey_status = 'CORRECTION_REQUIRED')           AS correction_count,
    -- Derived from SCOPE, not from placeholder rows.
    -- Counting NOT_SURVEYED/IN_PROGRESS rows only works when the assignment
    -- eagerly seeded a row per property. An assignment created with
    -- include_properties = false has real scope and no rows, and used to
    -- report outstanding = 0 - i.e. "nothing left to do" for work that had
    -- not been started. Scope minus surveyed is correct either way.
    GREATEST(
        COALESCE(NULLIF(a.total_properties, 0), COUNT(s.survey_id))
      - COUNT(*) FILTER (WHERE s.survey_status IN
            ('SUBMITTED','APPROVED','CORRECTION_REQUIRED','REJECTED')),
        0)::bigint                                                            AS outstanding_count
FROM survey_assignments a
LEFT JOIN administrative_units au ON au.admin_unit_id = a.admin_unit_id
LEFT JOIN survey_users su         ON su.user_id = a.assigned_to
LEFT JOIN property_surveys s      ON s.assignment_id = a.assignment_id
GROUP BY a.assignment_id, au.name, au.unit_type, su.name;

-- Surveyor workload / throughput.
CREATE OR REPLACE VIEW v_surveyor_performance AS
SELECT
    u.user_id,
    u.name,
    u.employee_id,
    u.active,
    COUNT(s.survey_id)                                              AS assigned_surveys,
    COUNT(*) FILTER (WHERE s.survey_status = 'APPROVED')            AS approved,
    COUNT(*) FILTER (WHERE s.survey_status = 'SUBMITTED')           AS pending_review,
    COUNT(*) FILTER (WHERE s.survey_status = 'CORRECTION_REQUIRED') AS correction_required,
    COUNT(*) FILTER (WHERE s.survey_status = 'IN_PROGRESS')         AS in_progress,
    COUNT(*) FILTER (WHERE s.survey_status IN
        ('SUBMITTED','APPROVED','CORRECTION_REQUIRED','REJECTED'))  AS completed,
    ROUND(100.0 * COUNT(*) FILTER (WHERE s.survey_status = 'CORRECTION_REQUIRED')
          / NULLIF(COUNT(*) FILTER (WHERE s.survey_status IN
            ('SUBMITTED','APPROVED','CORRECTION_REQUIRED','REJECTED')), 0), 1) AS correction_rate_pct,
    COUNT(DISTINCT date_trunc('day', s.survey_completed_at))         AS active_days,
    ROUND(COUNT(*) FILTER (WHERE s.survey_completed_at IS NOT NULL)::numeric
          / NULLIF(COUNT(DISTINCT date_trunc('day', s.survey_completed_at)), 0), 1) AS avg_per_day
FROM survey_users u
LEFT JOIN property_surveys s ON s.surveyor_id = u.user_id
WHERE u.role = 'SURVEYOR'
GROUP BY u.user_id, u.name, u.employee_id, u.active;

COMMIT;

-- =====================================================================
-- Wastraq - PROPERTY MASTER / PROPERTY REGISTRATION
--
-- Additive and idempotent. Safe to run repeatedly on a live database.
-- It adds nullable columns, widens CHECK vocabularies and creates one
-- new table plus one new view. It never drops, truncates or reseeds
-- anything, so the real 16-property pilot lane is untouched.
--
--   psql -v ON_ERROR_STOP=1 -d wastraq_demo -f database/property_master.sql
--
-- Run AFTER database/survey_schema.sql - this file assumes the survey
-- layer's columns (owner_phone, street_name, service_entity_type ...)
-- already exist.
--
-- Design notes
-- ------------
-- * ONE property master, still. This extends `properties`; it does not
--   create a competing registration table. Registration and field survey
--   are two screens over the same row.
-- * The REGISTRATION coordinate and the SURVEYED geometry are separate
--   by design:
--       properties.captured_*           - "roughly here", typed by an
--                                         office/field clerk's phone
--       property_entrances/frontages/   - the operational GIS truth,
--       property_service_zones            drawn and reviewed
--   Collapsing them would let an unreviewed phone fix silently become
--   the thing that decides which property a picker collected from.
-- * Vocabularies are WIDENED, never replaced. Every value already
--   present in the pilot data stays legal; the new registration values
--   are added alongside. Nothing has to be rewritten to migrate.
-- * A new property is PENDING_SURVEY. Only the reviewer's approval in
--   the survey workflow can produce VERIFIED_FOR_OPERATION.
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 1. Registration columns on properties
-- ---------------------------------------------------------------------

-- Reference coordinate captured at registration time. Indicative only:
-- this is where the clerk was standing, not where waste is collected.
ALTER TABLE properties ADD COLUMN IF NOT EXISTS captured_latitude   DOUBLE PRECISION;
ALTER TABLE properties ADD COLUMN IF NOT EXISTS captured_longitude  DOUBLE PRECISION;
ALTER TABLE properties ADD COLUMN IF NOT EXISTS captured_accuracy_m DOUBLE PRECISION;
ALTER TABLE properties ADD COLUMN IF NOT EXISTS captured_at         TIMESTAMPTZ;
ALTER TABLE properties ADD COLUMN IF NOT EXISTS location_source     TEXT;

-- Who touched the administrative record. Free text rather than a FK to
-- survey_users: a municipal clerk registering properties is not
-- necessarily a surveyor, and requiring an account would block the demo.
ALTER TABLE properties ADD COLUMN IF NOT EXISTS created_by          TEXT;
ALTER TABLE properties ADD COLUMN IF NOT EXISTS updated_by          TEXT;

-- Retired/demolished/merged properties are deactivated, never deleted:
-- collection_events reference them and history must stay readable.
ALTER TABLE properties ADD COLUMN IF NOT EXISTS active               BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE properties ADD COLUMN IF NOT EXISTS inactive_reason      TEXT;

ALTER TABLE properties DROP CONSTRAINT IF EXISTS properties_location_source_check;
ALTER TABLE properties ADD  CONSTRAINT properties_location_source_check
  CHECK (location_source IS NULL OR location_source IN
        ('DEVICE_GEOLOCATION','MANUAL_MAP_PICK','MANUAL_ENTRY','IMPORTED','SEED_SAMPLE'));

COMMENT ON COLUMN properties.captured_latitude IS
  'Registration reference fix. Indicative only - association uses property_service_zones.';
COMMENT ON COLUMN properties.captured_accuracy_m IS
  'Reported GNSS accuracy in metres at registration. A poor fix does not block registration.';


-- ---------------------------------------------------------------------
-- 2. Property type - widened vocabulary
--
-- Legacy values (RESIDENTIAL, COMMERCIAL, MIXED, INSTITUTIONAL,
-- INDUSTRIAL, VACANT) are kept so the existing pilot rows remain valid
-- and no UPDATE is needed. The registration UI offers the new, more
-- specific list; legacy values render with their own labels.
-- ---------------------------------------------------------------------
ALTER TABLE properties DROP CONSTRAINT IF EXISTS properties_property_type_check;
ALTER TABLE properties ADD  CONSTRAINT properties_property_type_check
  CHECK (property_type IN (
        -- registration vocabulary
        'INDEPENDENT_HOUSE','APARTMENT','SHOP','COMMERCIAL_BUILDING','OFFICE',
        'SCHOOL','HOSPITAL','HOTEL','MARKET','GATED_COMMUNITY','INDUSTRIAL',
        'VACANT_PROPERTY','OTHER',
        -- legacy vocabulary, still accepted
        'RESIDENTIAL','COMMERCIAL','MIXED','INSTITUTIONAL','VACANT'));


-- ---------------------------------------------------------------------
-- 3. Service entity type - widened vocabulary
--
-- Deliberately a separate axis from property_type: "what kind of
-- building is this" and "what does the collection vehicle service here"
-- are different questions. A GATED_COMMUNITY may be serviced as a
-- COMMON_COLLECTION_POINT.
-- ---------------------------------------------------------------------
ALTER TABLE properties DROP CONSTRAINT IF EXISTS properties_service_entity_type_check;
ALTER TABLE properties ADD  CONSTRAINT properties_service_entity_type_check
  CHECK (service_entity_type IS NULL OR service_entity_type IN (
        -- registration vocabulary
        'INDIVIDUAL_PROPERTY','BUILDING','COMMON_COLLECTION_POINT',
        'COMMERCIAL_COMPLEX','COMMUNITY_COLLECTION_POINT','OTHER',
        -- legacy vocabulary, still accepted
        'SINGLE_HOUSEHOLD','MULTI_HOUSEHOLD','APARTMENT_BLOCK','SHOP',
        'RESTAURANT','OFFICE','INSTITUTION','BULK_GENERATOR'));


-- ---------------------------------------------------------------------
-- 4. verification_status gains PENDING_SURVEY
--
-- A freshly registered property has an administrative record and no
-- field truth. UNVERIFIED does not distinguish "nobody has looked at
-- this yet" from "registered and waiting for a surveyor", and the
-- Property Master needs that distinction as a KPI.
-- ---------------------------------------------------------------------
ALTER TABLE properties DROP CONSTRAINT IF EXISTS properties_verification_status_check;
ALTER TABLE properties ADD  CONSTRAINT properties_verification_status_check
  CHECK (verification_status IN
        ('PENDING_SURVEY','UNVERIFIED','FIELD_SURVEYED','FIELD_VERIFIED',
         'VERIFIED_FOR_OPERATION','DISPUTED'));


-- ---------------------------------------------------------------------
-- 5. Search support
--
-- The master screen searches by owner name, phone, house number,
-- authority id and address. At 16 rows an index is theatre; at city
-- scale it is not, and adding it now costs nothing.
-- ---------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_properties_route        ON properties(route_id);
CREATE INDEX IF NOT EXISTS idx_properties_admin_unit   ON properties(admin_unit_id);
CREATE INDEX IF NOT EXISTS idx_properties_verification ON properties(verification_status);
CREATE INDEX IF NOT EXISTS idx_properties_authority    ON properties(authority_property_id)
    WHERE authority_property_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_properties_owner_lower  ON properties(lower(owner_name));
CREATE INDEX IF NOT EXISTS idx_properties_house_lower  ON properties(lower(house_number));


-- ---------------------------------------------------------------------
-- 6. property_change_log - administrative audit trail
--
-- property_geometry_history already versions the GIS. This is its
-- administrative counterpart: who changed the owner's phone number and
-- when. One row per changed field, so a diff is readable without
-- storing whole JSON snapshots.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS property_change_log (
    change_id    BIGSERIAL PRIMARY KEY,
    property_id  TEXT NOT NULL REFERENCES properties(property_id)
                 ON UPDATE CASCADE ON DELETE CASCADE,
    changed_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    changed_by   TEXT,
    action       TEXT NOT NULL
                 CHECK (action IN ('CREATED','UPDATED','LOCATION_CAPTURED',
                                   'DEACTIVATED','REACTIVATED','SENT_TO_SURVEY')),
    field_name   TEXT,
    old_value    TEXT,
    new_value    TEXT,
    note         TEXT
);

CREATE INDEX IF NOT EXISTS idx_property_change_log_prop
    ON property_change_log(property_id, changed_at DESC);


-- ---------------------------------------------------------------------
-- 7. v_property_master
--
-- One row per property, joining the administrative record to the survey
-- state and the presence/absence of each piece of GIS. Kept separate
-- from v_survey_property_status: that view answers "how is the survey
-- going", this one answers "what do we have on record".
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_property_master AS
SELECT
    p.property_id,
    p.authority_property_id,
    p.house_number,
    p.owner_name,
    p.owner_phone,
    p.owner_email,
    p.street_name,
    p.locality,
    p.pincode,
    p.formatted_address,
    p.property_type,
    p.service_entity_type,
    p.route_id,
    p.admin_unit_id,
    p.latitude,
    p.longitude,
    p.captured_latitude,
    p.captured_longitude,
    p.captured_accuracy_m,
    p.captured_at,
    p.location_source,
    p.mapping_confidence,
    p.verification_status,
    p.active,
    p.inactive_reason,
    p.created_by,
    p.updated_by,
    p.created_at,
    p.updated_at,
    au.name                    AS admin_unit_name,
    au.unit_type               AS admin_unit_type,
    ward.admin_unit_id         AS ward_id,
    ward.name                  AS ward_name,
    zone.admin_unit_id         AS zone_id,
    zone.name                  AS zone_name,
    COALESCE(cs.survey_status, 'NOT_SURVEYED') AS survey_status,
    cs.survey_id,
    cs.surveyor_id,
    su.name                    AS surveyor_name,
    cs.submitted_at,
    cs.reviewed_at,
    cs.review_status,
    (e.entrance_id IS NOT NULL)                AS has_entrance,
    (f.frontage_id IS NOT NULL)                AS has_frontage,
    (z.zone_id     IS NOT NULL)                AS has_service_zone,
    (SELECT count(*) > 0 FROM property_photos ph
      WHERE ph.property_id = p.property_id AND ph.photo_type = 'FRONTAGE')
                                               AS has_frontage_photo,
    (SELECT count(*) FROM property_qa_issues q
      WHERE q.property_id = p.property_id AND q.status = 'OPEN')
                                               AS open_qa_issues,
    GREATEST(p.updated_at, COALESCE(cs.updated_at, p.updated_at))
                                               AS last_activity_at
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

COMMENT ON VIEW v_property_master IS
  'Property Registration / Property Master screen. Administrative record + survey state + GIS presence.';

COMMIT;

-- ---------------------------------------------------------------------
-- Deliberately NOT in this file:
--   * any INSERT into properties. The Property Master is populated by
--     real registrations, not by a seed.
--   * any UPDATE of existing rows. Widening a CHECK does not require
--     rewriting data, which is exactly why the legacy vocabulary is
--     kept rather than migrated.
-- ---------------------------------------------------------------------

-- =====================================================================
-- Wastraq Source Segregation Evidence Engine - DEMO SCHEMA
-- Database: wastraq_demo
-- Requires: PostgreSQL 14+ with PostGIS 3.x
--
-- Core rule encoded here:
--   A property is NEVER identified by "nearest vehicle GPS point".
--   Association is done against a mapped GIS structure:
--     entrance point + frontage line + service-zone polygon.
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS postgis;

-- Idempotent for demo re-runs (drop children first)
DROP TABLE IF EXISTS property_photos      CASCADE;
DROP TABLE IF EXISTS evidence              CASCADE;
DROP TABLE IF EXISTS collection_events     CASCADE;
DROP TABLE IF EXISTS property_service_zones CASCADE;
DROP TABLE IF EXISTS property_frontages    CASCADE;
DROP TABLE IF EXISTS property_entrances    CASCADE;
DROP TABLE IF EXISTS pickers               CASCADE;
DROP TABLE IF EXISTS properties            CASCADE;


-- ---------------------------------------------------------------------
-- 1. properties
-- ---------------------------------------------------------------------
CREATE TABLE properties (
    property_id           TEXT PRIMARY KEY,                    -- PROP-001 ...
    authority_property_id TEXT,                                -- ULB / PID reference
    house_number          TEXT,
    owner_name            TEXT,
    formatted_address     TEXT,
    property_type         TEXT NOT NULL DEFAULT 'RESIDENTIAL'
                          CHECK (property_type IN
                                ('RESIDENTIAL','COMMERCIAL','MIXED','INSTITUTIONAL')),
    route_id              TEXT,
    latitude              DOUBLE PRECISION,                    -- indicative centroid only
    longitude             DOUBLE PRECISION,                    -- NOT used for association
    mapping_confidence    NUMERIC(4,3) DEFAULT 1.000
                          CHECK (mapping_confidence BETWEEN 0 AND 1),
    verification_status   TEXT NOT NULL DEFAULT 'UNVERIFIED'
                          CHECK (verification_status IN
                                ('UNVERIFIED','FIELD_SURVEYED','FIELD_VERIFIED','DISPUTED')),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON COLUMN properties.latitude IS
  'Indicative centroid for map display only. Property association uses property_service_zones.';


-- ---------------------------------------------------------------------
-- 2. property_entrances  (POINT)
-- ---------------------------------------------------------------------
CREATE TABLE property_entrances (
    entrance_id  TEXT PRIMARY KEY,                             -- ENT-001 ...
    property_id  TEXT NOT NULL REFERENCES properties(property_id)
                 ON UPDATE CASCADE ON DELETE CASCADE,
    geometry     GEOMETRY(POINT, 4326) NOT NULL,
    verified     BOOLEAN NOT NULL DEFAULT FALSE,
    -- FIELD_SURVEY | FIELD_SURVEY_PLUS_AUTO_GEOMETRY | QGIS_MANUAL | SYNTHETIC
    source       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_entrances_geom ON property_entrances USING GIST (geometry);
CREATE INDEX idx_entrances_property ON property_entrances (property_id);


-- ---------------------------------------------------------------------
-- 3. property_frontages  (LINESTRING)
-- ---------------------------------------------------------------------
CREATE TABLE property_frontages (
    frontage_id  TEXT PRIMARY KEY,                             -- FRONT-001 ...
    property_id  TEXT NOT NULL REFERENCES properties(property_id)
                 ON UPDATE CASCADE ON DELETE CASCADE,
    geometry     GEOMETRY(LINESTRING, 4326) NOT NULL,
    road_side    TEXT CHECK (road_side IN ('NORTH','SOUTH','EAST','WEST')),
    verified     BOOLEAN NOT NULL DEFAULT FALSE,
    source       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_frontages_geom ON property_frontages USING GIST (geometry);
CREATE INDEX idx_frontages_property ON property_frontages (property_id);


-- ---------------------------------------------------------------------
-- 4. property_service_zones  (POLYGON)  <-- the association surface
-- ---------------------------------------------------------------------
CREATE TABLE property_service_zones (
    zone_id      TEXT PRIMARY KEY,                             -- SZ-001 ...
    property_id  TEXT NOT NULL REFERENCES properties(property_id)
                 ON UPDATE CASCADE ON DELETE CASCADE,
    geometry     GEOMETRY(POLYGON, 4326) NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    verified     BOOLEAN NOT NULL DEFAULT FALSE,
    source       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Required spatial index: every lookup hits this.
CREATE INDEX idx_service_zones_geom ON property_service_zones USING GIST (geometry);
CREATE INDEX idx_service_zones_property ON property_service_zones (property_id);

-- Only one active zone version per property in this demo.
CREATE UNIQUE INDEX idx_service_zones_property_version
    ON property_service_zones (property_id, version);


-- ---------------------------------------------------------------------
-- 5. pickers
-- ---------------------------------------------------------------------
CREATE TABLE pickers (
    picker_id    TEXT PRIMARY KEY,                             -- PICKER-01 ...
    picker_name  TEXT NOT NULL,
    rfid_uid     TEXT UNIQUE,
    active       BOOLEAN NOT NULL DEFAULT TRUE
);


-- ---------------------------------------------------------------------
-- 6. collection_events
-- ---------------------------------------------------------------------
CREATE TABLE collection_events (
    event_id               TEXT PRIMARY KEY,                   -- EVENT-001 ...
    property_id            TEXT NOT NULL REFERENCES properties(property_id)
                           ON UPDATE CASCADE ON DELETE RESTRICT,
    picker_id              TEXT REFERENCES pickers(picker_id)
                           ON UPDATE CASCADE ON DELETE SET NULL,
    track_id               TEXT,                               -- camera/GNSS track reference
    collected              BOOLEAN NOT NULL DEFAULT TRUE,
    -- Normal collection is SEGREGATED by default.
    -- The picker only acts when waste is NOT segregated.
    segregation_status     TEXT NOT NULL DEFAULT 'SEGREGATED'
                           CHECK (segregation_status IN
                                 ('SEGREGATED','NOT_SEGREGATED')),
    association_confidence NUMERIC(4,3)
                           CHECK (association_confidence BETWEEN 0 AND 1),
    collection_time        TIMESTAMPTZ NOT NULL DEFAULT now(),
    rfid_triggered         BOOLEAN NOT NULL DEFAULT FALSE,
    review_status          TEXT NOT NULL DEFAULT 'AUTO_CONFIRMED'
                           CHECK (review_status IN
                                 ('AUTO_CONFIRMED','NEEDS_REVIEW','REVIEWED_OK','REVIEWED_REJECTED'))
);

CREATE INDEX idx_events_property ON collection_events (property_id);
CREATE INDEX idx_events_picker   ON collection_events (picker_id);
CREATE INDEX idx_events_time     ON collection_events (collection_time DESC);


-- ---------------------------------------------------------------------
-- 7. evidence
-- ---------------------------------------------------------------------
CREATE TABLE evidence (
    evidence_id   TEXT PRIMARY KEY,                            -- EVID-001 ...
    event_id      TEXT NOT NULL REFERENCES collection_events(event_id)
                  ON UPDATE CASCADE ON DELETE CASCADE,
    evidence_type TEXT NOT NULL
                  CHECK (evidence_type IN
                        ('COLLECTION_PROOF','NON_SEGREGATION_PROOF','VIDEO_CLIP','CAMERA_FRAME')),
    file_path     TEXT NOT NULL,
    captured_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified      BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX idx_evidence_event ON evidence (event_id);


-- ---------------------------------------------------------------------
-- 8. property_photos  (survey QA / human verification only)
--
-- The frontage photo exists so a human can confirm a mapping, settle a
-- dispute, or correct geometry. It is deliberately NOT part of the live
-- association path - that stays property_service_zones.
-- ---------------------------------------------------------------------
CREATE TABLE property_photos (
    photo_id     TEXT PRIMARY KEY,                            -- PHOTO-001 ...
    property_id  TEXT NOT NULL REFERENCES properties(property_id)
                 ON UPDATE CASCADE ON DELETE CASCADE,
    photo_type   TEXT NOT NULL DEFAULT 'FRONTAGE'
                 CHECK (photo_type IN ('FRONTAGE','ENTRANCE','CONTEXT','DISPUTE')),
    file_path    TEXT NOT NULL,
    captured_at  TIMESTAMPTZ,
    verified     BOOLEAN NOT NULL DEFAULT FALSE,
    notes        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_property_photos_property ON property_photos (property_id);
-- at most one frontage photo per property
CREATE UNIQUE INDEX idx_property_photos_one_frontage
    ON property_photos (property_id) WHERE photo_type = 'FRONTAGE';


-- ---------------------------------------------------------------------
-- Convenience view for the dashboard
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_collection_summary AS
SELECT
    ce.event_id,
    ce.property_id,
    p.house_number,
    p.owner_name,
    p.formatted_address,
    ce.picker_id,
    pk.picker_name,
    ce.collected,
    ce.segregation_status,
    ce.association_confidence,
    ce.collection_time,
    ce.rfid_triggered,
    ce.review_status,
    COUNT(e.evidence_id) AS evidence_count
FROM collection_events ce
JOIN properties p  ON p.property_id = ce.property_id
LEFT JOIN pickers pk ON pk.picker_id = ce.picker_id
LEFT JOIN evidence e ON e.event_id = ce.event_id
GROUP BY ce.event_id, p.house_number, p.owner_name, p.formatted_address, pk.picker_name;

-- =====================================================================
-- Wastraq demo - REAL 16-property lane
-- 2nd Cross Road, Krishnamurthy Puram, Mysuru
--
-- GENERATED FILE. Edit scripts/generate_real_lane.py and re-run:
--     python3 scripts/generate_real_lane.py
--
-- Anchors      : 16 surveyed entrance/service points (EPSG:4326)
-- Construction : EPSG:32643 (WGS84 / UTM 43N), metres
-- Road bearing : 96.0 deg   lane 85.0 m long, 8.6 m across
-- South side   : PROP-010, PROP-009, PROP-008, PROP-007, PROP-006, PROP-005, PROP-004, PROP-003, PROP-002, PROP-001
-- North side   : PROP-011, PROP-012, PROP-013, PROP-014, PROP-015, PROP-016
--
-- Frontages and service zones are PROVISIONAL auto-generated geometry:
--     source   = FIELD_SURVEY_PLUS_AUTO_GEOMETRY
--     verified = false
-- Adjust them in QGIS and set verified = true as they are checked.
--
-- Runs in a single transaction. Does not drop the database and does not
-- touch collection_events, evidence or pickers.
-- =====================================================================

\set ON_ERROR_STOP on
BEGIN;

-- ---------------------------------------------------------------------
-- Additive schema changes (safe on an already-loaded demo database)
-- ---------------------------------------------------------------------
ALTER TABLE property_entrances     ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE property_frontages     ADD COLUMN IF NOT EXISTS source TEXT;
ALTER TABLE property_service_zones ADD COLUMN IF NOT EXISTS source TEXT;

-- properties.verification_status gains FIELD_SURVEYED
ALTER TABLE properties DROP CONSTRAINT IF EXISTS properties_verification_status_check;
ALTER TABLE properties ADD  CONSTRAINT properties_verification_status_check
  CHECK (verification_status IN ('UNVERIFIED','FIELD_SURVEYED','FIELD_VERIFIED','DISPUTED'));

-- Frontage photo linkage. Survey QA / human verification / dispute review only -
-- never the primary property-recognition mechanism (that stays the service zone).
CREATE TABLE IF NOT EXISTS property_photos (
    photo_id     TEXT PRIMARY KEY,
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
CREATE INDEX IF NOT EXISTS idx_property_photos_property ON property_photos (property_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_property_photos_one_frontage
    ON property_photos (property_id) WHERE photo_type = 'FRONTAGE';

-- ---------------------------------------------------------------------
-- 1. Upsert the 16 properties (administrative data stays dummy)
-- ---------------------------------------------------------------------
INSERT INTO properties (property_id, authority_property_id, house_number, owner_name,
                        formatted_address, property_type, route_id,
                        latitude, longitude, mapping_confidence, verification_status)
VALUES
  ('PROP-001','ULB-KMP-1001','D001','Demo Owner 01',
   '2nd Cross Road, Krishnamurthy Puram, Mysuru, Karnataka 570014','RESIDENTIAL','ROUTE-DEMO-01',
   12.29425630, 76.64186490, 0.900, 'FIELD_SURVEYED'),
  ('PROP-002','ULB-KMP-1002','D002','Demo Owner 02',
   '2nd Cross Road, Krishnamurthy Puram, Mysuru, Karnataka 570014','RESIDENTIAL','ROUTE-DEMO-01',
   12.29427790, 76.64176330, 0.900, 'FIELD_SURVEYED'),
  ('PROP-003','ULB-KMP-1003','D003','Demo Owner 03',
   '2nd Cross Road, Krishnamurthy Puram, Mysuru, Karnataka 570014','RESIDENTIAL','ROUTE-DEMO-01',
   12.29429950, 76.64165560, 0.900, 'FIELD_SURVEYED'),
  ('PROP-004','ULB-KMP-1004','D004','Demo Owner 04',
   '2nd Cross Road, Krishnamurthy Puram, Mysuru, Karnataka 570014','RESIDENTIAL','ROUTE-DEMO-01',
   12.29430970, 76.64155170, 0.900, 'FIELD_SURVEYED'),
  ('PROP-005','ULB-KMP-1005','D005','Demo Owner 05',
   '2nd Cross Road, Krishnamurthy Puram, Mysuru, Karnataka 570014','RESIDENTIAL','ROUTE-DEMO-01',
   12.29430050, 76.64152150, 0.900, 'FIELD_SURVEYED'),
  ('PROP-006','ULB-KMP-1006','D006','Demo Owner 06',
   '2nd Cross Road, Krishnamurthy Puram, Mysuru, Karnataka 570014','RESIDENTIAL','ROUTE-DEMO-01',
   12.29430840, 76.64144740, 0.900, 'FIELD_SURVEYED'),
  ('PROP-007','ULB-KMP-1007','D007','Demo Owner 07',
   '2nd Cross Road, Krishnamurthy Puram, Mysuru, Karnataka 570014','RESIDENTIAL','ROUTE-DEMO-01',
   12.29431130, 76.64136500, 0.900, 'FIELD_SURVEYED'),
  ('PROP-008','ULB-KMP-1008','D008','Demo Owner 08',
   '2nd Cross Road, Krishnamurthy Puram, Mysuru, Karnataka 570014','RESIDENTIAL','ROUTE-DEMO-01',
   12.29433820, 76.64129860, 0.900, 'FIELD_SURVEYED'),
  ('PROP-009','ULB-KMP-1009','D009','Demo Owner 09',
   '2nd Cross Road, Krishnamurthy Puram, Mysuru, Karnataka 570014','RESIDENTIAL','ROUTE-DEMO-01',
   12.29434080, 76.64120800, 0.900, 'FIELD_SURVEYED'),
  ('PROP-010','ULB-KMP-1010','D010','Demo Owner 10',
   '2nd Cross Road, Krishnamurthy Puram, Mysuru, Karnataka 570014','RESIDENTIAL','ROUTE-DEMO-01',
   12.29434770, 76.64111720, 0.900, 'FIELD_SURVEYED'),
  ('PROP-011','ULB-KMP-1011','D011','Demo Owner 11',
   '2nd Cross Road, Krishnamurthy Puram, Mysuru, Karnataka 570014','RESIDENTIAL','ROUTE-DEMO-01',
   12.29440210, 76.64109540, 0.900, 'FIELD_SURVEYED'),
  ('PROP-012','ULB-KMP-1012','D012','Demo Owner 12',
   '2nd Cross Road, Krishnamurthy Puram, Mysuru, Karnataka 570014','RESIDENTIAL','ROUTE-DEMO-01',
   12.29438110, 76.64139580, 0.900, 'FIELD_SURVEYED'),
  ('PROP-013','ULB-KMP-1013','D013','Demo Owner 13',
   '2nd Cross Road, Krishnamurthy Puram, Mysuru, Karnataka 570014','RESIDENTIAL','ROUTE-DEMO-01',
   12.29438080, 76.64144310, 0.900, 'FIELD_SURVEYED'),
  ('PROP-014','ULB-KMP-1014','D014','Demo Owner 14',
   '2nd Cross Road, Krishnamurthy Puram, Mysuru, Karnataka 570014','RESIDENTIAL','ROUTE-DEMO-01',
   12.29435520, 76.64158050, 0.900, 'FIELD_SURVEYED'),
  ('PROP-015','ULB-KMP-1015','D015','Demo Owner 15',
   '2nd Cross Road, Krishnamurthy Puram, Mysuru, Karnataka 570014','RESIDENTIAL','ROUTE-DEMO-01',
   12.29433690, 76.64169620, 0.900, 'FIELD_SURVEYED'),
  ('PROP-016','ULB-KMP-1016','D016','Demo Owner 16',
   '2nd Cross Road, Krishnamurthy Puram, Mysuru, Karnataka 570014','RESIDENTIAL','ROUTE-DEMO-01',
   12.29431890, 76.64183200, 0.900, 'FIELD_SURVEYED')
ON CONFLICT (property_id) DO UPDATE SET
    authority_property_id = EXCLUDED.authority_property_id,
    house_number          = EXCLUDED.house_number,
    owner_name            = EXCLUDED.owner_name,
    formatted_address     = EXCLUDED.formatted_address,
    property_type         = EXCLUDED.property_type,
    route_id              = EXCLUDED.route_id,
    latitude              = EXCLUDED.latitude,
    longitude             = EXCLUDED.longitude,
    mapping_confidence    = EXCLUDED.mapping_confidence,
    verification_status   = EXCLUDED.verification_status;

-- Retire any synthetic property that is not part of the real lane.
-- Guarded: a property that already carries collection events is kept, so
-- foreign keys and history are never broken.
DELETE FROM properties p
 WHERE p.property_id <> ALL (ARRAY['PROP-001','PROP-002','PROP-003','PROP-004','PROP-005','PROP-006','PROP-007','PROP-008','PROP-009','PROP-010','PROP-011','PROP-012','PROP-013','PROP-014','PROP-015','PROP-016'])
   AND NOT EXISTS (SELECT 1 FROM collection_events c WHERE c.property_id = p.property_id);

-- ---------------------------------------------------------------------
-- 2. Replace the synthetic geometry for the real lane
-- ---------------------------------------------------------------------
DELETE FROM property_service_zones;
DELETE FROM property_frontages;
DELETE FROM property_entrances;

-- 2a. Surveyed entrance / service anchors (exact on-site coordinates)
INSERT INTO property_entrances (entrance_id, property_id, geometry, verified, source) VALUES
  ('ENT-001','PROP-001',ST_SetSRID(ST_MakePoint(76.64186490, 12.29425630), 4326), TRUE, 'FIELD_SURVEY'),
  ('ENT-002','PROP-002',ST_SetSRID(ST_MakePoint(76.64176330, 12.29427790), 4326), TRUE, 'FIELD_SURVEY'),
  ('ENT-003','PROP-003',ST_SetSRID(ST_MakePoint(76.64165560, 12.29429950), 4326), TRUE, 'FIELD_SURVEY'),
  ('ENT-004','PROP-004',ST_SetSRID(ST_MakePoint(76.64155170, 12.29430970), 4326), TRUE, 'FIELD_SURVEY'),
  ('ENT-005','PROP-005',ST_SetSRID(ST_MakePoint(76.64152150, 12.29430050), 4326), TRUE, 'FIELD_SURVEY'),
  ('ENT-006','PROP-006',ST_SetSRID(ST_MakePoint(76.64144740, 12.29430840), 4326), TRUE, 'FIELD_SURVEY'),
  ('ENT-007','PROP-007',ST_SetSRID(ST_MakePoint(76.64136500, 12.29431130), 4326), TRUE, 'FIELD_SURVEY'),
  ('ENT-008','PROP-008',ST_SetSRID(ST_MakePoint(76.64129860, 12.29433820), 4326), TRUE, 'FIELD_SURVEY'),
  ('ENT-009','PROP-009',ST_SetSRID(ST_MakePoint(76.64120800, 12.29434080), 4326), TRUE, 'FIELD_SURVEY'),
  ('ENT-010','PROP-010',ST_SetSRID(ST_MakePoint(76.64111720, 12.29434770), 4326), TRUE, 'FIELD_SURVEY'),
  ('ENT-011','PROP-011',ST_SetSRID(ST_MakePoint(76.64109540, 12.29440210), 4326), TRUE, 'FIELD_SURVEY'),
  ('ENT-012','PROP-012',ST_SetSRID(ST_MakePoint(76.64139580, 12.29438110), 4326), TRUE, 'FIELD_SURVEY'),
  ('ENT-013','PROP-013',ST_SetSRID(ST_MakePoint(76.64144310, 12.29438080), 4326), TRUE, 'FIELD_SURVEY'),
  ('ENT-014','PROP-014',ST_SetSRID(ST_MakePoint(76.64158050, 12.29435520), 4326), TRUE, 'FIELD_SURVEY'),
  ('ENT-015','PROP-015',ST_SetSRID(ST_MakePoint(76.64169620, 12.29433690), 4326), TRUE, 'FIELD_SURVEY'),
  ('ENT-016','PROP-016',ST_SetSRID(ST_MakePoint(76.64183200, 12.29431890), 4326), TRUE, 'FIELD_SURVEY');

-- 2b. Provisional frontages (auto-generated, road side computed from geometry)
INSERT INTO property_frontages (frontage_id, property_id, geometry, road_side, verified, source) VALUES
  ('FRONT-001','PROP-001',ST_GeomFromText('LINESTRING(76.64181545 12.29426681, 76.64192645 12.29424321)', 4326),'SOUTH', FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('FRONT-002','PROP-002',ST_GeomFromText('LINESTRING(76.64171086 12.29428872, 76.64181281 12.29426768)', 4326),'SOUTH', FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('FRONT-003','PROP-003',ST_GeomFromText('LINESTRING(76.64160542 12.29430704, 76.64170850 12.29429155)', 4326),'SOUTH', FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('FRONT-004','PROP-004',ST_GeomFromText('LINESTRING(76.64153802 12.29430980, 76.64160231 12.29430932)', 4326),'SOUTH', FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('FRONT-005','PROP-005',ST_GeomFromText('LINESTRING(76.64148589 12.29430006, 76.64153528 12.29430067)', 4326),'SOUTH', FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('FRONT-006','PROP-006',ST_GeomFromText('LINESTRING(76.64141165 12.29431087, 76.64147960 12.29430618)', 4326),'SOUTH', FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('FRONT-007','PROP-007',ST_GeomFromText('LINESTRING(76.64133512 12.29431728, 76.64139965 12.29430436)', 4326),'SOUTH', FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('FRONT-008','PROP-008',ST_GeomFromText('LINESTRING(76.64126027 12.29434540, 76.64132848 12.29433259)', 4326),'SOUTH', FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('FRONT-009','PROP-009',ST_GeomFromText('LINESTRING(76.64116392 12.29434311, 76.64125187 12.29433850)', 4326),'SOUTH', FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('FRONT-010','PROP-010',ST_GeomFromText('LINESTRING(76.64105442 12.29435247, 76.64116123 12.29434435)', 4326),'SOUTH', FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('FRONT-011','PROP-011',ST_GeomFromText('LINESTRING(76.64103259 12.29440649, 76.64115821 12.29439771)', 4326),'NORTH', FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('FRONT-012','PROP-012',ST_GeomFromText('LINESTRING(76.64133924 12.29438456, 76.64141577 12.29437988)', 4326),'NORTH', FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('FRONT-013','PROP-013',ST_GeomFromText('LINESTRING(76.64142127 12.29438386, 76.64150544 12.29437206)', 4326),'NORTH', FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('FRONT-014','PROP-014',ST_GeomFromText('LINESTRING(76.64151849 12.29436596, 76.64163684 12.29434543)', 4326),'NORTH', FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('FRONT-015','PROP-015',ST_GeomFromText('LINESTRING(76.64163960 12.29434507, 76.64175850 12.29432791)', 4326),'NORTH', FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('FRONT-016','PROP-016',ST_GeomFromText('LINESTRING(76.64176959 12.29432717, 76.64189441 12.29431063)', 4326),'NORTH', FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY');

-- 2c. Provisional service zones (auto-generated, non-overlapping)
INSERT INTO property_service_zones (zone_id, property_id, geometry, version, verified, source) VALUES
  ('SZ-001','PROP-001',ST_GeomFromText('POLYGON((76.64181351 12.29425798, 76.64192451 12.29423438, 76.64193132 12.29426536, 76.64182032 12.29428896, 76.64181351 12.29425798))', 4326), 1, FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('SZ-002','PROP-002',ST_GeomFromText('POLYGON((76.64170898 12.29427988, 76.64181093 12.29425883, 76.64181751 12.29428968, 76.64171556 12.29431072, 76.64170898 12.29427988))', 4326), 1, FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('SZ-003','PROP-003',ST_GeomFromText('POLYGON((76.64160404 12.29429810, 76.64170711 12.29428261, 76.64171111 12.29430834, 76.64160803 12.29432383, 76.64160404 12.29429810))', 4326), 1, FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('SZ-004','PROP-004',ST_GeomFromText('POLYGON((76.64153795 12.29430076, 76.64160224 12.29430028, 76.64160245 12.29432766, 76.64153816 12.29432814, 76.64153795 12.29430076))', 4326), 1, FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('SZ-005','PROP-005',ST_GeomFromText('POLYGON((76.64148600 12.29429102, 76.64153539 12.29429163, 76.64153499 12.29432313, 76.64148560 12.29432251, 76.64148600 12.29429102))', 4326), 1, FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('SZ-006','PROP-006',ST_GeomFromText('POLYGON((76.64141100 12.29430185, 76.64147895 12.29429716, 76.64148161 12.29433441, 76.64141366 12.29433910, 76.64141100 12.29430185))', 4326), 1, FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('SZ-007','PROP-007',ST_GeomFromText('POLYGON((76.64133328 12.29430843, 76.64139781 12.29429550, 76.64140562 12.29433320, 76.64134109 12.29434612, 76.64133328 12.29430843))', 4326), 1, FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('SZ-008','PROP-008',ST_GeomFromText('POLYGON((76.64125854 12.29433652, 76.64132675 12.29432371, 76.64133286 12.29435514, 76.64126465 12.29436796, 76.64125854 12.29433652))', 4326), 1, FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('SZ-009','PROP-009',ST_GeomFromText('POLYGON((76.64116343 12.29433408, 76.64125138 12.29432948, 76.64125312 12.29436161, 76.64116517 12.29436622, 76.64116343 12.29433408))', 4326), 1, FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('SZ-010','PROP-010',ST_GeomFromText('POLYGON((76.64105371 12.29434346, 76.64116052 12.29433534, 76.64116293 12.29436606, 76.64105612 12.29437418, 76.64105371 12.29434346))', 4326), 1, FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('SZ-011','PROP-011',ST_GeomFromText('POLYGON((76.64103101 12.29438469, 76.64115664 12.29437591, 76.64115886 12.29440673, 76.64103324 12.29441551, 76.64103101 12.29438469))', 4326), 1, FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('SZ-012','PROP-012',ST_GeomFromText('POLYGON((76.64133746 12.29435649, 76.64141399 12.29435180, 76.64141634 12.29438890, 76.64133981 12.29439359, 76.64133746 12.29435649))', 4326), 1, FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('SZ-013','PROP-013',ST_GeomFromText('POLYGON((76.64141681 12.29435315, 76.64150099 12.29434135, 76.64150674 12.29438101, 76.64142256 12.29439281, 76.64141681 12.29435315))', 4326), 1, FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('SZ-014','PROP-014',ST_GeomFromText('POLYGON((76.64151489 12.29434592, 76.64163324 12.29432539, 76.64163844 12.29435433, 76.64152008 12.29437486, 76.64151489 12.29434592))', 4326), 1, FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('SZ-015','PROP-015',ST_GeomFromText('POLYGON((76.64163710 12.29432837, 76.64175601 12.29431121, 76.64175984 12.29433685, 76.64164093 12.29435401, 76.64163710 12.29432837))', 4326), 1, FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY'),
  ('SZ-016','PROP-016',ST_GeomFromText('POLYGON((76.64176629 12.29430305, 76.64189110 12.29428651, 76.64189563 12.29431959, 76.64177082 12.29433613, 76.64176629 12.29430305))', 4326), 1, FALSE, 'FIELD_SURVEY_PLUS_AUTO_GEOMETRY');

-- ---------------------------------------------------------------------
-- 3. Link the 16 frontage photos
--    __PHOTO_DIR__ is substituted by scripts/load_real_lane.sh
-- ---------------------------------------------------------------------
INSERT INTO property_photos (photo_id, property_id, photo_type, file_path, verified, notes) VALUES
  ('PHOTO-001','PROP-001','FRONTAGE','__PHOTO_DIR__/PROP-001.jpg', FALSE, 'Survey QA / human verification only - not used for live recognition.'),
  ('PHOTO-002','PROP-002','FRONTAGE','__PHOTO_DIR__/PROP-002.jpg', FALSE, 'Survey QA / human verification only - not used for live recognition.'),
  ('PHOTO-003','PROP-003','FRONTAGE','__PHOTO_DIR__/PROP-003.jpg', FALSE, 'Survey QA / human verification only - not used for live recognition.'),
  ('PHOTO-004','PROP-004','FRONTAGE','__PHOTO_DIR__/PROP-004.jpg', FALSE, 'Survey QA / human verification only - not used for live recognition.'),
  ('PHOTO-005','PROP-005','FRONTAGE','__PHOTO_DIR__/PROP-005.jpg', FALSE, 'Survey QA / human verification only - not used for live recognition.'),
  ('PHOTO-006','PROP-006','FRONTAGE','__PHOTO_DIR__/PROP-006.jpg', FALSE, 'Survey QA / human verification only - not used for live recognition.'),
  ('PHOTO-007','PROP-007','FRONTAGE','__PHOTO_DIR__/PROP-007.jpg', FALSE, 'Survey QA / human verification only - not used for live recognition.'),
  ('PHOTO-008','PROP-008','FRONTAGE','__PHOTO_DIR__/PROP-008.jpg', FALSE, 'Survey QA / human verification only - not used for live recognition.'),
  ('PHOTO-009','PROP-009','FRONTAGE','__PHOTO_DIR__/PROP-009.jpg', FALSE, 'Survey QA / human verification only - not used for live recognition.'),
  ('PHOTO-010','PROP-010','FRONTAGE','__PHOTO_DIR__/PROP-010.jpg', FALSE, 'Survey QA / human verification only - not used for live recognition.'),
  ('PHOTO-011','PROP-011','FRONTAGE','__PHOTO_DIR__/PROP-011.jpg', FALSE, 'Survey QA / human verification only - not used for live recognition.'),
  ('PHOTO-012','PROP-012','FRONTAGE','__PHOTO_DIR__/PROP-012.jpg', FALSE, 'Survey QA / human verification only - not used for live recognition.'),
  ('PHOTO-013','PROP-013','FRONTAGE','__PHOTO_DIR__/PROP-013.jpg', FALSE, 'Survey QA / human verification only - not used for live recognition.'),
  ('PHOTO-014','PROP-014','FRONTAGE','__PHOTO_DIR__/PROP-014.jpg', FALSE, 'Survey QA / human verification only - not used for live recognition.'),
  ('PHOTO-015','PROP-015','FRONTAGE','__PHOTO_DIR__/PROP-015.jpg', FALSE, 'Survey QA / human verification only - not used for live recognition.'),
  ('PHOTO-016','PROP-016','FRONTAGE','__PHOTO_DIR__/PROP-016.jpg', FALSE, 'Survey QA / human verification only - not used for live recognition.')
ON CONFLICT (photo_id) DO UPDATE SET
    property_id = EXCLUDED.property_id,
    file_path   = EXCLUDED.file_path,
    photo_type  = EXCLUDED.photo_type;

-- ---------------------------------------------------------------------
-- 4. In-transaction assertions - the whole thing rolls back on failure
-- ---------------------------------------------------------------------
DO $$
DECLARE n INT; bad TEXT;
BEGIN
  SELECT count(*) INTO n FROM properties;
  IF n <> 16 THEN RAISE EXCEPTION 'expected 16 properties, found %', n; END IF;
  SELECT count(*) INTO n FROM property_entrances;
  IF n <> 16 THEN RAISE EXCEPTION 'expected 16 entrances, found %', n; END IF;
  SELECT count(*) INTO n FROM property_frontages;
  IF n <> 16 THEN RAISE EXCEPTION 'expected 16 frontages, found %', n; END IF;
  SELECT count(*) INTO n FROM property_service_zones;
  IF n <> 16 THEN RAISE EXCEPTION 'expected 16 service zones, found %', n; END IF;
  SELECT count(*) INTO n FROM property_photos WHERE photo_type = 'FRONTAGE';
  IF n <> 16 THEN RAISE EXCEPTION 'expected 16 frontage photos, found %', n; END IF;

  SELECT count(*) INTO n FROM property_service_zones WHERE NOT ST_IsValid(geometry);
  IF n > 0 THEN RAISE EXCEPTION '% invalid service-zone polygons', n; END IF;

  SELECT count(*) INTO n FROM property_service_zones a
    JOIN property_service_zones b ON a.zone_id < b.zone_id
   WHERE ST_Overlaps(a.geometry, b.geometry) OR ST_Contains(a.geometry, b.geometry);
  IF n > 0 THEN RAISE EXCEPTION '% overlapping service-zone pairs', n; END IF;

  SELECT string_agg(e.property_id, ', ') INTO bad
    FROM property_entrances e JOIN property_service_zones z USING (property_id)
   WHERE NOT ST_Within(e.geometry, z.geometry);
  IF bad IS NOT NULL THEN
    RAISE EXCEPTION 'surveyed anchor lies outside its own service zone: %', bad;
  END IF;

  SELECT count(*) INTO n FROM (
      SELECT ST_SRID(geometry) s FROM property_service_zones
      UNION SELECT ST_SRID(geometry) FROM property_frontages
      UNION SELECT ST_SRID(geometry) FROM property_entrances) q
   WHERE q.s <> 4326;
  IF n > 0 THEN RAISE EXCEPTION 'geometry found with SRID <> 4326'; END IF;

  RAISE NOTICE 'Real lane loaded: 16 properties, 16 entrances, 16 frontages, 16 service zones, 16 frontage photos.';
END $$;

COMMIT;

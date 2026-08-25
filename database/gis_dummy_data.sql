-- =====================================================================
-- Wastraq demo: synthetic GIS data for 'Demo Lane'
-- GENERATED FILE - edit scripts/generate_gis_data.py and re-run.
--
-- Road centreline latitude : 12.97
-- Plot frontage width      : 12.0 m
-- Service zone depth       : 6.0 m
-- Zone spans 4.0 m to 10.0 m north of the road centreline
-- All geometry is SRID 4326 (WGS84 lon/lat).
-- =====================================================================

DELETE FROM property_service_zones;
DELETE FROM property_frontages;
DELETE FROM property_entrances;

-- Entrance points ------------------------------------------------------
INSERT INTO property_entrances (entrance_id, property_id, geometry, verified) VALUES
  ('ENT-001','PROP-001',ST_SetSRID(ST_MakePoint(77.5900553, 12.9700904), 4326), TRUE),
  ('ENT-002','PROP-002',ST_SetSRID(ST_MakePoint(77.5901659, 12.9700904), 4326), TRUE),
  ('ENT-003','PROP-003',ST_SetSRID(ST_MakePoint(77.5902765, 12.9700904), 4326), TRUE),
  ('ENT-004','PROP-004',ST_SetSRID(ST_MakePoint(77.5903872, 12.9700904), 4326), TRUE),
  ('ENT-005','PROP-005',ST_SetSRID(ST_MakePoint(77.5904978, 12.9700904), 4326), TRUE),
  ('ENT-006','PROP-006',ST_SetSRID(ST_MakePoint(77.5906084, 12.9700904), 4326), TRUE),
  ('ENT-007','PROP-007',ST_SetSRID(ST_MakePoint(77.5907190, 12.9700904), 4326), TRUE),
  ('ENT-008','PROP-008',ST_SetSRID(ST_MakePoint(77.5908296, 12.9700904), 4326), TRUE),
  ('ENT-009','PROP-009',ST_SetSRID(ST_MakePoint(77.5909403, 12.9700904), 4326), TRUE),
  ('ENT-010','PROP-010',ST_SetSRID(ST_MakePoint(77.5910509, 12.9700904), 4326), TRUE);

-- Frontage lines (road-facing edge, road is to the SOUTH) --------------
INSERT INTO property_frontages (frontage_id, property_id, geometry, road_side, verified) VALUES
  ('FRONT-001','PROP-001',ST_GeomFromText('LINESTRING(77.5900000 12.9700904, 77.5901106 12.9700904)', 4326), 'SOUTH', TRUE),
  ('FRONT-002','PROP-002',ST_GeomFromText('LINESTRING(77.5901106 12.9700904, 77.5902212 12.9700904)', 4326), 'SOUTH', TRUE),
  ('FRONT-003','PROP-003',ST_GeomFromText('LINESTRING(77.5902212 12.9700904, 77.5903319 12.9700904)', 4326), 'SOUTH', TRUE),
  ('FRONT-004','PROP-004',ST_GeomFromText('LINESTRING(77.5903319 12.9700904, 77.5904425 12.9700904)', 4326), 'SOUTH', TRUE),
  ('FRONT-005','PROP-005',ST_GeomFromText('LINESTRING(77.5904425 12.9700904, 77.5905531 12.9700904)', 4326), 'SOUTH', TRUE),
  ('FRONT-006','PROP-006',ST_GeomFromText('LINESTRING(77.5905531 12.9700904, 77.5906637 12.9700904)', 4326), 'SOUTH', TRUE),
  ('FRONT-007','PROP-007',ST_GeomFromText('LINESTRING(77.5906637 12.9700904, 77.5907743 12.9700904)', 4326), 'SOUTH', TRUE),
  ('FRONT-008','PROP-008',ST_GeomFromText('LINESTRING(77.5907743 12.9700904, 77.5908850 12.9700904)', 4326), 'SOUTH', TRUE),
  ('FRONT-009','PROP-009',ST_GeomFromText('LINESTRING(77.5908850 12.9700904, 77.5909956 12.9700904)', 4326), 'SOUTH', TRUE),
  ('FRONT-010','PROP-010',ST_GeomFromText('LINESTRING(77.5909956 12.9700904, 77.5911062 12.9700904)', 4326), 'SOUTH', TRUE);

-- Service-zone polygons (the association surface) ----------------------
INSERT INTO property_service_zones (zone_id, property_id, geometry, version, verified) VALUES
  ('SZ-001','PROP-001',ST_GeomFromText('POLYGON((77.5900000 12.9700362, 77.5901106 12.9700362, 77.5901106 12.9700904, 77.5900000 12.9700904, 77.5900000 12.9700362))', 4326), 1, TRUE),
  ('SZ-002','PROP-002',ST_GeomFromText('POLYGON((77.5901106 12.9700362, 77.5902212 12.9700362, 77.5902212 12.9700904, 77.5901106 12.9700904, 77.5901106 12.9700362))', 4326), 1, TRUE),
  ('SZ-003','PROP-003',ST_GeomFromText('POLYGON((77.5902212 12.9700362, 77.5903319 12.9700362, 77.5903319 12.9700904, 77.5902212 12.9700904, 77.5902212 12.9700362))', 4326), 1, TRUE),
  ('SZ-004','PROP-004',ST_GeomFromText('POLYGON((77.5903319 12.9700362, 77.5904425 12.9700362, 77.5904425 12.9700904, 77.5903319 12.9700904, 77.5903319 12.9700362))', 4326), 1, TRUE),
  ('SZ-005','PROP-005',ST_GeomFromText('POLYGON((77.5904425 12.9700362, 77.5905531 12.9700362, 77.5905531 12.9700904, 77.5904425 12.9700904, 77.5904425 12.9700362))', 4326), 1, TRUE),
  ('SZ-006','PROP-006',ST_GeomFromText('POLYGON((77.5905531 12.9700362, 77.5906637 12.9700362, 77.5906637 12.9700904, 77.5905531 12.9700904, 77.5905531 12.9700362))', 4326), 1, TRUE),
  ('SZ-007','PROP-007',ST_GeomFromText('POLYGON((77.5906637 12.9700362, 77.5907743 12.9700362, 77.5907743 12.9700904, 77.5906637 12.9700904, 77.5906637 12.9700362))', 4326), 1, TRUE),
  ('SZ-008','PROP-008',ST_GeomFromText('POLYGON((77.5907743 12.9700362, 77.5908850 12.9700362, 77.5908850 12.9700904, 77.5907743 12.9700904, 77.5907743 12.9700362))', 4326), 1, TRUE),
  ('SZ-009','PROP-009',ST_GeomFromText('POLYGON((77.5908850 12.9700362, 77.5909956 12.9700362, 77.5909956 12.9700904, 77.5908850 12.9700904, 77.5908850 12.9700362))', 4326), 1, TRUE),
  ('SZ-010','PROP-010',ST_GeomFromText('POLYGON((77.5909956 12.9700362, 77.5911062 12.9700362, 77.5911062 12.9700904, 77.5909956 12.9700904, 77.5909956 12.9700362))', 4326), 1, TRUE);

-- Keep properties.latitude/longitude consistent with generated geometry
-- (display only - never used for association).
UPDATE properties p SET latitude = v.lat, longitude = v.lon
FROM (VALUES
  ('PROP-001', 12.9701447, 77.5900553),
  ('PROP-002', 12.9701447, 77.5901659),
  ('PROP-003', 12.9701447, 77.5902765),
  ('PROP-004', 12.9701447, 77.5903872),
  ('PROP-005', 12.9701447, 77.5904978),
  ('PROP-006', 12.9701447, 77.5906084),
  ('PROP-007', 12.9701447, 77.5907190),
  ('PROP-008', 12.9701447, 77.5908296),
  ('PROP-009', 12.9701447, 77.5909403),
  ('PROP-010', 12.9701447, 77.5910509)
) AS v(pid, lat, lon) WHERE p.property_id = v.pid;

-- Sanity check: every property must have exactly one of each geometry.
DO $$
DECLARE n_missing INT;
BEGIN
  SELECT COUNT(*) INTO n_missing FROM properties p
  WHERE NOT EXISTS (SELECT 1 FROM property_entrances e WHERE e.property_id = p.property_id)
     OR NOT EXISTS (SELECT 1 FROM property_frontages f WHERE f.property_id = p.property_id)
     OR NOT EXISTS (SELECT 1 FROM property_service_zones z WHERE z.property_id = p.property_id);
  IF n_missing > 0 THEN
    RAISE EXCEPTION 'GIS seed incomplete: % properties missing geometry', n_missing;
  END IF;
  RAISE NOTICE 'GIS dummy data loaded: all properties have entrance, frontage and service zone.';
END $$;

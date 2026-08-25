-- =====================================================================
-- Wastraq demo seed: 10 dummy properties + 2 dummy pickers
-- All names / addresses / house numbers are fictitious.
-- Lane: "Demo Lane" - a synthetic one-lane street, properties on the
-- NORTH side of an east-west road.
-- Centroid lat/lon here are indicative only (map display), never used
-- for property association.
-- =====================================================================

TRUNCATE evidence, collection_events, property_service_zones,
         property_frontages, property_entrances, pickers, properties
         RESTART IDENTITY CASCADE;

INSERT INTO properties (
    property_id, authority_property_id, house_number, owner_name,
    formatted_address, property_type, route_id,
    latitude, longitude, mapping_confidence, verification_status
) VALUES
('PROP-001','ULB-DM-1001','12/1','Anitha Raman',
 '12/1, Demo Lane, Ward 42, Demo City 560001','RESIDENTIAL','ROUTE-A',
 12.9701447, 77.5900553, 0.980, 'FIELD_VERIFIED'),

('PROP-002','ULB-DM-1002','12/2','Suresh Kamath',
 '12/2, Demo Lane, Ward 42, Demo City 560001','RESIDENTIAL','ROUTE-A',
 12.9701447, 77.5901659, 0.975, 'FIELD_VERIFIED'),

('PROP-003','ULB-DM-1003','12/3','Fathima Beevi',
 '12/3, Demo Lane, Ward 42, Demo City 560001','RESIDENTIAL','ROUTE-A',
 12.9701447, 77.5902765, 0.990, 'FIELD_VERIFIED'),

('PROP-004','ULB-DM-1004','12/4','Ravi Deshpande',
 '12/4, Demo Lane, Ward 42, Demo City 560001','RESIDENTIAL','ROUTE-A',
 12.9701447, 77.5903872, 0.960, 'FIELD_VERIFIED'),

('PROP-005','ULB-DM-1005','12/5','Meera Nair',
 '12/5, Demo Lane, Ward 42, Demo City 560001','RESIDENTIAL','ROUTE-A',
 12.9701447, 77.5904978, 0.955, 'UNVERIFIED'),

('PROP-006','ULB-DM-1006','12/6','Joseph Fernandes',
 '12/6, Demo Lane, Ward 42, Demo City 560001','MIXED','ROUTE-A',
 12.9701447, 77.5906084, 0.940, 'UNVERIFIED'),

('PROP-007','ULB-DM-1007','12/7','Lakshmi Iyer',
 '12/7, Demo Lane, Ward 42, Demo City 560001','RESIDENTIAL','ROUTE-A',
 12.9701447, 77.5907190, 0.970, 'FIELD_VERIFIED'),

('PROP-008','ULB-DM-1008','12/8','Gurdeep Singh',
 '12/8, Demo Lane, Ward 42, Demo City 560001','COMMERCIAL','ROUTE-A',
 12.9701447, 77.5908297, 0.930, 'UNVERIFIED'),

('PROP-009','ULB-DM-1009','12/9','Priyanka Shetty',
 '12/9, Demo Lane, Ward 42, Demo City 560001','RESIDENTIAL','ROUTE-A',
 12.9701447, 77.5909403, 0.965, 'FIELD_VERIFIED'),

('PROP-010','ULB-DM-1010','12/10','Vikram Chauhan',
 '12/10, Demo Lane, Ward 42, Demo City 560001','INSTITUTIONAL','ROUTE-A',
 12.9701447, 77.5910509, 0.945, 'UNVERIFIED');


INSERT INTO pickers (picker_id, picker_name, rfid_uid, active) VALUES
('PICKER-01','Ganesh Bhat', 'RFID-DUMMY-A1B2C3D4', TRUE),
('PICKER-02','Shanti Devi', 'RFID-DUMMY-E5F6A7B8', TRUE);

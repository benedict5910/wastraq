#!/usr/bin/env python3
"""
Generate database/gis_dummy_data.sql for the Wastraq demo lane.

Synthetic geometry, but metrically correct: a straight east-west road with
10 adjacent properties on the north side. Each property gets

    entrance point  -> at the middle of its frontage
    frontage line   -> road-facing edge of the plot
    service zone    -> rectangle between the frontage and the kerb

Layout (looking down, north up):

    PROP-001  PROP-002  PROP-003  ...
    [ SZ-1 ]  [ SZ-2 ]  [ SZ-3 ]        <- service zones, adjacent, shared edges
    ------------- ROAD -------------    <- road centreline at ROAD_LAT

Run:  python3 scripts/generate_gis_data.py
"""

import math
import os

# --- lane parameters (metres) ----------------------------------------------
ROAD_LAT = 12.970000          # road centreline latitude
LON_START = 77.590000         # west end of the lane
N_PROPERTIES = 10

FRONTAGE_WIDTH_M = 12.0       # plot width along the road
ZONE_FAR_OFFSET_M = 10.0      # frontage line: distance north of road centreline
ZONE_NEAR_OFFSET_M = 4.0      # kerb edge of the service zone
BUILDING_OFFSET_M = 16.0      # indicative property centroid, north of centreline

# --- CRS helpers ------------------------------------------------------------
M_PER_DEG_LAT = 110574.0
M_PER_DEG_LON = 111320.0 * math.cos(math.radians(ROAD_LAT))


def dlat(metres: float) -> float:
    return metres / M_PER_DEG_LAT


def dlon(metres: float) -> float:
    return metres / M_PER_DEG_LON


LAT_FAR = ROAD_LAT + dlat(ZONE_FAR_OFFSET_M)
LAT_NEAR = ROAD_LAT + dlat(ZONE_NEAR_OFFSET_M)
LAT_BUILDING = ROAD_LAT + dlat(BUILDING_OFFSET_M)
W = dlon(FRONTAGE_WIDTH_M)


def f(x: float) -> str:
    return f"{x:.7f}"


def build():
    props = []
    for i in range(1, N_PROPERTIES + 1):
        pid = f"PROP-{i:03d}"
        lon_w = LON_START + (i - 1) * W
        lon_e = LON_START + i * W
        lon_mid = (lon_w + lon_e) / 2.0
        props.append(
            dict(
                property_id=pid,
                idx=i,
                lon_w=lon_w,
                lon_e=lon_e,
                lon_mid=lon_mid,
                entrance=(LAT_FAR, lon_mid),
                frontage=[(LAT_FAR, lon_w), (LAT_FAR, lon_e)],
                # polygon ring, CCW, closed
                zone=[
                    (LAT_NEAR, lon_w),
                    (LAT_NEAR, lon_e),
                    (LAT_FAR, lon_e),
                    (LAT_FAR, lon_w),
                    (LAT_NEAR, lon_w),
                ],
                centroid=(LAT_BUILDING, lon_mid),
            )
        )
    return props


def to_sql(props):
    out = []
    out.append("-- =====================================================================")
    out.append("-- Wastraq demo: synthetic GIS data for 'Demo Lane'")
    out.append("-- GENERATED FILE - edit scripts/generate_gis_data.py and re-run.")
    out.append("--")
    out.append(f"-- Road centreline latitude : {ROAD_LAT}")
    out.append(f"-- Plot frontage width      : {FRONTAGE_WIDTH_M} m")
    out.append(f"-- Service zone depth       : {ZONE_FAR_OFFSET_M - ZONE_NEAR_OFFSET_M} m")
    out.append(f"-- Zone spans {ZONE_NEAR_OFFSET_M} m to {ZONE_FAR_OFFSET_M} m north of the road centreline")
    out.append("-- All geometry is SRID 4326 (WGS84 lon/lat).")
    out.append("-- =====================================================================")
    out.append("")
    out.append("DELETE FROM property_service_zones;")
    out.append("DELETE FROM property_frontages;")
    out.append("DELETE FROM property_entrances;")
    out.append("")

    # entrances
    out.append("-- Entrance points ------------------------------------------------------")
    out.append("INSERT INTO property_entrances (entrance_id, property_id, geometry, verified) VALUES")
    rows = []
    for p in props:
        lat, lon = p["entrance"]
        rows.append(
            f"  ('ENT-{p['idx']:03d}','{p['property_id']}',"
            f"ST_SetSRID(ST_MakePoint({f(lon)}, {f(lat)}), 4326), TRUE)"
        )
    out.append(",\n".join(rows) + ";")
    out.append("")

    # frontages
    out.append("-- Frontage lines (road-facing edge, road is to the SOUTH) --------------")
    out.append("INSERT INTO property_frontages (frontage_id, property_id, geometry, road_side, verified) VALUES")
    rows = []
    for p in props:
        (la1, lo1), (la2, lo2) = p["frontage"]
        wkt = f"LINESTRING({f(lo1)} {f(la1)}, {f(lo2)} {f(la2)})"
        rows.append(
            f"  ('FRONT-{p['idx']:03d}','{p['property_id']}',"
            f"ST_GeomFromText('{wkt}', 4326), 'SOUTH', TRUE)"
        )
    out.append(",\n".join(rows) + ";")
    out.append("")

    # service zones
    out.append("-- Service-zone polygons (the association surface) ----------------------")
    out.append("INSERT INTO property_service_zones (zone_id, property_id, geometry, version, verified) VALUES")
    rows = []
    for p in props:
        ring = ", ".join(f"{f(lo)} {f(la)}" for la, lo in p["zone"])
        wkt = f"POLYGON(({ring}))"
        rows.append(
            f"  ('SZ-{p['idx']:03d}','{p['property_id']}',"
            f"ST_GeomFromText('{wkt}', 4326), 1, TRUE)"
        )
    out.append(",\n".join(rows) + ";")
    out.append("")

    # keep indicative centroids consistent with the generated geometry
    out.append("-- Keep properties.latitude/longitude consistent with generated geometry")
    out.append("-- (display only - never used for association).")
    rows = []
    for p in props:
        lat, lon = p["centroid"]
        rows.append(f"  ('{p['property_id']}', {f(lat)}, {f(lon)})")
    out.append("UPDATE properties p SET latitude = v.lat, longitude = v.lon")
    out.append("FROM (VALUES")
    out.append(",\n".join(rows))
    out.append(") AS v(pid, lat, lon) WHERE p.property_id = v.pid;")
    out.append("")
    out.append("-- Sanity check: every property must have exactly one of each geometry.")
    out.append("DO $$")
    out.append("DECLARE n_missing INT;")
    out.append("BEGIN")
    out.append("  SELECT COUNT(*) INTO n_missing FROM properties p")
    out.append("  WHERE NOT EXISTS (SELECT 1 FROM property_entrances e WHERE e.property_id = p.property_id)")
    out.append("     OR NOT EXISTS (SELECT 1 FROM property_frontages f WHERE f.property_id = p.property_id)")
    out.append("     OR NOT EXISTS (SELECT 1 FROM property_service_zones z WHERE z.property_id = p.property_id);")
    out.append("  IF n_missing > 0 THEN")
    out.append("    RAISE EXCEPTION 'GIS seed incomplete: % properties missing geometry', n_missing;")
    out.append("  END IF;")
    out.append("  RAISE NOTICE 'GIS dummy data loaded: all properties have entrance, frontage and service zone.';")
    out.append("END $$;")
    out.append("")
    return "\n".join(out)


def main():
    props = build()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "database", "gis_dummy_data.sql")
    with open(path, "w") as fh:
        fh.write(to_sql(props))
    print(f"wrote {path}")
    print(f"lane spans lon {f(props[0]['lon_w'])} .. {f(props[-1]['lon_e'])}")
    print(f"zones span lat {f(LAT_NEAR)} .. {f(LAT_FAR)}")


if __name__ == "__main__":
    main()

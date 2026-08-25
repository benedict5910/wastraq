#!/usr/bin/env python3
"""
Offline sanity check for the demo lane geometry.

Parses database/gis_dummy_data.sql, rebuilds the polygons in pure Python and
replays the same decision ladder gis.py uses - so the expected outcome of every
simulation waypoint can be checked without a database.

This does NOT replace the PostGIS run (scripts/verify_demo.sh); it catches the
class of bug where the seeded coordinates simply don't sit where the demo
narrative claims they do.

    python3 scripts/validate_geometry.py
"""

from __future__ import annotations

import math
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQL = os.path.join(HERE, "database", "gis_dummy_data.sql")

# Must mirror backend/app/config.py
SEARCH_RADIUS_M = 15.0
AUTO_MAX_DISTANCE_M = 3.0
AMBIGUITY_MARGIN_M = 2.0
MIN_AUTO_CONFIDENCE = 0.70

REF_LAT = 12.97
M_PER_DEG_LAT = 110574.0
M_PER_DEG_LON = 111320.0 * math.cos(math.radians(REF_LAT))


def to_m(lat, lon):
    """Local planar projection; exact enough (<1 mm) over a 100 m lane."""
    return (lon * M_PER_DEG_LON, lat * M_PER_DEG_LAT)


def parse_polygons(path: str) -> list[tuple[str, str, list[tuple[float, float]]]]:
    text = open(path).read()
    block = text.split("INSERT INTO property_service_zones")[1]
    out = []
    pattern = re.compile(
        r"\('(SZ-\d+)','(PROP-\d+)',ST_GeomFromText\('POLYGON\(\((.*?)\)\)', 4326\)"
    )
    for zone_id, prop_id, ring in pattern.findall(block):
        pts = []
        for pair in ring.split(","):
            lon_s, lat_s = pair.strip().split()
            pts.append((float(lat_s), float(lon_s)))
        out.append((zone_id, prop_id, pts))
    return out


def point_in_ring(lat, lon, ring) -> bool:
    x, y = to_m(lat, lon)
    pts = [to_m(la, lo) for la, lo in ring]
    inside = False
    for i in range(len(pts) - 1):
        (x1, y1), (x2, y2) = pts[i], pts[i + 1]
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xin:
                inside = not inside
    return inside


def dist_to_segment(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def dist_to_ring_m(lat, lon, ring) -> float:
    x, y = to_m(lat, lon)
    pts = [to_m(la, lo) for la, lo in ring]
    return min(
        dist_to_segment(x, y, *pts[i], *pts[i + 1]) for i in range(len(pts) - 1)
    )


def decide(lat, lon, zones):
    inside = [(z, p, r) for z, p, r in zones if point_in_ring(lat, lon, r)]
    if len(inside) == 1:
        z, p, r = inside[0]
        margin = dist_to_ring_m(lat, lon, r)
        conf = round(min(0.95 + min(margin / 20.0, 0.04), 0.99), 3)
        return dict(decision="AUTO_ASSOCIATED", property_id=p, confidence=conf,
                    d1=0.0, sep=None, n=1)
    if len(inside) > 1:
        return dict(decision="AMBIGUOUS", property_id=None, confidence=0.0,
                    d1=0.0, sep=0.0, n=len(inside))

    near = sorted(
        ((dist_to_ring_m(lat, lon, r), p, z) for z, p, r in zones),
        key=lambda t: t[0],
    )
    near = [n for n in near if n[0] <= SEARCH_RADIUS_M]
    if not near:
        return dict(decision="NO_MATCH", property_id=None, confidence=0.0,
                    d1=None, sep=None, n=0)

    d1 = near[0][0]
    d2 = near[1][0] if len(near) > 1 else None
    sep = (d2 - d1) if d2 is not None else float("inf")
    prox = max(0.0, 0.90 - (d1 / AUTO_MAX_DISTANCE_M) * 0.15)
    sepf = min(sep / AMBIGUITY_MARGIN_M, 1.0) if sep != float("inf") else 1.0
    conf = round(prox * (0.6 + 0.4 * sepf), 3)

    if d1 > AUTO_MAX_DISTANCE_M or sep < AMBIGUITY_MARGIN_M or conf < MIN_AUTO_CONFIDENCE:
        return dict(decision="AMBIGUOUS", property_id=None, confidence=conf,
                    d1=d1, sep=sep, n=len(near))
    return dict(decision="AUTO_ASSOCIATED", property_id=near[0][1], confidence=conf,
                d1=d1, sep=sep, n=len(near))


# waypoint, expected decision, expected property (None = don't care / no match)
EXPECTATIONS = [
    ("VEHICLE",  12.9698800, 77.5900500, "NO_MATCH",        None),
    ("APPROACH", 12.9699800, 77.5900500, "AMBIGUOUS",       None),
    ("KERB",     12.9700100, 77.5901700, "AUTO_ASSOCIATED", "PROP-002"),
    ("ZONE",     12.9700600, 77.5902800, "AUTO_ASSOCIATED", "PROP-003"),
    ("ZONE",     12.9700650, 77.5903900, "AUTO_ASSOCIATED", "PROP-004"),
    ("BOUNDARY", 12.9700250, 77.5904425, "AMBIGUOUS",       None),
    ("RETURN",   12.9698800, 77.5906000, "NO_MATCH",        None),
]


def main() -> int:
    zones = parse_polygons(SQL)
    if len(zones) != 10:
        print(f"FAIL: expected 10 service zones, parsed {len(zones)}")
        return 1
    print(f"Parsed {len(zones)} service-zone polygons from gis_dummy_data.sql\n")

    # geometry sanity: zones must not overlap
    overlaps = 0
    for i, (zi, pi, ri) in enumerate(zones):
        for (zj, pj, rj) in zones[i + 1:]:
            # sample the interior of zone i and test containment in zone j
            cy = sum(p[0] for p in ri[:-1]) / (len(ri) - 1)
            cx = sum(p[1] for p in ri[:-1]) / (len(ri) - 1)
            if point_in_ring(cy, cx, rj):
                print(f"FAIL: centroid of {zi} falls inside {zj}")
                overlaps += 1
    print(f"Overlap check: {overlaps} overlapping pair(s)\n")

    print(f"{'WAYPOINT':<9} {'DECISION':<16} {'PROPERTY':<9} {'CONF':>6} {'d1(m)':>7} {'sep(m)':>7}  RESULT")
    failures = 0
    for label, lat, lon, exp_dec, exp_prop in EXPECTATIONS:
        r = decide(lat, lon, zones)
        ok = r["decision"] == exp_dec and (exp_prop is None or r["property_id"] == exp_prop)
        failures += 0 if ok else 1
        d1 = "-" if r["d1"] is None else f"{r['d1']:.2f}"
        sep = "-" if r["sep"] in (None, float("inf")) else f"{r['sep']:.2f}"
        print(
            f"{label:<9} {r['decision']:<16} {str(r['property_id'] or '-'):<9} "
            f"{r['confidence']:>6} {d1:>7} {sep:>7}  {'OK' if ok else 'FAIL exp ' + exp_dec + ' ' + str(exp_prop)}"
        )

    print()
    total = failures + overlaps
    print("ALL CHECKS PASSED" if total == 0 else f"{total} CHECK(S) FAILED")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())

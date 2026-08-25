#!/usr/bin/env python3
"""
Offline test of the association ladder against the REAL 16-property lane.

Runs the actual `backend/app/gis.py` decision code, but replaces PostGIS with
the same UTM-projected geometry that generated the lane - so it needs neither a
database nor FastAPI installed. Distances agree with PostGIS `::geography` to
well under a centimetre at this scale.

Covers the five required lookup cases:
  1. a coordinate inside a service zone returns that property
  2. a coordinate between two zones is AMBIGUOUS
  3. a coordinate off the lane is NO_MATCH
  4. candidates come back ordered nearest-first and are plausible
  5. an ambiguous result never carries a property_id

...and then replays every waypoint of simulation/track_real_lane.json.

    python3 scripts/test_real_lane_lookup.py
"""

from __future__ import annotations

import json
import os
import sys
import types

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, HERE)

# --- stub the DB / env dependencies so app.gis imports standalone ------------
for name, attrs in [
    ("psycopg", {"Connection": object}),
    ("psycopg.rows", {"dict_row": None}),
    ("psycopg_pool", {"ConnectionPool": object}),
    ("dotenv", {"load_dotenv": lambda *a, **k: None}),
]:
    if name not in sys.modules:
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
sys.modules["psycopg"].rows = sys.modules["psycopg.rows"]

from app import gis  # noqa: E402

import generate_real_lane as G  # noqa: E402
from utm import to_utm  # noqa: E402

PROPS, QUADS, OUT, META, XY, ZONE, FRAME = G.build()
ZONE_OF = {pid: f"SZ-{int(pid.split('-')[1]):03d}" for pid in OUT}


# ---------------------------------------------------------------------------
# geometry helpers (metres, EPSG:32643)
# ---------------------------------------------------------------------------
def _seg_dist(p, a, b):
    ab = b - a
    denom = float(ab @ ab)
    if denom < 1e-15:
        return float(np.linalg.norm(p - a))
    t = max(0.0, min(1.0, float((p - a) @ ab) / denom))
    return float(np.linalg.norm(p - (a + t * ab)))


def _inside(p, poly):
    for i in range(len(poly)):
        e = poly[(i + 1) % len(poly)] - poly[i]
        if G.cross2(e, p - poly[i]) < 0:
            return False
    return True


def _boundary_dist(p, poly):
    return min(_seg_dist(p, poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly)))


def _zone_dist(p, poly):
    return 0.0 if _inside(p, poly) else _boundary_dist(p, poly)


def fake_fetch_all(sql: str, params: dict):
    p = np.array(to_utm(params["lat"], params["lon"], ZONE))
    if "ST_Within" in sql:
        return [
            {"property_id": pid, "zone_id": ZONE_OF[pid], "distance_m": 0.0, "inside": True,
             "margin_m": _boundary_dist(p, QUADS[pid]),
             "entrance_distance_m": float(np.linalg.norm(p - PROPS[pid]["anchor"]))}
            for pid in sorted(QUADS) if _inside(p, QUADS[pid])
        ]
    rows = sorted(
        ({"property_id": pid, "zone_id": ZONE_OF[pid],
          "distance_m": _zone_dist(p, QUADS[pid]), "inside": False, "margin_m": None,
          "entrance_distance_m": float(np.linalg.norm(p - PROPS[pid]["anchor"]))}
         for pid in QUADS),
        key=lambda r: r["distance_m"],
    )
    return [r for r in rows if r["distance_m"] <= params["radius_m"]][:10]


gis.fetch_all = fake_fetch_all


# ---------------------------------------------------------------------------
FAILS: list[str] = []


def check(cond, label, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(label)


def show(tag, r):
    cands = ", ".join(
        f"{c['property_id']}@{c['distance_m']:.2f}m" + ("*" if c["inside"] else "")
        for c in r["candidates"][:4]
    ) or "none"
    print(f"        {tag}: {r['decision']} / {r['property_id'] or '-'} "
          f"conf={r['confidence']} [{cands}]")


def main() -> int:
    env = {}
    env_path = os.path.join(HERE, "real_lane_testpoints.env")
    if os.path.exists(env_path):
        for line in open(env_path):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                env[k] = v

    print(f"Real lane: {len(OUT)} properties, EPSG:{META['epsg']}, "
          f"{META['lane_length_m']} m long")
    print(f"  south: {', '.join(META['south'])}")
    print(f"  north: {', '.join(META['north'])}\n")

    print("1. inside a service zone -> that property")
    pid = env.get("INSIDE_PROPERTY", "PROP-003")
    r = gis.lookup_property(float(env["INSIDE_LAT"]), float(env["INSIDE_LON"]))
    show(pid, r)
    check(r["decision"] == "AUTO_ASSOCIATED" and r["property_id"] == pid,
          f"inside {pid} -> AUTO_ASSOCIATED {pid}")
    check(r["confidence"] >= 0.90, "containment confidence >= 0.90", str(r["confidence"]))

    print("\n2. between two adjacent zones -> AMBIGUOUS")
    r2 = gis.lookup_property(float(env["AMBIG_LAT"]), float(env["AMBIG_LON"]))
    show(f"{env.get('AMBIG_A')}|{env.get('AMBIG_B')}", r2)
    check(r2["decision"] == "AMBIGUOUS", "boundary point -> AMBIGUOUS", r2["reason"])
    top2 = {c["property_id"] for c in r2["candidates"][:2]}
    check({env.get("AMBIG_A"), env.get("AMBIG_B")} <= top2,
          "both neighbours are the top-2 candidates", str(sorted(top2)))

    print("\n3. off the lane -> NO_MATCH")
    r3 = gis.lookup_property(float(env["FAR_LAT"]), float(env["FAR_LON"]))
    show("far", r3)
    check(r3["decision"] == "NO_MATCH", "point far from the lane -> NO_MATCH")
    check(r3["candidates"] == [], "no candidates returned")

    print("\n4. candidate ordering is nearest-first and plausible")
    r4 = gis.lookup_property(float(env["ROAD_LAT"]), float(env["ROAD_LON"]))
    show("mid-carriageway", r4)
    d = [c["distance_m"] for c in r4["candidates"]]
    check(d == sorted(d), "candidates are sorted nearest-first", str(d[:4]))
    check(len(r4["candidates"]) >= 2, "several plausible candidates offered", str(len(d)))
    check(all(c["distance_m"] <= 15.0 for c in r4["candidates"]),
          "every candidate is inside the search radius")

    print("\n5. an ambiguous result never carries a property_id")
    for label, rr in (("boundary", r2), ("mid-carriageway", r4)):
        if rr["decision"] != "AUTO_ASSOCIATED":
            check(rr["property_id"] is None, f"{label}: {rr['decision']} -> property_id is null")
    forced = []
    for pid_, p in PROPS.items():                     # sweep every inter-zone gap
        if not p["next"]:
            continue
        mid = (p["anchor"] + PROPS[p["next"]]["anchor"]) / 2.0
        from utm import from_utm
        la, lo = from_utm(float(mid[0]), float(mid[1]), ZONE)
        rr = gis.lookup_property(la, lo)
        if rr["decision"] == "AUTO_ASSOCIATED":
            near = sorted(c["distance_m"] for c in rr["candidates"])
            if len(near) > 1 and (near[1] - near[0]) < 2.0:
                forced.append((pid_, rr["property_id"], round(near[1] - near[0], 2)))
    check(not forced, "no property is forced when two candidates are within 2 m",
          str(forced))

    print("\n6. every waypoint of the generated picker track")
    track = json.load(open(os.path.join(ROOT, "simulation", "track_real_lane.json")))
    events = 0
    for i, wp in enumerate(track["waypoints"], 1):
        rr = gis.lookup_property(wp["latitude"], wp["longitude"])
        collect = rr["decision"] == "AUTO_ASSOCIATED" and rr["confidence"] >= 0.90
        events += 1 if collect else 0
        print(f"  {i:>2} {wp['label']:<9} {rr['decision']:<16} "
              f"{str(rr['property_id'] or '-'):<9} conf={rr['confidence']:<6} "
              f"{'-> COLLECT' if collect else ''}")
        if wp["label"] == "ZONE":
            want = wp["note"].split("of ")[1].split(" ")[0]
            check(rr["property_id"] == want, f"  waypoint {i} associates {want}",
                  f"got {rr['property_id']}")
        if wp["label"] in ("VEHICLE", "RETURN"):
            check(rr["decision"] == "NO_MATCH", f"  waypoint {i} ({wp['label']}) -> NO_MATCH")
        if wp["label"] in ("GAP", "CROSSING"):
            check(rr["decision"] == "AMBIGUOUS", f"  waypoint {i} ({wp['label']}) -> AMBIGUOUS",
                  rr["reason"])
        if wp["label"] == "APPROACH":
            # understood, but deliberately under the 0.90 collection threshold
            check(rr["decision"] == "AUTO_ASSOCIATED" and not collect,
                  f"  waypoint {i} (APPROACH) -> located but below collect threshold",
                  f"{rr['decision']} conf={rr['confidence']}")
    check(events >= 4, f"track produces {events} collectable stops (>= 4)")

    print()
    print("ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} CHECK(S) FAILED")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

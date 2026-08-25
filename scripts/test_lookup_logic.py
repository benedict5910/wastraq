#!/usr/bin/env python3
"""
Offline unit test for the decision ladder in backend/app/gis.py.

PostGIS is replaced by the pure-Python geometry in validate_geometry.py, so
this runs with no database and no FastAPI install. It tests the part that is
easy to get wrong - the AUTO_ASSOCIATED / AMBIGUOUS / NO_MATCH thresholds -
against the same waypoints the simulation uses.

    python3 scripts/test_lookup_logic.py
"""

from __future__ import annotations

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, HERE)

# --- stub the DB / env dependencies so app.gis imports standalone ------------
psycopg = types.ModuleType("psycopg")
psycopg.Connection = object
rows_mod = types.ModuleType("psycopg.rows")
rows_mod.dict_row = None
psycopg.rows = rows_mod
pool_mod = types.ModuleType("psycopg_pool")
pool_mod.ConnectionPool = object
dotenv_mod = types.ModuleType("dotenv")
dotenv_mod.load_dotenv = lambda *a, **k: None
sys.modules.setdefault("psycopg", psycopg)
sys.modules.setdefault("psycopg.rows", rows_mod)
sys.modules.setdefault("psycopg_pool", pool_mod)
sys.modules.setdefault("dotenv", dotenv_mod)

from app import gis  # noqa: E402
from validate_geometry import (  # noqa: E402
    EXPECTATIONS,
    SQL,
    dist_to_ring_m,
    parse_polygons,
    point_in_ring,
)

ZONES = parse_polygons(SQL)


def fake_fetch_all(sql: str, params: dict):
    """Stand in for PostGIS: same rows, computed in Python."""
    lat, lon = params["lat"], params["lon"]
    if "ST_Within" in sql:
        return [
            {
                "property_id": p,
                "zone_id": z,
                "distance_m": 0.0,
                "inside": True,
                "margin_m": dist_to_ring_m(lat, lon, r),
                "entrance_distance_m": 5.0,
            }
            for z, p, r in ZONES
            if point_in_ring(lat, lon, r)
        ]
    radius = params["radius_m"]
    near = sorted(
        (
            {
                "property_id": p,
                "zone_id": z,
                "distance_m": dist_to_ring_m(lat, lon, r),
                "inside": False,
                "margin_m": None,
                "entrance_distance_m": 5.0,
            }
            for z, p, r in ZONES
        ),
        key=lambda d: d["distance_m"],
    )
    return [n for n in near if n["distance_m"] <= radius][:10]


gis.fetch_all = fake_fetch_all


def main() -> int:
    failures = 0
    print(f"{'WAYPOINT':<9} {'DECISION':<16} {'PROPERTY':<9} {'CONF':>6}  RESULT")
    for label, lat, lon, exp_decision, exp_property in EXPECTATIONS:
        r = gis.lookup_property(lat, lon)
        ok = r["decision"] == exp_decision and (
            exp_property is None or r["property_id"] == exp_property
        )
        failures += 0 if ok else 1
        print(
            f"{label:<9} {r['decision']:<16} {str(r['property_id'] or '-'):<9} "
            f"{r['confidence']:>6}  {'OK' if ok else 'FAIL expected ' + exp_decision}"
        )

    # structural checks on the response contract
    r = gis.lookup_property(12.9700600, 77.5902800)
    for key in ("property_id", "decision", "confidence", "method", "reason", "query", "candidates"):
        if key not in r:
            print(f"FAIL: response missing key {key!r}")
            failures += 1
    if r["candidates"] and set(r["candidates"][0]) != {
        "property_id", "zone_id", "distance_m", "inside", "entrance_distance_m"
    }:
        print("FAIL: candidate shape does not match schemas.CandidateOut")
        failures += 1

    # an AMBIGUOUS or NO_MATCH result must never carry a property_id
    for lat, lon in [(12.9700250, 77.5904425), (12.9698800, 77.5900500), (12.0, 77.0)]:
        r = gis.lookup_property(lat, lon)
        if r["decision"] != "AUTO_ASSOCIATED" and r["property_id"] is not None:
            print(f"FAIL: {r['decision']} returned property_id {r['property_id']}")
            failures += 1

    print()
    print("ALL CHECKS PASSED" if failures == 0 else f"{failures} CHECK(S) FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

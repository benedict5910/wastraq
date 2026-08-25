#!/usr/bin/env python3
"""
Build the real 16-property Wastraq lane from surveyed entrance coordinates.

Input  : the 16 on-site (latitude, longitude) anchors below.
Output : database/real_lane_16.sql         - transactional upsert of everything
         simulation/track_real_lane.json   - picker walk along the real lane
         scripts/real_lane_testpoints.env  - coordinates verify_demo.sh asserts on

Method
------
1. Project every anchor to **EPSG:32643 (WGS84 / UTM 43N)** so all construction
   is in real metres. Nothing is ever computed on raw degrees.
2. Fit the road axis by PCA over all 16 anchors. The sign of each anchor's
   offset along the axis normal decides which SIDE of the road it is on -
   geometry decides this, not the property number.
3. Order each side along the axis and give every property a local direction
   taken from its same-side neighbours, so a bend in the road is followed
   rather than flattened.
4. Frontage  = a segment through the anchor along that local direction,
   ending halfway to each same-side neighbour (capped, and shrunk by half the
   inter-zone gap so neighbours never touch).
5. Service zone = the quad swept from that frontage 1 m back into the plot and
   `f` metres out toward the road, where `f` stops short of the crown of the
   road so the two sides can never meet. No circular buffers anywhere.
6. Every quad is checked against every other with a separating-axis test; any
   pair that still touches is shrunk until it doesn't.
7. Convert back to EPSG:4326 for storage.

The result is a provisional first approximation for the demo - good enough to
associate pickers with properties, explicitly NOT cadastral truth. Everything
generated is written with source = FIELD_SURVEY_PLUS_AUTO_GEOMETRY and
verified = false so it is obvious in QGIS what still needs a human eye.

    python3 scripts/generate_real_lane.py            # write the files
    python3 scripts/generate_real_lane.py --check    # validate only, write nothing
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

from utm import epsg_for, from_utm, selftest, to_utm, zone_for_lon  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --------------------------------------------------------------------------
# Surveyed anchors: property_id, latitude, longitude  (order as collected)
# --------------------------------------------------------------------------
SURVEY = [
    ("PROP-001", 12.2942563, 76.6418649),
    ("PROP-002", 12.2942779, 76.6417633),
    ("PROP-003", 12.2942995, 76.6416556),
    ("PROP-004", 12.2943097, 76.6415517),
    ("PROP-005", 12.2943005, 76.6415215),
    ("PROP-006", 12.2943084, 76.6414474),
    ("PROP-007", 12.2943113, 76.6413650),
    ("PROP-008", 12.2943382, 76.6412986),
    ("PROP-009", 12.2943408, 76.6412080),
    ("PROP-010", 12.2943477, 76.6411172),
    ("PROP-011", 12.2944021, 76.6410954),
    ("PROP-012", 12.2943811, 76.6413958),
    ("PROP-013", 12.2943808, 76.6414431),
    ("PROP-014", 12.2943552, 76.6415805),
    ("PROP-015", 12.2943369, 76.6416962),
    ("PROP-016", 12.2943189, 76.6418320),
]

ADDRESS = "2nd Cross Road, Krishnamurthy Puram, Mysuru, Karnataka 570014"
ROUTE_ID = "ROUTE-DEMO-01"
GEOM_SOURCE = "FIELD_SURVEY_PLUS_AUTO_GEOMETRY"
PHOTO_DIR_TOKEN = "__PHOTO_DIR__"   # replaced by load_real_lane.sh at load time

# --- geometry tuning (metres) ---------------------------------------------
HALF_WIDTH_MIN = 1.5      # a frontage is never shorter than 2 x this
HALF_WIDTH_MAX = 7.0      # ...nor longer, even next to a big gap
ZONE_GAP = 0.30           # clear space left between neighbouring zones
ZONE_BACK = 1.0           # zone extends this far behind the frontage
ROAD_CROWN_CLEAR = 0.50   # zone stops this far short of the road centreline
ZONE_DEPTH_MIN = 1.0
ZONE_DEPTH_MAX = 4.0


# ==========================================================================
# small vector helpers
# ==========================================================================
def unit(v):
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-12 else np.array([1.0, 0.0])


def perp(v):
    return np.array([-v[1], v[0]])


def cross2(a, b) -> float:
    """2-D scalar cross product (np.cross on 2-vectors is deprecated)."""
    return float(a[0] * b[1] - a[1] * b[0])


def quads_overlap(p, q, eps: float = 1e-9) -> bool:
    """Separating-axis test for two convex polygons (our zones are quads)."""
    for poly in (p, q):
        n = len(poly)
        for i in range(n):
            edge = poly[(i + 1) % n] - poly[i]
            ax = perp(edge)
            nrm = float(np.linalg.norm(ax))
            if nrm < 1e-12:
                continue
            ax = ax / nrm
            pp, qq = p @ ax, q @ ax
            if pp.max() <= qq.min() + eps or qq.max() <= pp.min() + eps:
                return False
    return True


def poly_area(poly) -> float:
    x, y = poly[:, 0], poly[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def ensure_ccw(poly):
    x, y = poly[:, 0], poly[:, 1]
    signed = 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))
    return poly if signed > 0 else poly[::-1]


# ==========================================================================
# the build
# ==========================================================================
def build():
    zone = zone_for_lon(SURVEY[0][2])
    epsg = epsg_for(SURVEY[0][1], SURVEY[0][2])

    ids = [p[0] for p in SURVEY]
    lla = np.array([[p[1], p[2]] for p in SURVEY])
    xy = np.array([to_utm(lat, lon, zone) for lat, lon in lla])

    centre = xy.mean(axis=0)
    rel = xy - centre

    # --- 2. road axis by PCA ---------------------------------------------
    _, sv, vt = np.linalg.svd(rel, full_matrices=False)
    axis = vt[0]
    if axis[0] < 0:                       # point the axis east-ish for stability
        axis = -axis
    nrm = perp(axis)
    t = rel @ axis                        # along the road
    d = rel @ nrm                         # across the road (signed)

    side = np.where(d > 0, "NORTH", "SOUTH")
    bearing = math.degrees(math.atan2(axis[0], axis[1])) % 360.0

    groups = {s: [i for i in np.argsort(t) if side[i] == s] for s in ("NORTH", "SOUTH")}

    props = {}
    for s, idxs in groups.items():
        for pos, i in enumerate(idxs):
            prev_i = idxs[pos - 1] if pos > 0 else None
            next_i = idxs[pos + 1] if pos < len(idxs) - 1 else None

            # --- 3. local along-road direction from same-side neighbours ---
            if prev_i is not None and next_i is not None:
                u = unit(xy[next_i] - xy[prev_i])
            elif next_i is not None:
                u = unit(xy[next_i] - xy[i])
            elif prev_i is not None:
                u = unit(xy[i] - xy[prev_i])
            else:
                u = axis.copy()

            # roadward normal: perpendicular to u, pointing at the other side
            n = perp(u)
            roadward = -1.0 if s == "NORTH" else 1.0      # toward the axis
            if float(np.dot(n, nrm)) * roadward < 0:
                n = -n

            # --- 4. frontage half-widths: midpoints to same-side neighbours -
            def half(j):
                if j is None:
                    return HALF_WIDTH_MAX
                return abs(float(np.dot(xy[j] - xy[i], u))) / 2.0

            a = min(max(half(prev_i), HALF_WIDTH_MIN), HALF_WIDTH_MAX)
            b = min(max(half(next_i), HALF_WIDTH_MIN), HALF_WIDTH_MAX)
            a = max(a - ZONE_GAP / 2, HALF_WIDTH_MIN * 0.5)
            b = max(b - ZONE_GAP / 2, HALF_WIDTH_MIN * 0.5)

            # --- 5. zone depth: stop short of the crown of the road ---------
            opp = groups["SOUTH" if s == "NORTH" else "NORTH"]
            if opp:
                # the anchor most directly across from this one
                j = min(opp, key=lambda k: abs(float(np.dot(xy[k] - xy[i], u))))
                across = abs(float(np.dot(xy[j] - xy[i], n)))
            else:
                across = 2 * ZONE_DEPTH_MAX
            f = min(max(across / 2.0 - ROAD_CROWN_CLEAR, ZONE_DEPTH_MIN), ZONE_DEPTH_MAX)

            props[ids[i]] = dict(
                idx=int(i), side=s, order=pos, u=u, n=n, a=a, b=b, f=f,
                anchor=xy[i], t=float(t[i]), d=float(d[i]),
                prev=(ids[prev_i] if prev_i is not None else None),
                next=(ids[next_i] if next_i is not None else None),
            )

    # --- build quads, then 6. de-conflict ---------------------------------
    def quad(p):
        A = p["anchor"]
        return ensure_ccw(np.array([
            A - p["a"] * p["u"] - ZONE_BACK * p["n"],
            A + p["b"] * p["u"] - ZONE_BACK * p["n"],
            A + p["b"] * p["u"] + p["f"] * p["n"],
            A - p["a"] * p["u"] + p["f"] * p["n"],
        ]))

    shrinks = 0
    shrunk: set[str] = set()
    for _ in range(40):
        quads = {k: quad(v) for k, v in props.items()}
        clash = None
        keys = list(props)
        for ii in range(len(keys)):
            for jj in range(ii + 1, len(keys)):
                if quads_overlap(quads[keys[ii]], quads[keys[jj]]):
                    clash = (keys[ii], keys[jj])
                    break
            if clash:
                break
        if not clash:
            break
        for k in clash:
            props[k]["a"] *= 0.90
            props[k]["b"] *= 0.90
            props[k]["f"] *= 0.90
            shrunk.add(k)
        shrinks += 1
    quads = {k: quad(v) for k, v in props.items()}

    # --- 7. back to EPSG:4326 --------------------------------------------
    def ll(pt):
        lat, lon = from_utm(float(pt[0]), float(pt[1]), zone)
        return (round(lat, 8), round(lon, 8))

    out = {}
    for pid, p in props.items():
        A = p["anchor"]
        front = [A - p["a"] * p["u"], A + p["b"] * p["u"]]
        ring = list(quads[pid]) + [quads[pid][0]]
        out[pid] = dict(
            side=p["side"],
            order=p["order"],
            t=p["t"],
            d=p["d"],
            anchor_ll=ll(A),
            frontage_ll=[ll(x) for x in front],
            zone_ll=[ll(x) for x in ring],
            frontage_len_m=round(p["a"] + p["b"], 3),
            zone_area_m2=round(poly_area(quads[pid]), 2),
            zone_depth_m=round(p["f"] + ZONE_BACK, 3),
            prev=p["prev"], next=p["next"],
        )

    meta = dict(
        epsg=epsg, utm_zone=zone, bearing_deg=round(bearing, 1),
        lane_length_m=round(float(t.max() - t.min()), 1),
        cross_span_m=round(float(d.max() - d.min()), 1),
        pca_sv=[round(float(x), 2) for x in sv],
        shrink_rounds=shrinks,
        shrunk=sorted(shrunk),
        north=[ids[i] for i in groups["NORTH"]],
        south=[ids[i] for i in groups["SOUTH"]],
    )
    frame = dict(axis=axis, nrm=nrm, centre=centre, t=t, d=d)
    return props, quads, out, meta, xy, zone, frame


# ==========================================================================
# validation
# ==========================================================================
def validate(props, quads, out, meta, xy, zone) -> tuple[bool, list[str]]:
    msgs, ok = [], True

    def chk(cond, good, bad):
        nonlocal ok
        msgs.append(("  ok    " if cond else "  FAIL  ") + (good if cond else bad))
        if not cond:
            ok = False

    chk(selftest(), "UTM projection self-test", "UTM projection self-test failed")
    chk(len(out) == 16, "16 properties built", f"built {len(out)} properties, expected 16")
    chk(sorted(out) == [f"PROP-{i:03d}" for i in range(1, 17)],
        "ids are PROP-001..PROP-016 with no gaps", "property ids are wrong")

    # anchors must land inside their own zone
    def inside(pt, poly):
        n = len(poly)
        for i in range(n):
            e = poly[(i + 1) % n] - poly[i]
            if cross2(e, pt - poly[i]) < -1e-9:
                return False
        return True

    bad = [k for k, p in props.items() if not inside(p["anchor"], quads[k])]
    chk(not bad, "every surveyed anchor lies inside its own service zone",
        f"anchors outside their zone: {bad}")

    # no overlaps at all
    keys = list(quads)
    ov = [(keys[i], keys[j]) for i in range(len(keys)) for j in range(i + 1, len(keys))
          if quads_overlap(quads[keys[i]], quads[keys[j]])]
    chk(not ov, "no service-zone pair overlaps", f"overlapping zones: {ov}")

    areas = [v["zone_area_m2"] for v in out.values()]
    chk(all(2.0 <= a <= 80.0 for a in areas),
        f"zone areas {min(areas):.1f}-{max(areas):.1f} m2 are plausible",
        f"implausible zone areas: {sorted(areas)[:3]} .. {sorted(areas)[-3:]}")

    lens = [v["frontage_len_m"] for v in out.values()]
    chk(all(1.0 <= l <= 15.0 for l in lens),
        f"frontage lengths {min(lens):.1f}-{max(lens):.1f} m are plausible",
        f"implausible frontage lengths: {sorted(lens)[:3]}")

    if meta["shrink_rounds"]:
        msgs.append(
            f"  note  {meta['shrink_rounds']} de-conflict round(s) shrank "
            f"{', '.join(meta['shrunk'])} - their anchors are unusually close together"
        )
    else:
        msgs.append("  ok    no de-conflict shrinking was needed")
    chk(meta["shrink_rounds"] < 20,
        "de-conflict converged",
        "de-conflict did not converge - anchors may be duplicated")

    # sides must have been found geometrically, and must be non-trivial
    chk(len(meta["north"]) >= 3 and len(meta["south"]) >= 3,
        f"two roadsides detected: {len(meta['south'])} south, {len(meta['north'])} north",
        "roadside split looks wrong")
    return ok, msgs


# ==========================================================================
# test points for verify_demo.sh
# ==========================================================================
def test_points(props, quads, out, zone):
    def ll(pt):
        lat, lon = from_utm(float(pt[0]), float(pt[1]), zone)
        return (round(lat, 7), round(lon, 7))

    # (1) deep inside one zone: pick the zone whose anchor sits furthest from
    #     its own boundary, so the test is not fragile.
    def margin(pid):
        poly, A = quads[pid], props[pid]["anchor"]
        return min(
            abs(cross2(unit(poly[(i + 1) % 4] - poly[i]), A - poly[i]))
            for i in range(4)
        )
    inside_pid = max(quads, key=margin)
    inside_pt = ll(quads[inside_pid].mean(axis=0))

    # (2) in the gap between two adjacent same-side zones -> a genuine tie
    pairs = [(k, v["next"]) for k, v in props.items() if v["next"]]
    best = min(pairs, key=lambda p: abs(props[p[0]]["t"] - props[p[1]]["t"]))
    amb_pt = ll((props[best[0]]["anchor"] + props[best[1]]["anchor"]) / 2.0)

    # (3) well off the lane
    allxy = np.array([props[k]["anchor"] for k in props])
    far_pt = ll(allxy.mean(axis=0) + np.array([0.0, -120.0]))

    # (4) middle of the carriageway: several plausible candidates, no winner
    road_pt = ll(allxy.mean(axis=0))

    return dict(
        INSIDE_PROPERTY=inside_pid,
        INSIDE_LAT=inside_pt[0], INSIDE_LON=inside_pt[1],
        AMBIG_A=best[0], AMBIG_B=best[1],
        AMBIG_LAT=amb_pt[0], AMBIG_LON=amb_pt[1],
        FAR_LAT=far_pt[0], FAR_LON=far_pt[1],
        ROAD_LAT=road_pt[0], ROAD_LON=road_pt[1],
    )


# ==========================================================================
# picker track
# ==========================================================================
def build_track(props, quads, out, zone, frame):
    """A picker walk along the real lane, hitting every decision branch."""
    axis, nrm, centre = frame["axis"], frame["nrm"], frame["centre"]

    def ll(pt):
        lat, lon = from_utm(float(pt[0]), float(pt[1]), zone)
        return (round(lat, 7), round(lon, 7))

    south = sorted((k for k, v in props.items() if v["side"] == "SOUTH"),
                   key=lambda k: -props[k]["t"])          # east -> west
    north = sorted((k for k, v in props.items() if v["side"] == "NORTH"),
                   key=lambda k: props[k]["t"])           # west -> east
    east_end = south[0]

    def zc(pid):
        return ll(quads[pid].mean(axis=0))

    def gap(p1, p2):
        return ll((props[p1]["anchor"] + props[p2]["anchor"]) / 2.0)

    tr = []
    # 1. parked well clear of the lane -> NO_MATCH
    tr.append(("VEHICLE", *ll(props[east_end]["anchor"] + axis * 22.0 + nrm * -28.0),
               "vehicle parked off the lane at the east end"))
    # 2. approaching along the lane, just short of the first zone: understood
    #    but below the collection confidence threshold -> a pass-by
    tr.append(("APPROACH", *ll(props[east_end]["anchor"] + axis * 9.0),
               "walking in from the east end, just short of the first zone"))
    # 3-5. three collections down the south side
    for pid in south[:3]:
        tr.append(("ZONE", *zc(pid), f"inside the service zone of {pid} (south side)"))
    # 6. astride two adjacent zones -> genuine tie
    tr.append(("GAP", *gap(south[3], south[4]),
               f"standing between {south[3]} and {south[4]}"))
    # 7. mid-carriageway, both rows plausible
    tr.append(("CROSSING", *ll(centre), "crossing the carriageway - both rows plausible"))
    # 8-9. two collections back along the north side
    for pid in north[-1:] + north[-2:-1]:
        tr.append(("ZONE", *zc(pid), f"inside the service zone of {pid} (north side)"))
    # 10. back to the vehicle
    tr.append(("RETURN", *ll(centre + nrm * -85.0),
               "walking back to the vehicle, off the lane"))
    return tr


# ==========================================================================
# SQL
# ==========================================================================
def sql_lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def emit_sql(out, meta) -> str:
    ids = [f"PROP-{i:03d}" for i in range(1, 17)]
    L = []
    w = L.append

    w("-- =====================================================================")
    w("-- Wastraq demo - REAL 16-property lane")
    w("-- 2nd Cross Road, Krishnamurthy Puram, Mysuru")
    w("--")
    w("-- GENERATED FILE. Edit scripts/generate_real_lane.py and re-run:")
    w("--     python3 scripts/generate_real_lane.py")
    w("--")
    w(f"-- Anchors      : 16 surveyed entrance/service points (EPSG:4326)")
    w(f"-- Construction : EPSG:{meta['epsg']} (WGS84 / UTM {meta['utm_zone']}N), metres")
    w(f"-- Road bearing : {meta['bearing_deg']} deg   lane {meta['lane_length_m']} m"
      f" long, {meta['cross_span_m']} m across")
    w(f"-- South side   : {', '.join(meta['south'])}")
    w(f"-- North side   : {', '.join(meta['north'])}")
    w("--")
    w("-- Frontages and service zones are PROVISIONAL auto-generated geometry:")
    w(f"--     source   = {GEOM_SOURCE}")
    w("--     verified = false")
    w("-- Adjust them in QGIS and set verified = true as they are checked.")
    w("--")
    w("-- Runs in a single transaction. Does not drop the database and does not")
    w("-- touch collection_events, evidence or pickers.")
    w("-- =====================================================================")
    w("")
    w("\\set ON_ERROR_STOP on")
    w("BEGIN;")
    w("")

    # ---- additive schema -------------------------------------------------
    w("-- ---------------------------------------------------------------------")
    w("-- Additive schema changes (safe on an already-loaded demo database)")
    w("-- ---------------------------------------------------------------------")
    w("ALTER TABLE property_entrances     ADD COLUMN IF NOT EXISTS source TEXT;")
    w("ALTER TABLE property_frontages     ADD COLUMN IF NOT EXISTS source TEXT;")
    w("ALTER TABLE property_service_zones ADD COLUMN IF NOT EXISTS source TEXT;")
    w("")
    w("-- properties.verification_status gains FIELD_SURVEYED")
    w("ALTER TABLE properties DROP CONSTRAINT IF EXISTS properties_verification_status_check;")
    w("ALTER TABLE properties ADD  CONSTRAINT properties_verification_status_check")
    w("  CHECK (verification_status IN ('UNVERIFIED','FIELD_SURVEYED','FIELD_VERIFIED','DISPUTED'));")
    w("")
    w("-- Frontage photo linkage. Survey QA / human verification / dispute review only -")
    w("-- never the primary property-recognition mechanism (that stays the service zone).")
    w("CREATE TABLE IF NOT EXISTS property_photos (")
    w("    photo_id     TEXT PRIMARY KEY,")
    w("    property_id  TEXT NOT NULL REFERENCES properties(property_id)")
    w("                 ON UPDATE CASCADE ON DELETE CASCADE,")
    w("    photo_type   TEXT NOT NULL DEFAULT 'FRONTAGE'")
    w("                 CHECK (photo_type IN ('FRONTAGE','ENTRANCE','CONTEXT','DISPUTE')),")
    w("    file_path    TEXT NOT NULL,")
    w("    captured_at  TIMESTAMPTZ,")
    w("    verified     BOOLEAN NOT NULL DEFAULT FALSE,")
    w("    notes        TEXT,")
    w("    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()")
    w(");")
    w("CREATE INDEX IF NOT EXISTS idx_property_photos_property ON property_photos (property_id);")
    w("CREATE UNIQUE INDEX IF NOT EXISTS idx_property_photos_one_frontage")
    w("    ON property_photos (property_id) WHERE photo_type = 'FRONTAGE';")
    w("")

    # ---- properties ------------------------------------------------------
    w("-- ---------------------------------------------------------------------")
    w("-- 1. Upsert the 16 properties (administrative data stays dummy)")
    w("-- ---------------------------------------------------------------------")
    w("INSERT INTO properties (property_id, authority_property_id, house_number, owner_name,")
    w("                        formatted_address, property_type, route_id,")
    w("                        latitude, longitude, mapping_confidence, verification_status)")
    w("VALUES")
    rows = []
    for n, pid in enumerate(ids, start=1):
        lat, lon = out[pid]["anchor_ll"]
        rows.append(
            f"  ('{pid}','ULB-KMP-{1000+n}','D{n:03d}','Demo Owner {n:02d}',\n"
            f"   {sql_lit(ADDRESS)},'RESIDENTIAL','{ROUTE_ID}',\n"
            f"   {lat:.8f}, {lon:.8f}, 0.900, 'FIELD_SURVEYED')"
        )
    w(",\n".join(rows))
    w("ON CONFLICT (property_id) DO UPDATE SET")
    w("    authority_property_id = EXCLUDED.authority_property_id,")
    w("    house_number          = EXCLUDED.house_number,")
    w("    owner_name            = EXCLUDED.owner_name,")
    w("    formatted_address     = EXCLUDED.formatted_address,")
    w("    property_type         = EXCLUDED.property_type,")
    w("    route_id              = EXCLUDED.route_id,")
    w("    latitude              = EXCLUDED.latitude,")
    w("    longitude             = EXCLUDED.longitude,")
    w("    mapping_confidence    = EXCLUDED.mapping_confidence,")
    w("    verification_status   = EXCLUDED.verification_status;")
    w("")
    w("-- Retire any synthetic property that is not part of the real lane.")
    w("-- Guarded: a property that already carries collection events is kept, so")
    w("-- foreign keys and history are never broken.")
    w("DELETE FROM properties p")
    w(" WHERE p.property_id <> ALL (ARRAY[" + ",".join(f"'{i}'" for i in ids) + "])")
    w("   AND NOT EXISTS (SELECT 1 FROM collection_events c WHERE c.property_id = p.property_id);")
    w("")

    # ---- geometry --------------------------------------------------------
    w("-- ---------------------------------------------------------------------")
    w("-- 2. Replace the synthetic geometry for the real lane")
    w("-- ---------------------------------------------------------------------")
    w("DELETE FROM property_service_zones;")
    w("DELETE FROM property_frontages;")
    w("DELETE FROM property_entrances;")
    w("")
    w("-- 2a. Surveyed entrance / service anchors (exact on-site coordinates)")
    w("INSERT INTO property_entrances (entrance_id, property_id, geometry, verified, source) VALUES")
    rows = []
    for n, pid in enumerate(ids, start=1):
        lat, lon = out[pid]["anchor_ll"]
        rows.append(
            f"  ('ENT-{n:03d}','{pid}',"
            f"ST_SetSRID(ST_MakePoint({lon:.8f}, {lat:.8f}), 4326), TRUE, 'FIELD_SURVEY')"
        )
    w(",\n".join(rows) + ";")
    w("")

    w("-- 2b. Provisional frontages (auto-generated, road side computed from geometry)")
    w("INSERT INTO property_frontages (frontage_id, property_id, geometry, road_side, verified, source) VALUES")
    rows = []
    for n, pid in enumerate(ids, start=1):
        pts = out[pid]["frontage_ll"]
        wkt = "LINESTRING(" + ", ".join(f"{lo:.8f} {la:.8f}" for la, lo in pts) + ")"
        rows.append(
            f"  ('FRONT-{n:03d}','{pid}',ST_GeomFromText('{wkt}', 4326),"
            f"'{out[pid]['side']}', FALSE, '{GEOM_SOURCE}')"
        )
    w(",\n".join(rows) + ";")
    w("")

    w("-- 2c. Provisional service zones (auto-generated, non-overlapping)")
    w("INSERT INTO property_service_zones (zone_id, property_id, geometry, version, verified, source) VALUES")
    rows = []
    for n, pid in enumerate(ids, start=1):
        ring = out[pid]["zone_ll"]
        wkt = "POLYGON((" + ", ".join(f"{lo:.8f} {la:.8f}" for la, lo in ring) + "))"
        rows.append(
            f"  ('SZ-{n:03d}','{pid}',ST_GeomFromText('{wkt}', 4326), 1, FALSE, '{GEOM_SOURCE}')"
        )
    w(",\n".join(rows) + ";")
    w("")

    # ---- photos ----------------------------------------------------------
    w("-- ---------------------------------------------------------------------")
    w("-- 3. Link the 16 frontage photos")
    w(f"--    {PHOTO_DIR_TOKEN} is substituted by scripts/load_real_lane.sh")
    w("-- ---------------------------------------------------------------------")
    w("INSERT INTO property_photos (photo_id, property_id, photo_type, file_path, verified, notes) VALUES")
    rows = []
    for n, pid in enumerate(ids, start=1):
        rows.append(
            f"  ('PHOTO-{n:03d}','{pid}','FRONTAGE','{PHOTO_DIR_TOKEN}/{pid}.jpg', FALSE,"
            f" 'Survey QA / human verification only - not used for live recognition.')"
        )
    w(",\n".join(rows))
    w("ON CONFLICT (photo_id) DO UPDATE SET")
    w("    property_id = EXCLUDED.property_id,")
    w("    file_path   = EXCLUDED.file_path,")
    w("    photo_type  = EXCLUDED.photo_type;")
    w("")

    # ---- assertions ------------------------------------------------------
    w("-- ---------------------------------------------------------------------")
    w("-- 4. In-transaction assertions - the whole thing rolls back on failure")
    w("-- ---------------------------------------------------------------------")
    w("DO $$")
    w("DECLARE n INT; bad TEXT;")
    w("BEGIN")
    w("  SELECT count(*) INTO n FROM properties;")
    w("  IF n <> 16 THEN RAISE EXCEPTION 'expected 16 properties, found %', n; END IF;")
    w("  SELECT count(*) INTO n FROM property_entrances;")
    w("  IF n <> 16 THEN RAISE EXCEPTION 'expected 16 entrances, found %', n; END IF;")
    w("  SELECT count(*) INTO n FROM property_frontages;")
    w("  IF n <> 16 THEN RAISE EXCEPTION 'expected 16 frontages, found %', n; END IF;")
    w("  SELECT count(*) INTO n FROM property_service_zones;")
    w("  IF n <> 16 THEN RAISE EXCEPTION 'expected 16 service zones, found %', n; END IF;")
    w("  SELECT count(*) INTO n FROM property_photos WHERE photo_type = 'FRONTAGE';")
    w("  IF n <> 16 THEN RAISE EXCEPTION 'expected 16 frontage photos, found %', n; END IF;")
    w("")
    w("  SELECT count(*) INTO n FROM property_service_zones WHERE NOT ST_IsValid(geometry);")
    w("  IF n > 0 THEN RAISE EXCEPTION '% invalid service-zone polygons', n; END IF;")
    w("")
    w("  SELECT count(*) INTO n FROM property_service_zones a")
    w("    JOIN property_service_zones b ON a.zone_id < b.zone_id")
    w("   WHERE ST_Overlaps(a.geometry, b.geometry) OR ST_Contains(a.geometry, b.geometry);")
    w("  IF n > 0 THEN RAISE EXCEPTION '% overlapping service-zone pairs', n; END IF;")
    w("")
    w("  SELECT string_agg(e.property_id, ', ') INTO bad")
    w("    FROM property_entrances e JOIN property_service_zones z USING (property_id)")
    w("   WHERE NOT ST_Within(e.geometry, z.geometry);")
    w("  IF bad IS NOT NULL THEN")
    w("    RAISE EXCEPTION 'surveyed anchor lies outside its own service zone: %', bad;")
    w("  END IF;")
    w("")
    w("  SELECT count(*) INTO n FROM (")
    w("      SELECT ST_SRID(geometry) s FROM property_service_zones")
    w("      UNION SELECT ST_SRID(geometry) FROM property_frontages")
    w("      UNION SELECT ST_SRID(geometry) FROM property_entrances) q")
    w("   WHERE q.s <> 4326;")
    w("  IF n > 0 THEN RAISE EXCEPTION 'geometry found with SRID <> 4326'; END IF;")
    w("")
    w("  RAISE NOTICE 'Real lane loaded: 16 properties, 16 entrances, 16 frontages, "
      "16 service zones, 16 frontage photos.';")
    w("END $$;")
    w("")
    w("COMMIT;")
    w("")
    return "\n".join(L)


# ==========================================================================
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="validate only, write nothing")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    props, quads, out, meta, xy, zone, frame = build()
    ok, msgs = validate(props, quads, out, meta, xy, zone)

    if not args.quiet:
        print("Wastraq real lane - 16 surveyed properties")
        print(f"  CRS for construction : EPSG:{meta['epsg']} (UTM {meta['utm_zone']}N)")
        print(f"  road bearing         : {meta['bearing_deg']} deg")
        print(f"  lane extent          : {meta['lane_length_m']} m long, "
              f"{meta['cross_span_m']} m across")
        print(f"  south side ({len(meta['south'])})       : {', '.join(meta['south'])}")
        print(f"  north side ({len(meta['north'])})       : {', '.join(meta['north'])}")
        print()
        print(f"  {'id':<9} {'side':<6} {'frontage m':>10} {'zone m2':>8} {'depth m':>8}")
        for pid in sorted(out):
            v = out[pid]
            print(f"  {pid:<9} {v['side']:<6} {v['frontage_len_m']:>10.2f} "
                  f"{v['zone_area_m2']:>8.2f} {v['zone_depth_m']:>8.2f}")
        print()
        print("Validation:")
        for m in msgs:
            print(m)
        print()

    if args.check:
        print("CHECK PASSED" if ok else "CHECK FAILED")
        return 0 if ok else 1
    if not ok:
        print("Refusing to write files: validation failed.")
        return 1

    sql_path = os.path.join(ROOT, "database", "real_lane_16.sql")
    with open(sql_path, "w") as fh:
        fh.write(emit_sql(out, meta))
    print(f"wrote {sql_path}")

    track = build_track(props, quads, out, zone, frame)
    track_path = os.path.join(ROOT, "simulation", "track_real_lane.json")
    with open(track_path, "w") as fh:
        json.dump(
            {"name": "2nd Cross Road, Krishnamurthy Puram (real surveyed lane)",
             "picker_id": "PICKER-01", "track_id": "TRACK-KMP-001",
             "waypoints": [{"label": l, "latitude": la, "longitude": lo, "note": nt}
                           for l, la, lo, nt in track]},
            fh, indent=2)
    print(f"wrote {track_path}  ({len(track)} waypoints)")

    tp = test_points(props, quads, out, zone)
    tp["EXPECTED_PROPERTIES"] = 16
    env_path = os.path.join(ROOT, "scripts", "real_lane_testpoints.env")
    with open(env_path, "w") as fh:
        fh.write("# GENERATED by scripts/generate_real_lane.py - sourced by verify_demo.sh\n")
        for k, v in tp.items():
            fh.write(f"{k}={v}\n")
    print(f"wrote {env_path}")

    geo_path = os.path.join(ROOT, "database", "real_lane_16.geojson")
    feats = []
    for pid in sorted(out):
        v = out[pid]
        feats.append({"type": "Feature", "id": pid,
                      "properties": {"property_id": pid, "kind": "service_zone",
                                     "side": v["side"], "area_m2": v["zone_area_m2"],
                                     "source": GEOM_SOURCE, "verified": False},
                      "geometry": {"type": "Polygon",
                                   "coordinates": [[[lo, la] for la, lo in v["zone_ll"]]]}})
        feats.append({"type": "Feature", "id": pid + "-anchor",
                      "properties": {"property_id": pid, "kind": "entrance"},
                      "geometry": {"type": "Point",
                                   "coordinates": [v["anchor_ll"][1], v["anchor_ll"][0]]}})
    with open(geo_path, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": feats}, fh)
    print(f"wrote {geo_path}  (drag into QGIS or geojson.io for a quick look)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

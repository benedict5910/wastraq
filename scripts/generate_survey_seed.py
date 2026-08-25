#!/usr/bin/env python3
"""
Generate database/survey_seed.sql - a realistic city-scale survey dataset.

Everything here is fictitious: invented owner names, invented surveyor names,
invented ward codes. No real personal data.

Shape of the generated city (Mysuru, used as the sample authority):

    CITY-MYS  Mysuru City Corporation
      3 ZONEs
        8 WARDs
          20 ROUTE_AREAs
            ~1200 properties

The 16 real surveyed demo-lane properties are NOT recreated - they already
exist. The seed only attaches them to the hierarchy (WARD-W12 /
RA-KMP-DEMO) and gives them an APPROVED survey record, so the same property
appears in both the operations dashboard and the survey dashboard.

Deterministic: a fixed PRNG seed, so re-running produces byte-identical SQL.

    python3 scripts/generate_survey_seed.py
"""

from __future__ import annotations

import math
import os
import random
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from utm import from_utm, to_utm, zone_for_lon  # noqa: E402

SEED = 20260819

# Must match settings.GNSS_ACCURACY_WARN_M (backend/app/config.py). The seed
# has to obey the same rule the API enforces at capture time - see the cap
# applied just after `acc` is drawn below.
GNSS_ACCURACY_WARN_M = 10.0
RNG = random.Random(SEED)

# The demo lane, so the city is built around somewhere real.
CITY_LAT, CITY_LON = 12.2958, 76.6394
UTM_ZONE = zone_for_lon(CITY_LON)

# Timeline anchor. Fixed so the SQL is deterministic; the loader shifts
# everything relative to now() at load time.
T0 = datetime(2026, 6, 1, 9, 0, 0)

DEMO_ROUTE = "ROUTE-DEMO-01"
DEMO_WARD = "WARD-W12"
DEMO_ROUTE_AREA = "RA-KMP-DEMO"
REAL_PROPS = [f"PROP-{i:03d}" for i in range(1, 17)]

ZONES = [
    ("ZONE-N", "North Zone", "MCC-Z-N"),
    ("ZONE-S", "South Zone", "MCC-Z-S"),
    ("ZONE-W", "West Zone", "MCC-Z-W"),
]

WARDS = [
    ("WARD-W03", "Jayalakshmipuram", "ZONE-N", "MCC-W-003"),
    ("WARD-W07", "Vijayanagar 2nd Stage", "ZONE-N", "MCC-W-007"),
    ("WARD-W12", "Krishnamurthy Puram", "ZONE-W", "MCC-W-012"),
    ("WARD-W15", "Saraswathipuram", "ZONE-W", "MCC-W-015"),
    ("WARD-W21", "Kuvempunagar", "ZONE-S", "MCC-W-021"),
    ("WARD-W26", "Srirampura", "ZONE-S", "MCC-W-026"),
    ("WARD-W31", "Gokulam", "ZONE-N", "MCC-W-031"),
    ("WARD-W38", "Hebbal Industrial Area", "ZONE-W", "MCC-W-038"),
]

STREETS = [
    "1st Cross Road", "2nd Cross Road", "3rd Cross Road", "4th Cross Road",
    "1st Main Road", "2nd Main Road", "3rd Main Road", "Temple Street",
    "Park Road", "School Road", "Market Lane", "Canal Road",
    "Hospital Road", "Station Road", "Bank Street", "Garden Lane",
    "Post Office Road", "Library Road", "Water Tank Road", "Playground Road",
]

FIRST = ["Anitha", "Suresh", "Fathima", "Ravi", "Meera", "Joseph", "Lakshmi",
         "Gurdeep", "Priyanka", "Vikram", "Nandini", "Arun", "Kavya", "Manoj",
         "Sunitha", "Harish", "Deepa", "Rajesh", "Shalini", "Girish",
         "Ramya", "Prakash", "Vidya", "Naveen", "Asha", "Kiran", "Bhavani",
         "Mohan", "Sneha", "Ganesh"]
LAST = ["Raman", "Kamath", "Beevi", "Deshpande", "Nair", "Fernandes", "Iyer",
        "Singh", "Shetty", "Chauhan", "Rao", "Gowda", "Hegde", "Bhat",
        "Murthy", "Patil", "Reddy", "Kulkarni", "Menon", "Pillai"]

PROPERTY_TYPES = (["RESIDENTIAL"] * 78) + (["COMMERCIAL"] * 14) + \
                 (["MIXED"] * 6) + (["INSTITUTIONAL"] * 2)

SURVEYORS = [
    ("USR-101", "Ganesh Bhat", "EMP-1101"),
    ("USR-102", "Shanti Devi", "EMP-1102"),
    ("USR-103", "Imran Khan", "EMP-1103"),
    ("USR-104", "Rekha Prasad", "EMP-1104"),
    ("USR-105", "Tarun Sequeira", "EMP-1105"),
    ("USR-106", "Divya Rangan", "EMP-1106"),
    ("USR-107", "Basavaraj Naik", "EMP-1107"),
    ("USR-108", "Nithya Varma", "EMP-1108"),
]
REVIEWERS = [
    ("USR-201", "Anand Kulkarni", "EMP-1201"),
    ("USR-202", "Farida Sait", "EMP-1202"),
    ("USR-203", "Vinod Achar", "EMP-1203"),
]
SUPERVISORS = [
    ("USR-301", "Latha Srinivasan", "EMP-1301"),
    ("USR-302", "Mahesh Poojary", "EMP-1302"),
]
GIS_ADMINS = [("USR-401", "Yogesh Kamath", "EMP-1401")]
ADMINS = [("USR-501", "Wastraq Admin", "EMP-1501")]

# survey_status distribution for generated city properties
STATUS_MIX = (
    ["NOT_SURVEYED"] * 44 + ["IN_PROGRESS"] * 7 + ["SUBMITTED"] * 9 +
    ["APPROVED"] * 32 + ["CORRECTION_REQUIRED"] * 6 + ["REJECTED"] * 2
)
ANOMALIES = ["SHARED_GATE", "UNCLEAR_COLLECTION_AREA", "PROPERTY_MISMATCH",
             "COMMON_COLLECTION_POINT", "WRONG_MAP_POSITION",
             "INACCESSIBLE_PROPERTY", "OTHER"]


# ---------------------------------------------------------------------------
def q(v) -> str:
    """SQL literal."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, datetime):
        return "'" + v.strftime("%Y-%m-%d %H:%M:%S") + "'::timestamptz"
    if isinstance(v, date):
        return "'" + v.isoformat() + "'::date"
    if isinstance(v, (list, tuple)):
        inner = ",".join(str(x).replace("'", "") for x in v)
        return "'{" + inner + "}'"
    return "'" + str(v).replace("'", "''") + "'"


def m_offset(lat, lon, east_m, north_m):
    """Move a WGS84 point by metres, via UTM."""
    x, y = to_utm(lat, lon, UTM_ZONE)
    return from_utm(x + east_m, y + north_m, UTM_ZONE)


def rect_multipolygon(lat, lon, half_w_m, half_h_m) -> str:
    corners = [(-half_w_m, -half_h_m), (half_w_m, -half_h_m),
               (half_w_m, half_h_m), (-half_w_m, half_h_m), (-half_w_m, -half_h_m)]
    pts = []
    for ex, ny in corners:
        la, lo = m_offset(lat, lon, ex, ny)
        pts.append(f"{lo:.7f} {la:.7f}")
    return "MULTIPOLYGON(((" + ", ".join(pts) + ")))"


# ---------------------------------------------------------------------------
def build():
    out = {"admin_units": [], "users": [], "assignments": [], "properties": [],
           "surveys": [], "entrances": [], "frontages": [], "zones": [],
           "photos": [], "qa": []}

    # --- administrative hierarchy -----------------------------------------
    out["admin_units"].append(dict(
        admin_unit_id="CITY-MYS", name="Mysuru City Corporation", unit_type="CITY",
        parent_id=None, authority_code="MCC",
        geometry=rect_multipolygon(CITY_LAT, CITY_LON, 6000, 5000)))

    zone_pos = {"ZONE-N": (0, 2200), "ZONE-S": (0, -2200), "ZONE-W": (-2600, 0)}
    for zid, zname, zcode in ZONES:
        ex, ny = zone_pos[zid]
        la, lo = m_offset(CITY_LAT, CITY_LON, ex, ny)
        out["admin_units"].append(dict(
            admin_unit_id=zid, name=zname, unit_type="ZONE", parent_id="CITY-MYS",
            authority_code=zcode, geometry=rect_multipolygon(la, lo, 2400, 1800)))

    ward_centre = {}
    per_zone = {}
    for wid, wname, zid, wcode in WARDS:
        n = per_zone.get(zid, 0)
        per_zone[zid] = n + 1
        zex, zny = zone_pos[zid]
        # lay wards out in a row inside their zone
        wex = zex + (n - 1) * 1300
        wny = zny + (0 if n % 2 == 0 else 550)
        la, lo = m_offset(CITY_LAT, CITY_LON, wex, wny)
        ward_centre[wid] = (la, lo)
        out["admin_units"].append(dict(
            admin_unit_id=wid, name=wname, unit_type="WARD", parent_id=zid,
            authority_code=wcode, geometry=rect_multipolygon(la, lo, 620, 480)))

    # --- route areas -------------------------------------------------------
    route_areas = []          # (ra_id, name, ward_id, route_id, lat, lon, n_props)
    ra_n = 0
    # the demo lane gets its own route area inside WARD-W12
    route_areas.append((DEMO_ROUTE_AREA, "Krishnamurthy Puram Demo Lane",
                        DEMO_WARD, DEMO_ROUTE, 12.2943300, 76.6414800, 0))
    remaining = 20 - 1
    per_ward = {w[0]: 0 for w in WARDS}
    while remaining > 0:
        for wid, wname, zid, _ in WARDS:
            if remaining <= 0:
                break
            k = per_ward[wid]
            per_ward[wid] = k + 1
            ra_n += 1
            la0, lo0 = ward_centre[wid]
            la, lo = m_offset(la0, lo0, (k % 3 - 1) * 340, (k // 3 - 0.5) * 300)
            rid = f"ROUTE-R{ra_n:02d}"
            route_areas.append((f"RA-{ra_n:02d}", f"{wname} Route {k + 1}",
                                wid, rid, la, lo, RNG.randint(48, 92)))
            remaining -= 1

    for ra_id, ra_name, wid, rid, la, lo, _ in route_areas:
        out["admin_units"].append(dict(
            admin_unit_id=ra_id, name=ra_name, unit_type="ROUTE_AREA",
            parent_id=wid, authority_code=rid,
            geometry=rect_multipolygon(la, lo, 165, 140)))

    # --- users -------------------------------------------------------------
    def add_users(rows, role):
        for uid, name, emp in rows:
            out["users"].append(dict(
                user_id=uid, name=name, employee_id=emp, role=role,
                email=f"{uid.lower()}@wastraq-demo.invalid",
                phone=f"+91-90000-{uid[-3:]}00", active=True,
                assigned_authority="CITY-MYS"))
    add_users(SURVEYORS, "SURVEYOR")
    add_users(REVIEWERS, "REVIEWER")
    add_users(SUPERVISORS, "SUPERVISOR")
    add_users(GIS_ADMINS, "GIS_ADMIN")
    add_users(ADMINS, "ADMIN")

    # --- assignments -------------------------------------------------------
    assignments = {}
    for i, (ra_id, ra_name, wid, rid, la, lo, n_props) in enumerate(route_areas, start=1):
        aid = f"ASG-{i:04d}"
        surveyor = SURVEYORS[(i - 1) % len(SURVEYORS)][0]
        supervisor = SUPERVISORS[(i - 1) % len(SUPERVISORS)][0]
        created = T0 + timedelta(days=RNG.randint(0, 20), hours=RNG.randint(0, 6))
        due = (created + timedelta(days=RNG.randint(10, 35))).date()
        assignments[ra_id] = aid
        out["assignments"].append(dict(
            assignment_id=aid, admin_unit_id=ra_id, route_id=rid,
            assigned_to=surveyor, assigned_by=supervisor,
            status="NOT_STARTED", total_properties=n_props or len(REAL_PROPS),
            created_at=created, due_date=due))

    # --- properties + surveys ---------------------------------------------
    pid_n = 1000                     # generated ids start at PROP-01001
    survey_n = 0
    ent_n = frt_n = szn_n = photo_n = qa_n = 0

    def next_survey_id():
        nonlocal survey_n
        survey_n += 1
        return f"SRV-{survey_n:06d}"

    # 1) the 16 real demo properties: attach + APPROVED survey, no new geometry
    demo_asg = assignments[DEMO_ROUTE_AREA]
    for k, pid in enumerate(REAL_PROPS):
        sid = next_survey_id()
        started = T0 + timedelta(days=2, minutes=18 * k)
        completed = started + timedelta(minutes=12)
        reviewed = completed + timedelta(days=1, hours=2)
        out["surveys"].append(dict(
            survey_id=sid, property_id=pid, assignment_id=demo_asg,
            surveyor_id="USR-101", survey_status="APPROVED",
            survey_started_at=started, survey_completed_at=completed,
            mapping_confidence="HIGH", source_class="VERIFIED_FIELD_SURVEY",
            notes="Demo lane - surveyed on site, geometry auto-generated then reviewed.",
            anomaly_type=[], submitted_at=completed,
            reviewer_id="USR-201", reviewed_at=reviewed, review_status="APPROVED",
            review_notes="Matches field observation.",
            captured_latitude=None, captured_longitude=None,
            location_accuracy_m=4.2, location_source="DEVICE_GNSS",
            captured_at=started, captured_by="USR-101",
            capture_device="Android handset (seed)", manually_adjusted=(k % 4 == 0),
            adjustment_timestamp=(completed if k % 4 == 0 else None),
            adjusted_by=("USR-101" if k % 4 == 0 else None),
            attach_real_photo=k, use_entrance_as_capture=True))

    # 2) generated city properties
    for ra_id, ra_name, wid, rid, la, lo, n_props in route_areas:
        if n_props == 0:
            continue
        aid = assignments[ra_id]
        street = STREETS[abs(hash(ra_id)) % len(STREETS)]
        ward_name = next(w[1] for w in WARDS if w[0] == wid)
        # two rows of plots along a street through the route area
        for j in range(n_props):
            pid_n += 1
            pid = f"PROP-{pid_n:05d}"
            side = -1 if j % 2 == 0 else 1
            along = (j // 2) * 13.5 - (n_props / 4) * 13.5
            plat, plon = m_offset(la, lo, along, side * 9.0)

            owner = f"{RNG.choice(FIRST)} {RNG.choice(LAST)}"
            house = f"{(j // 2) + 1}{'AB'[j % 2]}"
            ptype = RNG.choice(PROPERTY_TYPES)
            status = RNG.choice(STATUS_MIX)

            surveyed = status != "NOT_SURVEYED"
            approved = status == "APPROVED"
            conf = ("HIGH" if approved and RNG.random() < 0.75 else
                    "MEDIUM" if RNG.random() < 0.7 else "LOW") if surveyed else None
            vstatus = ("VERIFIED_FOR_OPERATION" if approved else
                       "FIELD_SURVEYED" if surveyed else "UNVERIFIED")
            map_conf = {"HIGH": 0.95, "MEDIUM": 0.78, "LOW": 0.55}.get(conf, 0.30)

            out["properties"].append(dict(
                property_id=pid, authority_property_id=f"MCC-{pid_n:06d}",
                house_number=house, owner_name=owner,
                formatted_address=f"{house}, {street}, {ward_name}, Mysuru, Karnataka",
                property_type=ptype, route_id=rid,
                latitude=round(plat, 7), longitude=round(plon, 7),
                mapping_confidence=map_conf, verification_status=vstatus,
                admin_unit_id=ra_id))

            if not surveyed:
                # still create a NOT_SURVEYED row so assignment totals line up
                out["surveys"].append(dict(
                    survey_id=next_survey_id(), property_id=pid, assignment_id=aid,
                    surveyor_id=None, survey_status="NOT_SURVEYED",
                    anomaly_type=[], manually_adjusted=False))
                continue

            sid = next_survey_id()
            surveyor = next(a["assigned_to"] for a in out["assignments"]
                            if a["assignment_id"] == aid)
            started = T0 + timedelta(days=RNG.randint(3, 40),
                                     hours=RNG.randint(9, 17), minutes=RNG.randint(0, 59))
            completed = started + timedelta(minutes=RNG.randint(6, 25))

            # --- device GNSS capture, deliberately imperfect ---------------
            acc = round(RNG.choice([3.1, 4.0, 4.8, 5.5, 6.2, 7.4, 9.0, 12.5, 18.0, 24.0]), 1)

            # The same rule the API applies on capture: a fix worse than the
            # threshold cannot carry HIGH confidence. Without this the
            # demonstration data contradicts the behaviour the dashboards
            # describe - a reviewer would see "HIGH confidence" sitting next to
            # a plus-or-minus 24 m fix, which is exactly what the design says
            # must never happen. Applied AFTER the draw so the random sequence
            # (and therefore the rest of the seed) is unchanged.
            if conf == "HIGH" and acc > GNSS_ACCURACY_WARN_M:
                conf = "MEDIUM"
                out["properties"][-1]["mapping_confidence"] = 0.78
            jitter = min(acc, 12.0)
            clat, clon = m_offset(plat, plon,
                                  RNG.uniform(-jitter, jitter), RNG.uniform(-jitter, jitter))
            adjusted = acc > GNSS_ACCURACY_WARN_M or RNG.random() < 0.18

            anomalies = []
            if RNG.random() < 0.12:
                anomalies.append(RNG.choice(ANOMALIES))

            reviewer = RNG.choice(REVIEWERS)[0]
            review_map = {"APPROVED": "APPROVED", "CORRECTION_REQUIRED": "CORRECTION_REQUIRED",
                          "REJECTED": "REJECTED", "SUBMITTED": "PENDING"}
            rstatus = review_map.get(status)
            reviewed_at = (completed + timedelta(days=RNG.randint(1, 4))
                           if status in ("APPROVED", "CORRECTION_REQUIRED", "REJECTED") else None)

            out["surveys"].append(dict(
                survey_id=sid, property_id=pid, assignment_id=aid,
                surveyor_id=surveyor, survey_status=status,
                survey_started_at=started,
                survey_completed_at=(completed if status != "IN_PROGRESS" else None),
                mapping_confidence=conf,
                source_class=("VERIFIED_FIELD_SURVEY" if conf == "HIGH" else
                              "APPROXIMATE_GEOCODE" if conf == "LOW" else "AUTHORITY_GIS"),
                notes=None,
                anomaly_type=anomalies,
                submitted_at=(completed if status != "IN_PROGRESS" else None),
                reviewer_id=(reviewer if reviewed_at else None),
                reviewed_at=reviewed_at, review_status=rstatus,
                review_notes=("Service zone extends across the kerb - please redraw."
                              if status == "CORRECTION_REQUIRED" else None),
                captured_latitude=round(clat, 7), captured_longitude=round(clon, 7),
                location_accuracy_m=acc, location_source="DEVICE_GNSS",
                captured_at=started, captured_by=surveyor,
                capture_device=RNG.choice(["Android handset", "Field tablet"]),
                manually_adjusted=adjusted,
                adjustment_timestamp=(completed if adjusted else None),
                adjusted_by=(surveyor if adjusted else None),
                attach_real_photo=None, use_entrance_as_capture=False))

            # --- geometry for anything that reached a reviewer -------------
            if status in ("SUBMITTED", "APPROVED", "CORRECTION_REQUIRED"):
                ent_n += 1
                frt_n += 1
                szn_n += 1
                verified = (status == "APPROVED")
                src = "FIELD_SURVEY_PLUS_AUTO_GEOMETRY"
                out["entrances"].append(dict(
                    entrance_id=f"ENT-C{ent_n:05d}", property_id=pid,
                    lat=round(plat, 7), lon=round(plon, 7), verified=verified,
                    source="FIELD_SURVEY", survey_id=sid, created_by=surveyor,
                    verified_by=(reviewer if verified else None),
                    verified_at=(reviewed_at if verified else None)))

                half = 5.6
                a1 = m_offset(plat, plon, -half, 0)
                a2 = m_offset(plat, plon, half, 0)
                out["frontages"].append(dict(
                    frontage_id=f"FRONT-C{frt_n:05d}", property_id=pid,
                    pts=[a1, a2], road_side=("NORTH" if side > 0 else "SOUTH"),
                    verified=verified, source=src, survey_id=sid, created_by=surveyor,
                    verified_by=(reviewer if verified else None),
                    verified_at=(reviewed_at if verified else None)))

                depth_out = 3.2 * (1 if side < 0 else -1)
                ring = [m_offset(plat, plon, -half + 0.2, -0.9 * (1 if side < 0 else -1)),
                        m_offset(plat, plon, half - 0.2, -0.9 * (1 if side < 0 else -1)),
                        m_offset(plat, plon, half - 0.2, depth_out),
                        m_offset(plat, plon, -half + 0.2, depth_out)]
                ring.append(ring[0])
                out["zones"].append(dict(
                    zone_id=f"SZ-C{szn_n:05d}", property_id=pid, ring=ring,
                    verified=verified, source=src, survey_id=sid, created_by=surveyor,
                    verified_by=(reviewer if verified else None),
                    verified_at=(reviewed_at if verified else None)))

                # --- photos -------------------------------------------------
                for ptype_photo in (["FRONTAGE"] +
                                    (["HOUSE_NUMBER"] if RNG.random() < 0.6 else []) +
                                    (["GATE"] if RNG.random() < 0.35 else [])):
                    photo_n += 1
                    sample = REAL_PROPS[photo_n % len(REAL_PROPS)]
                    out["photos"].append(dict(
                        photo_id=f"PHOTO-C{photo_n:05d}", property_id=pid,
                        survey_id=sid, photo_type=ptype_photo, sample=sample,
                        captured_at=completed, captured_by=surveyor))

            # --- QA issues from conditions that are actually true ----------
            if acc > GNSS_ACCURACY_WARN_M:
                qa_n += 1
                out["qa"].append(dict(
                    issue_id=f"QA-{qa_n:06d}", property_id=pid, survey_id=sid,
                    issue_type="LARGE_GPS_DISPLACEMENT", severity="MEDIUM",
                    status="OPEN" if not adjusted else "RESOLVED",
                    description=f"Device reported {acc} m accuracy, above the 10 m threshold.",
                    detected_at=completed,
                    resolved_at=(completed if adjusted else None),
                    resolved_by=(surveyor if adjusted else None)))
            elif conf == "LOW":
                qa_n += 1
                out["qa"].append(dict(
                    issue_id=f"QA-{qa_n:06d}", property_id=pid, survey_id=sid,
                    issue_type="LOW_MAPPING_CONFIDENCE", severity="LOW", status="OPEN",
                    description="Surveyor recorded LOW mapping confidence.",
                    detected_at=completed, resolved_at=None, resolved_by=None))
            elif "SHARED_GATE" in anomalies:
                qa_n += 1
                out["qa"].append(dict(
                    issue_id=f"QA-{qa_n:06d}", property_id=pid, survey_id=sid,
                    issue_type="SHARED_GATE", severity="MEDIUM", status="OPEN",
                    description="Surveyor flagged a gate shared with a neighbouring property.",
                    detected_at=completed, resolved_at=None, resolved_by=None))
            elif "COMMON_COLLECTION_POINT" in anomalies:
                qa_n += 1
                out["qa"].append(dict(
                    issue_id=f"QA-{qa_n:06d}", property_id=pid, survey_id=sid,
                    issue_type="COMMON_COLLECTION_POINT", severity="HIGH", status="OPEN",
                    description="Waste is presented at a shared collection point.",
                    detected_at=completed, resolved_at=None, resolved_by=None))
            elif status == "SUBMITTED" and RNG.random() < 0.10:
                qa_n += 1
                out["qa"].append(dict(
                    issue_id=f"QA-{qa_n:06d}", property_id=pid, survey_id=sid,
                    issue_type="MANUAL_REVIEW_REQUIRED", severity="LOW", status="OPEN",
                    description="Flagged by the surveyor for a second opinion.",
                    detected_at=completed, resolved_at=None, resolved_by=None))

    # assignment statuses derived from their surveys
    by_asg = {}
    for s in out["surveys"]:
        by_asg.setdefault(s["assignment_id"], []).append(s["survey_status"])
    for a in out["assignments"]:
        st = by_asg.get(a["assignment_id"], [])
        done = sum(1 for x in st if x in ("APPROVED", "REJECTED"))
        touched = sum(1 for x in st if x != "NOT_SURVEYED")
        if st and done == len(st):
            a["status"] = "COMPLETED"
            a["completed_at"] = T0 + timedelta(days=45)
        elif touched == 0:
            a["status"] = "NOT_STARTED"
        elif touched == len(st):
            a["status"] = "SUBMITTED"
        else:
            a["status"] = "IN_PROGRESS"
        a["surveyed_count"] = touched
        a["verified_count"] = sum(1 for x in st if x == "APPROVED")
        a["total_properties"] = len(st) or a["total_properties"]
    return out


# ---------------------------------------------------------------------------
def emit(d) -> str:
    L = []
    w = L.append
    w("-- =====================================================================")
    w("-- Wastraq - CITY-SCALE SURVEY SEED DATA")
    w("--")
    w("-- GENERATED FILE. Edit scripts/generate_survey_seed.py and re-run.")
    w("-- All names, owners, surveyors, ward codes and addresses are invented.")
    w("--")
    w(f"--   admin units : {len(d['admin_units'])}")
    w(f"--   users       : {len(d['users'])}")
    w(f"--   assignments : {len(d['assignments'])}")
    w(f"--   properties  : {len(d['properties'])}  (plus the 16 real demo-lane rows)")
    w(f"--   surveys     : {len(d['surveys'])}")
    w(f"--   geometry    : {len(d['entrances'])} entrances / "
      f"{len(d['frontages'])} frontages / {len(d['zones'])} zones")
    w(f"--   photos      : {len(d['photos'])}")
    w(f"--   QA issues   : {len(d['qa'])}")
    w("--")
    w("-- Runs in one transaction. Re-runnable: every insert is an upsert and")
    w("-- the generated rows are namespaced (PROP-01xxx, ENT-Cxxxxx, ...) so")
    w("-- the real 16-property demo lane is never touched.")
    w("-- =====================================================================")
    w("")
    w("\\set ON_ERROR_STOP on")
    w("BEGIN;")
    w("")
    w("-- Shift the fixed generation timeline so the data looks recent.")
    w("CREATE TEMP TABLE _wq_epoch ON COMMIT DROP AS")
    w("  SELECT (now() - '2026-07-05 12:00:00'::timestamptz) AS shift;")
    w("")

    # --- admin units (parents first) --------------------------------------
    w("-- Administrative hierarchy -------------------------------------------")
    for u in d["admin_units"]:
        w("INSERT INTO administrative_units (admin_unit_id, name, unit_type, parent_id, "
          "authority_code, geometry, active) VALUES (")
        w(f"  {q(u['admin_unit_id'])}, {q(u['name'])}, {q(u['unit_type'])}, "
          f"{q(u['parent_id'])}, {q(u['authority_code'])},")
        w(f"  ST_GeomFromText({q(u['geometry'])}, 4326), TRUE)")
        w("ON CONFLICT (admin_unit_id) DO UPDATE SET name = EXCLUDED.name,")
        w("  unit_type = EXCLUDED.unit_type, parent_id = EXCLUDED.parent_id,")
        w("  authority_code = EXCLUDED.authority_code, geometry = EXCLUDED.geometry,")
        w("  updated_at = now();")
    w("")

    # --- users -------------------------------------------------------------
    w("-- Surveyors, reviewers, supervisors ----------------------------------")
    w("INSERT INTO survey_users (user_id, name, employee_id, role, email, phone, "
      "active, assigned_authority) VALUES")
    rows = [f"  ({q(u['user_id'])}, {q(u['name'])}, {q(u['employee_id'])}, {q(u['role'])}, "
            f"{q(u['email'])}, {q(u['phone'])}, TRUE, {q(u['assigned_authority'])})"
            for u in d["users"]]
    w(",\n".join(rows))
    w("ON CONFLICT (user_id) DO UPDATE SET name = EXCLUDED.name, role = EXCLUDED.role,")
    w("  employee_id = EXCLUDED.employee_id, assigned_authority = EXCLUDED.assigned_authority;")
    w("")

    # --- assignments -------------------------------------------------------
    w("-- Survey assignments --------------------------------------------------")
    w("INSERT INTO survey_assignments (assignment_id, admin_unit_id, route_id, assigned_to,")
    w("  assigned_by, status, total_properties, surveyed_count, verified_count,")
    w("  created_at, due_date, completed_at) VALUES")
    rows = []
    for a in d["assignments"]:
        rows.append(
            f"  ({q(a['assignment_id'])}, {q(a['admin_unit_id'])}, {q(a['route_id'])}, "
            f"{q(a['assigned_to'])}, {q(a['assigned_by'])}, {q(a['status'])}, "
            f"{a['total_properties']}, {a.get('surveyed_count', 0)}, "
            f"{a.get('verified_count', 0)}, {q(a['created_at'])} + "
            f"(SELECT shift FROM _wq_epoch), {q(a['due_date'])}, "
            f"{q(a.get('completed_at'))})")
    w(",\n".join(rows))
    w("ON CONFLICT (assignment_id) DO UPDATE SET status = EXCLUDED.status,")
    w("  assigned_to = EXCLUDED.assigned_to, total_properties = EXCLUDED.total_properties,")
    w("  surveyed_count = EXCLUDED.surveyed_count, verified_count = EXCLUDED.verified_count,")
    w("  due_date = EXCLUDED.due_date, completed_at = EXCLUDED.completed_at;")
    w("")

    # --- attach the real demo properties to the hierarchy ------------------
    w("-- Attach the 16 real demo-lane properties to the hierarchy ------------")
    w(f"UPDATE properties SET admin_unit_id = {q(DEMO_ROUTE_AREA)}")
    w(f" WHERE route_id = {q(DEMO_ROUTE)};")
    w("")

    # --- generated properties ---------------------------------------------
    w("-- City-scale properties (all owner details are invented) -------------")
    CH = 200
    props = d["properties"]
    for i in range(0, len(props), CH):
        chunk = props[i:i + CH]
        w("INSERT INTO properties (property_id, authority_property_id, house_number,")
        w("  owner_name, formatted_address, property_type, route_id, latitude, longitude,")
        w("  mapping_confidence, verification_status, admin_unit_id) VALUES")
        rows = [
            f"  ({q(p['property_id'])}, {q(p['authority_property_id'])}, {q(p['house_number'])}, "
            f"{q(p['owner_name'])}, {q(p['formatted_address'])}, {q(p['property_type'])}, "
            f"{q(p['route_id'])}, {p['latitude']}, {p['longitude']}, {p['mapping_confidence']}, "
            f"{q(p['verification_status'])}, {q(p['admin_unit_id'])})"
            for p in chunk]
        w(",\n".join(rows))
        w("ON CONFLICT (property_id) DO UPDATE SET owner_name = EXCLUDED.owner_name,")
        w("  house_number = EXCLUDED.house_number, formatted_address = EXCLUDED.formatted_address,")
        w("  property_type = EXCLUDED.property_type, route_id = EXCLUDED.route_id,")
        w("  latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude,")
        w("  mapping_confidence = EXCLUDED.mapping_confidence,")
        w("  verification_status = EXCLUDED.verification_status,")
        w("  admin_unit_id = EXCLUDED.admin_unit_id;")
        w("")

    # --- surveys -----------------------------------------------------------
    w("-- Property surveys ---------------------------------------------------")
    surveys = d["surveys"]
    for i in range(0, len(surveys), CH):
        chunk = surveys[i:i + CH]
        w("INSERT INTO property_surveys (survey_id, property_id, assignment_id, surveyor_id,")
        w("  survey_status, survey_started_at, survey_completed_at, mapping_confidence,")
        w("  source_class, notes, anomaly_type, submitted_at, reviewer_id, reviewed_at,")
        w("  review_status, review_notes, captured_latitude, captured_longitude,")
        w("  captured_point, location_accuracy_m, location_source, captured_at, captured_by,")
        w("  capture_device, manually_adjusted, adjustment_timestamp, adjusted_by) VALUES")
        rows = []
        for s in chunk:
            shift = " + (SELECT shift FROM _wq_epoch)"
            def ts(key):
                v = s.get(key)
                return (q(v) + shift) if v else "NULL"
            if s.get("use_entrance_as_capture"):
                # the demo lane's raw fix: derive from the stored entrance
                pt = (f"(SELECT geometry FROM property_entrances "
                      f"WHERE property_id = {q(s['property_id'])} LIMIT 1)")
                clat = (f"(SELECT ST_Y(geometry) FROM property_entrances "
                        f"WHERE property_id = {q(s['property_id'])} LIMIT 1)")
                clon = (f"(SELECT ST_X(geometry) FROM property_entrances "
                        f"WHERE property_id = {q(s['property_id'])} LIMIT 1)")
            elif s.get("captured_latitude") is not None:
                pt = (f"ST_SetSRID(ST_MakePoint({s['captured_longitude']}, "
                      f"{s['captured_latitude']}), 4326)")
                clat, clon = repr(s["captured_latitude"]), repr(s["captured_longitude"])
            else:
                pt, clat, clon = "NULL", "NULL", "NULL"
            rows.append(
                f"  ({q(s['survey_id'])}, {q(s['property_id'])}, {q(s.get('assignment_id'))}, "
                f"{q(s.get('surveyor_id'))}, {q(s['survey_status'])}, "
                f"{ts('survey_started_at')}, {ts('survey_completed_at')}, "
                f"{q(s.get('mapping_confidence'))}, {q(s.get('source_class'))}, "
                f"{q(s.get('notes'))}, {q(s.get('anomaly_type', []))}, {ts('submitted_at')}, "
                f"{q(s.get('reviewer_id'))}, {ts('reviewed_at')}, {q(s.get('review_status'))}, "
                f"{q(s.get('review_notes'))}, {clat}, {clon}, {pt}, "
                f"{s.get('location_accuracy_m') if s.get('location_accuracy_m') else 'NULL'}, "
                f"{q(s.get('location_source'))}, {ts('captured_at')}, {q(s.get('captured_by'))}, "
                f"{q(s.get('capture_device'))}, {q(bool(s.get('manually_adjusted')))}, "
                f"{ts('adjustment_timestamp')}, {q(s.get('adjusted_by'))})")
        w(",\n".join(rows))
        w("ON CONFLICT (survey_id) DO UPDATE SET survey_status = EXCLUDED.survey_status,")
        w("  surveyor_id = EXCLUDED.surveyor_id, mapping_confidence = EXCLUDED.mapping_confidence,")
        w("  review_status = EXCLUDED.review_status, reviewed_at = EXCLUDED.reviewed_at,")
        w("  reviewer_id = EXCLUDED.reviewer_id, updated_at = now();")
        w("")

    # --- geometry ----------------------------------------------------------
    def geom_block(title, table, cols, rows_sql, conflict_key):
        w(f"-- {title}")
        for i in range(0, len(rows_sql), CH):
            chunk = rows_sql[i:i + CH]
            w(f"INSERT INTO {table} ({cols}) VALUES")
            w(",\n".join(chunk))
            w(f"ON CONFLICT ({conflict_key}) DO UPDATE SET geometry = EXCLUDED.geometry,")
            w("  verified = EXCLUDED.verified, source = EXCLUDED.source,")
            w("  survey_id = EXCLUDED.survey_id, updated_at = now();")
            w("")

    rows_sql = [
        f"  ({q(e['entrance_id'])}, {q(e['property_id'])}, "
        f"ST_SetSRID(ST_MakePoint({e['lon']}, {e['lat']}), 4326), {q(e['verified'])}, "
        f"{q(e['source'])}, 1, {q(e['survey_id'])}, {q(e['created_by'])}, "
        f"{q(e['verified_by'])}, {q(e['verified_at'])})"
        for e in d["entrances"]]
    geom_block("Survey entrance points", "property_entrances",
               "entrance_id, property_id, geometry, verified, source, version, survey_id, "
               "created_by, verified_by, verified_at", rows_sql, "entrance_id")

    rows_sql = []
    for f in d["frontages"]:
        wkt = "LINESTRING(" + ", ".join(f"{lo:.7f} {la:.7f}" for la, lo in f["pts"]) + ")"
        rows_sql.append(
            f"  ({q(f['frontage_id'])}, {q(f['property_id'])}, "
            f"ST_GeomFromText({q(wkt)}, 4326), {q(f['road_side'])}, {q(f['verified'])}, "
            f"{q(f['source'])}, 1, {q(f['survey_id'])}, {q(f['created_by'])}, "
            f"{q(f['verified_by'])}, {q(f['verified_at'])})")
    geom_block("Survey frontage lines", "property_frontages",
               "frontage_id, property_id, geometry, road_side, verified, source, version, "
               "survey_id, created_by, verified_by, verified_at", rows_sql, "frontage_id")

    rows_sql = []
    for z in d["zones"]:
        wkt = "POLYGON((" + ", ".join(f"{lo:.7f} {la:.7f}" for la, lo in z["ring"]) + "))"
        rows_sql.append(
            f"  ({q(z['zone_id'])}, {q(z['property_id'])}, "
            f"ST_GeomFromText({q(wkt)}, 4326), 1, {q(z['verified'])}, "
            f"{q(z['source'])}, {q(z['survey_id'])}, {q(z['created_by'])}, "
            f"{q(z['verified_by'])}, {q(z['verified_at'])})")
    geom_block("Survey service zones", "property_service_zones",
               "zone_id, property_id, geometry, version, verified, source, survey_id, "
               "created_by, verified_by, verified_at", rows_sql, "zone_id")

    # --- photos ------------------------------------------------------------
    w("-- Survey photos. Seeded rows point at the 16 real frontage images so the")
    w("-- survey UI shows something real; notes say plainly that they are samples.")
    photos = d["photos"]
    for i in range(0, len(photos), CH):
        chunk = photos[i:i + CH]
        w("INSERT INTO property_photos (photo_id, property_id, survey_id, photo_type,")
        w("  file_path, captured_at, captured_by, verified, notes) VALUES")
        rows = [
            f"  ({q(p['photo_id'])}, {q(p['property_id'])}, {q(p['survey_id'])}, "
            f"{q(p['photo_type'])}, '__PHOTO_DIR__/{p['sample']}.jpg', "
            f"{q(p['captured_at'])} + (SELECT shift FROM _wq_epoch), {q(p['captured_by'])}, "
            f"FALSE, 'Seed data: sample image, not a real capture for this property.')"
            for p in chunk]
        w(",\n".join(rows))
        w("ON CONFLICT (photo_id) DO UPDATE SET file_path = EXCLUDED.file_path,")
        w("  photo_type = EXCLUDED.photo_type, survey_id = EXCLUDED.survey_id;")
        w("")

    # --- QA ----------------------------------------------------------------
    w("-- GIS QA issues ------------------------------------------------------")
    qa = d["qa"]
    for i in range(0, len(qa), CH):
        chunk = qa[i:i + CH]
        w("INSERT INTO property_qa_issues (issue_id, property_id, survey_id, issue_type,")
        w("  severity, status, description, detected_at, resolved_at, resolved_by) VALUES")
        rows = [
            f"  ({q(x['issue_id'])}, {q(x['property_id'])}, {q(x['survey_id'])}, "
            f"{q(x['issue_type'])}, {q(x['severity'])}, {q(x['status'])}, "
            f"{q(x['description'])}, {q(x['detected_at'])} + (SELECT shift FROM _wq_epoch), "
            f"{(q(x['resolved_at']) + ' + (SELECT shift FROM _wq_epoch)') if x['resolved_at'] else 'NULL'}, "
            f"{q(x['resolved_by'])})"
            for x in chunk]
        w(",\n".join(rows))
        w("ON CONFLICT (issue_id) DO UPDATE SET status = EXCLUDED.status,")
        w("  severity = EXCLUDED.severity, resolved_at = EXCLUDED.resolved_at;")
        w("")

    # --- assertions --------------------------------------------------------
    w("-- Sanity assertions; the whole seed rolls back if any of these fail ---")
    w("DO $$")
    w("DECLARE n INT;")
    w("BEGIN")
    w("  SELECT count(*) INTO n FROM administrative_units WHERE unit_type='CITY';")
    w("  IF n <> 1 THEN RAISE EXCEPTION 'expected 1 city, found %', n; END IF;")
    w("  SELECT count(*) INTO n FROM administrative_units WHERE unit_type='WARD';")
    w(f"  IF n <> {len(WARDS)} THEN RAISE EXCEPTION 'expected {len(WARDS)} wards, found %', n; END IF;")
    w("  SELECT count(*) INTO n FROM administrative_units WHERE unit_type='ROUTE_AREA';")
    w("  IF n <> 20 THEN RAISE EXCEPTION 'expected 20 route areas, found %', n; END IF;")
    w("  SELECT count(*) INTO n FROM properties WHERE route_id = 'ROUTE-DEMO-01';")
    w("  IF n <> 16 THEN RAISE EXCEPTION 'the 16 demo-lane properties were disturbed: %', n; END IF;")
    w("  SELECT count(*) INTO n FROM properties WHERE admin_unit_id IS NULL;")
    w("  IF n > 0 THEN RAISE EXCEPTION '% properties are not attached to the hierarchy', n; END IF;")
    w("  SELECT count(*) INTO n FROM property_surveys s")
    w("    LEFT JOIN properties p ON p.property_id = s.property_id WHERE p.property_id IS NULL;")
    w("  IF n > 0 THEN RAISE EXCEPTION '% orphaned survey rows', n; END IF;")
    w("  RAISE NOTICE 'City survey seed loaded: % admin units, % users, % assignments, "
      "% properties, % surveys, % QA issues.',")
    w("    (SELECT count(*) FROM administrative_units), (SELECT count(*) FROM survey_users),")
    w("    (SELECT count(*) FROM survey_assignments), (SELECT count(*) FROM properties),")
    w("    (SELECT count(*) FROM property_surveys), (SELECT count(*) FROM property_qa_issues);")
    w("END $$;")
    w("")
    w("COMMIT;")
    w("")
    return "\n".join(L)


def main() -> int:
    d = build()
    sql = emit(d)
    path = os.path.join(ROOT, "database", "survey_seed.sql")
    with open(path, "w") as fh:
        fh.write(sql)
    print(f"wrote {path}  ({len(sql) // 1024} KB)")
    for k in ("admin_units", "users", "assignments", "properties", "surveys",
              "entrances", "frontages", "zones", "photos", "qa"):
        print(f"  {k:<12} {len(d[k])}")
    counts = {}
    for s in d["surveys"]:
        counts[s["survey_status"]] = counts.get(s["survey_status"], 0) + 1
    print("  survey status mix:", dict(sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())

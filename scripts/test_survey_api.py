#!/usr/bin/env python3
"""End-to-end test of the city survey workflow, over HTTP against a running backend.

    python3 scripts/test_survey_api.py            # quiet unless something fails
    python3 scripts/test_survey_api.py -v         # print every step

What it proves, in the order a real survey happens:

    assignment created -> survey started -> device fix captured -> poor fix
    warned about and confidence capped -> fix corrected on the map with BOTH
    coordinates preserved -> property attributes edited -> degenerate geometry
    rejected -> entrance / frontage / service zone drawn and measured by
    PostGIS -> frontage photo captured -> submission blocked until every
    requirement is met -> an entrance on the wrong building refused ->
    submitted -> returned for correction -> resubmitted -> approved ->
    property cleared for operation -> geometry history recorded throughout.

It also checks the module against the REAL pilot data, read-only: PROP-001
must return its own entrance, frontage, service zone, photo and thresholds,
and a property in an unassigned administrative unit must still be surveyable
end to end. There is
no synthetic city seed any more, so nothing here may assume one.

The test creates its own throwaway properties in a throwaway administrative
unit, runs the whole workflow against them, and deletes everything at the
end. It never touches the 16 pilot properties, so the counts every other
check asserts on stay exactly where they were. The cleanup also runs when a
step fails, so a broken run does not leave debris behind for the next one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

API = os.getenv("API", "http://127.0.0.1:8000").rstrip("/")

# Namespaced so nothing can collide with seeded ids.
T_UNIT = "TEST-UNIT-APIT"
T_PROP = "PROP-TESTAPI"
T_SURVEYOR = "USR-TESTAPI-S"
T_REVIEWER = "USR-TESTAPI-R"
# A second throwaway property deliberately created with NO assignment, to prove
# the field workflow does not require one. The synthetic city seed used to make
# every property arrive pre-assigned, which quietly hid that dependency.
T_PROP_SOLO = "PROP-TESTSOLO"
# ...in an administrative unit of its own. Putting it in T_UNIT was the bug:
# create_assignment seeds a NOT_SURVEYED row for EVERY property in the unit
# (recursively), so the solo property arrived pre-assigned and the "works with
# no assignment" case was never actually exercised.
T_UNIT_SOLO = "TEST-UNIT-SOLO"
# ...and one more in that same unassigned unit, used to prove the LAZY path:
# in an assignment's scope, no survey row until the surveyor starts it.
T_PROP_LAZY = "PROP-TESTLAZY"

# A patch of open ground ~2 km from the demo lane: far enough that nothing this
# test draws can ever fall inside a real service zone and perturb a lookup.
LAT, LON = 12.3130, 76.6600

PASS: list[str] = []
FAIL: list[str] = []
VERBOSE = False


# ---------------------------------------------------------------------------
def say(msg: str) -> None:
    if VERBOSE:
        print("   " + msg)


def ok(msg: str) -> None:
    PASS.append(msg)
    if VERBOSE:
        print(f"  \033[32mPASS\033[0m  {msg}")


def bad(msg: str) -> None:
    FAIL.append(msg)
    print(f"  \033[31mFAIL\033[0m  {msg}")


def check(cond: bool, msg: str) -> bool:
    (ok if cond else bad)(msg)
    return bool(cond)


def api(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    """Return (status, parsed-body). Never raises on an HTTP error status."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw
    except Exception as e:  # connection refused, timeout, ...
        return 0, str(e)


# --- direct SQL, only for creating and removing the test fixtures -----------
def db():
    from app.database import execute, fetch_one  # noqa: WPS433 (late import: needs sys.path)
    return execute, fetch_one


def setup() -> None:
    execute, fetch_one = db()
    root = fetch_one("SELECT admin_unit_id FROM administrative_units "
                     "WHERE parent_id IS NULL ORDER BY admin_unit_id LIMIT 1")
    parent = root["admin_unit_id"] if root else None

    execute(
        """
        INSERT INTO administrative_units (admin_unit_id, parent_id, name, unit_type, active)
        VALUES (%s, %s, 'API test area', 'ROUTE_AREA', TRUE)
        ON CONFLICT (admin_unit_id) DO UPDATE SET parent_id = EXCLUDED.parent_id
        """, (T_UNIT, parent))
    execute(
        """
        INSERT INTO administrative_units (admin_unit_id, parent_id, name, unit_type, active)
        VALUES (%s, %s, 'API test area (unassigned)', 'ROUTE_AREA', TRUE)
        ON CONFLICT (admin_unit_id) DO UPDATE SET parent_id = EXCLUDED.parent_id
        """, (T_UNIT_SOLO, parent))
    for uid, name, role in ((T_SURVEYOR, "API Test Surveyor", "SURVEYOR"),
                            (T_REVIEWER, "API Test Reviewer", "REVIEWER")):
        execute(
            """
            INSERT INTO survey_users (user_id, name, employee_id, role, active)
            VALUES (%s, %s, %s, %s, TRUE)
            ON CONFLICT (user_id) DO UPDATE SET role = EXCLUDED.role, active = TRUE
            """, (uid, name, uid, role))
    execute(
        """
        INSERT INTO properties (property_id, authority_property_id, house_number, owner_name,
            formatted_address, property_type, route_id, latitude, longitude,
            mapping_confidence, verification_status, admin_unit_id)
        VALUES (%s, 'TEST-API-1', 'T-1', 'API Test Owner',
                'Test plot, API test area', 'RESIDENTIAL', 'ROUTE-TESTAPI', %s, %s,
                0.50, 'UNVERIFIED', %s)
        ON CONFLICT (property_id) DO UPDATE SET admin_unit_id = EXCLUDED.admin_unit_id,
                                                verification_status = 'UNVERIFIED'
        """, (T_PROP, LAT, LON, T_UNIT))

    # deliberately NOT attached to an assignment, and ~40 m from the first
    execute(
        """
        INSERT INTO properties (property_id, authority_property_id, house_number, owner_name,
            formatted_address, property_type, route_id, latitude, longitude,
            mapping_confidence, verification_status, admin_unit_id)
        VALUES (%s, 'TEST-API-2', 'T-2', 'Unassigned Owner',
                'Unassigned plot, API test area', 'RESIDENTIAL', 'ROUTE-TESTAPI', %s, %s,
                0.50, 'UNVERIFIED', %s)
        ON CONFLICT (property_id) DO UPDATE SET admin_unit_id = EXCLUDED.admin_unit_id,
                                                verification_status = 'UNVERIFIED'
        """, (T_PROP_SOLO, LAT + 0.00036, LON, T_UNIT_SOLO))
    execute(
        """
        INSERT INTO properties (property_id, authority_property_id, house_number, owner_name,
            formatted_address, property_type, route_id, latitude, longitude,
            mapping_confidence, verification_status, admin_unit_id)
        VALUES (%s, 'TEST-API-3', 'T-3', 'Lazy Scope Owner',
                'Lazy plot, API test area', 'RESIDENTIAL', 'ROUTE-TESTAPI', %s, %s,
                0.50, 'UNVERIFIED', %s)
        ON CONFLICT (property_id) DO UPDATE SET admin_unit_id = EXCLUDED.admin_unit_id,
                                                verification_status = 'UNVERIFIED'
        """, (T_PROP_LAZY, LAT + 0.00072, LON, T_UNIT_SOLO))
    say(f"fixtures ready: {T_PROP} in {T_UNIT}; "
        f"{T_PROP_SOLO} and {T_PROP_LAZY} in {T_UNIT_SOLO}")


def teardown() -> None:
    execute, _ = db()
    for sql, params in (
        ("DELETE FROM property_qa_issues WHERE property_id = %s", (T_PROP_LAZY,)),
        ("DELETE FROM property_geometry_history WHERE property_id = %s", (T_PROP_LAZY,)),
        ("DELETE FROM property_photos WHERE property_id = %s", (T_PROP_LAZY,)),
        ("DELETE FROM property_entrances WHERE property_id = %s", (T_PROP_LAZY,)),
        ("DELETE FROM property_frontages WHERE property_id = %s", (T_PROP_LAZY,)),
        ("DELETE FROM property_service_zones WHERE property_id = %s", (T_PROP_LAZY,)),
        ("DELETE FROM property_surveys WHERE property_id = %s", (T_PROP_LAZY,)),
        ("DELETE FROM properties WHERE property_id = %s", (T_PROP_LAZY,)),
        ("DELETE FROM property_qa_issues WHERE property_id = %s", (T_PROP_SOLO,)),
        ("DELETE FROM property_geometry_history WHERE property_id = %s", (T_PROP_SOLO,)),
        ("DELETE FROM property_photos WHERE property_id = %s", (T_PROP_SOLO,)),
        ("DELETE FROM property_entrances WHERE property_id = %s", (T_PROP_SOLO,)),
        ("DELETE FROM property_frontages WHERE property_id = %s", (T_PROP_SOLO,)),
        ("DELETE FROM property_service_zones WHERE property_id = %s", (T_PROP_SOLO,)),
        ("DELETE FROM property_surveys WHERE property_id = %s", (T_PROP_SOLO,)),
        ("DELETE FROM properties WHERE property_id = %s", (T_PROP_SOLO,)),
        ("DELETE FROM property_qa_issues WHERE property_id = %s", (T_PROP,)),
        ("DELETE FROM property_geometry_history WHERE property_id = %s", (T_PROP,)),
        ("DELETE FROM property_photos WHERE property_id = %s", (T_PROP,)),
        ("DELETE FROM property_entrances WHERE property_id = %s", (T_PROP,)),
        ("DELETE FROM property_frontages WHERE property_id = %s", (T_PROP,)),
        ("DELETE FROM property_service_zones WHERE property_id = %s", (T_PROP,)),
        ("DELETE FROM property_surveys WHERE property_id = %s", (T_PROP,)),
        ("DELETE FROM properties WHERE property_id = %s", (T_PROP,)),
        ("DELETE FROM survey_assignments WHERE admin_unit_id IN (%s, %s)", (T_UNIT, T_UNIT_SOLO)),
        ("DELETE FROM administrative_units WHERE admin_unit_id IN (%s, %s)", (T_UNIT, T_UNIT_SOLO)),
        ("DELETE FROM survey_users WHERE user_id IN (%s, %s)", (T_SURVEYOR, T_REVIEWER)),
    ):
        try:
            execute(sql, params)
        except Exception as e:  # a failed run may have left less behind, not more
            say(f"cleanup note: {e}")
    say("fixtures removed")


# --- geometry the "surveyor" draws -----------------------------------------
D = 0.000045  # ~5 m at this latitude


def entrance_geojson(lat=LAT, lon=LON):
    return {"type": "Point", "coordinates": [lon, lat]}


def frontage_geojson():
    return {"type": "LineString",
            "coordinates": [[LON - D, LAT - D], [LON + D, LAT - D]]}


def zone_geojson():
    return {"type": "Polygon", "coordinates": [[
        [LON - D, LAT - D], [LON + D, LAT - D],
        [LON + D, LAT + D], [LON - D, LAT + D], [LON - D, LAT - D]]]}


def degenerate_line():
    """Two identical points: a LineString on paper, nothing on the ground."""
    return {"type": "LineString", "coordinates": [[LON, LAT], [LON, LAT]]}


def degenerate_zone():
    """A closed ring whose vertices are all the same point - zero area."""
    return {"type": "Polygon",
            "coordinates": [[[LON, LAT], [LON, LAT], [LON, LAT], [LON, LAT]]]}


def far_entrance():
    """~600 m away: the classic 'mapped the wrong building' mistake."""
    return {"type": "Point", "coordinates": [LON + 0.0055, LAT + 0.0055]}


# A 1x1 PNG. Small enough to inline, real enough that the endpoint hashes it
# and writes a genuine file to disk - which is the part being tested.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082")


def _upload_photo(survey_id: str, photo_type: str = "FRONTAGE",
                  method: str = "DEVICE_CAMERA") -> bool:
    """multipart/form-data by hand - no external HTTP library needed."""
    boundary = "----wastraqtestboundary"
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="file"; filename="frontage.png"\r\n',
        b"Content-Type: image/png\r\n\r\n",
        _PNG, b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    url = (f"{API}/survey/api/surveys/{survey_id}/photos"
           f"?photo_type={photo_type}&captured_by={T_SURVEYOR}&capture_method={method}")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status in (200, 201)
    except urllib.error.HTTPError as e:
        bad(f"photo upload failed: {e.code} {e.read().decode()[:200]}")
        return False
    except Exception as e:
        bad(f"photo upload failed: {type(e).__name__}: {e}")
        return False


# ---------------------------------------------------------------------------
def run() -> None:
    st, root = api("GET", "/")
    if not check(st == 200, f"backend reachable at {API}"):
        return

    # -- 1. assignment ------------------------------------------------------
    st, asg = api("POST", "/survey/api/assignments", {
        "admin_unit_id": T_UNIT, "route_id": "ROUTE-TESTAPI",
        "assigned_to": T_SURVEYOR, "due_date": "2026-12-31",
        "include_properties": True})
    if not check(st == 201 and isinstance(asg, dict), f"POST /assignments -> 201 (got {st})"):
        return
    assignment_id = asg["assignment_id"]
    # The response is {assignment_id, properties_in_scope, survey_rows_created,
    # assignment: <v_assignment_progress row>}. The old assertion read
    # asg["total_properties"], which lives one level down - so it defaulted to
    # 0 and failed while the endpoint was behaving perfectly.
    check(asg.get("properties_in_scope", 0) >= 1,
          f"the property is in the assignment's scope "
          f"({asg.get('properties_in_scope')} in scope)")
    check(asg.get("survey_rows_created", 0) >= 1,
          f"...and eager seeding created its survey row "
          f"({asg.get('survey_rows_created')} created)")
    prog = asg.get("assignment") or {}
    check(int(prog.get("total_properties") or 0) >= 1,
          f"...and progress reports the scope as the denominator "
          f"({prog.get('total_properties')} total)")
    check(int(prog.get("outstanding_count") or 0) >= 1,
          f"...with the work still outstanding ({prog.get('outstanding_count')})")

    st, one = api("GET", f"/survey/api/assignments/{assignment_id}")
    check(st == 200 and any(p["property_id"] == T_PROP for p in one.get("properties", [])),
          "assignment detail lists its properties")

    # -- 2. survey started --------------------------------------------------
    st, srv = api("POST", f"/survey/api/properties/{T_PROP}/survey",
                  {"surveyor_id": T_SURVEYOR, "assignment_id": assignment_id})
    if not check(st in (200, 201) and isinstance(srv, dict), f"start survey -> 201 (got {st})"):
        return
    survey_id = srv.get("survey_id") or srv.get("survey", {}).get("survey_id")
    if not check(bool(survey_id), "survey row has an id"):
        return
    say(f"survey {survey_id}")

    # -- 3. a poor device fix is warned about, not silently trusted ---------
    st, cap = api("POST", f"/survey/api/surveys/{survey_id}/location", {
        "latitude": LAT, "longitude": LON, "accuracy_m": 42.0,
        "source": "DEVICE_GNSS", "device": "test-runner", "captured_by": T_SURVEYOR,
        "set_entrance": True})
    check(st == 200, f"capture a ±42 m fix -> 200 (got {st})")
    check(bool(cap.get("poor_accuracy")), "a ±42 m fix is reported as poor")
    check(bool(cap.get("accuracy_warning")), "...with a warning the surveyor can see")
    conf = (cap.get("survey") or {}).get("mapping_confidence")
    check(conf != "HIGH", f"...and confidence is not HIGH on a poor fix (got {conf!r})")

    st, d = api("GET", f"/survey/api/properties/{T_PROP}/survey")
    qa_types = {q["issue_type"] for q in d.get("qa_issues", []) if q["status"] == "OPEN"}
    check("LARGE_GPS_DISPLACEMENT" in qa_types or "LOW_MAPPING_CONFIDENCE" in qa_types,
          "a poor fix raises a QA issue rather than passing quietly")

    # -- 4. a good fix ------------------------------------------------------
    st, cap = api("POST", f"/survey/api/surveys/{survey_id}/location", {
        "latitude": LAT, "longitude": LON, "accuracy_m": 3.2,
        "source": "DEVICE_GNSS", "device": "test-runner", "captured_by": T_SURVEYOR,
        "set_entrance": True})
    check(st == 200 and not cap.get("poor_accuracy"), "a ±3.2 m fix is accepted without warning")

    # -- 5. correcting the fix keeps BOTH coordinates -----------------------
    moved_lat, moved_lon = LAT + 0.00002, LON + 0.00002
    st, adj = api("POST", f"/survey/api/surveys/{survey_id}/location/adjust", {
        "latitude": moved_lat, "longitude": moved_lon, "adjusted_by": T_SURVEYOR})
    check(st == 200, f"adjust the entrance on the map -> 200 (got {st})")
    check((adj.get("moved_from_gnss_fix_m") or 0) > 0,
          "the API reports how far the entrance moved from the device fix")

    st, d = api("GET", f"/survey/api/properties/{T_PROP}/survey")
    s = d.get("survey") or {}
    check(abs((s.get("captured_latitude") or 0) - LAT) < 1e-9,
          "the original GNSS latitude is still stored unchanged")
    check(bool(s.get("manually_adjusted")), "the survey is marked manually adjusted")
    check(bool(s.get("adjusted_by")) and bool(s.get("adjustment_timestamp")),
          "...recording who moved it and when")
    ent = (d.get("geometry") or {}).get("entrance")
    check(ent is not None, "the corrected entrance is stored as the authoritative point")
    if ent and ent.get("geom"):
        elon, elat = ent["geom"]["coordinates"][:2]
        check(abs(elat - moved_lat) < 1e-6 and abs(elon - moved_lon) < 1e-6,
              "the entrance sits where the surveyor moved it, not on the raw fix")

    # -- 6. submission is blocked while the geometry is incomplete ----------
    st, ready = api("GET", f"/survey/api/surveys/{survey_id}/readiness")
    check(st == 200 and not ready.get("ready"),
          "readiness reports the survey is not submittable yet")
    check(any("frontage" in b.lower() or "zone" in b.lower()
              for b in ready.get("blockers", [])),
          "...and names the missing frontage / service zone")
    st, _ = api("POST", f"/survey/api/surveys/{survey_id}/submit", {"surveyor_id": T_SURVEYOR})
    check(st == 422, f"submitting an incomplete survey is refused (got {st})")

    # -- 6b. the surveyor edits the property attributes ---------------------
    st, prop = api("PATCH", f"/survey/api/properties/{T_PROP}", {
        "owner_name": "Edited Owner", "owner_phone": "9845000000",
        "street_name": "Test Street", "locality": "API test area",
        "pincode": "570014", "property_type": "COMMERCIAL",
        "service_entity_type": "SHOP", "updated_by": T_SURVEYOR})
    check(st == 200, f"PATCH property attributes -> 200 (got {st})")
    if st == 200:
        check(prop.get("service_entity_type") == "SHOP",
              "service entity type is stored")
        check("Test Street" in (prop.get("formatted_address") or ""),
              "the display address is rebuilt from its parts")

    # -- 6c. degenerate geometry is refused, not quietly stored -------------
    st, _ = api("PUT", f"/survey/api/surveys/{survey_id}/geometry",
                {"kind": "frontage", "geojson": degenerate_line(), "updated_by": T_SURVEYOR})
    if st == 200:
        st2, ready = api("GET", f"/survey/api/surveys/{survey_id}/readiness")
        check(any("2 distinct" in b for b in ready.get("blockers", [])),
              "a 2-identical-point frontage is reported as having <2 distinct points")
    else:
        check(st == 422, f"a degenerate frontage is refused outright (got {st})")

    st, _ = api("PUT", f"/survey/api/surveys/{survey_id}/geometry",
                {"kind": "service_zone", "geojson": degenerate_zone(),
                 "updated_by": T_SURVEYOR})
    if st == 200:
        st2, ready = api("GET", f"/survey/api/surveys/{survey_id}/readiness")
        blockers = " ".join(ready.get("blockers", []))
        check("3 distinct vertices" in blockers or "area" in blockers,
              "a zero-area service zone is reported as unusable")
    else:
        check(st == 422, f"a zero-area service zone is refused outright (got {st})")

    # -- 7. draw the rest ---------------------------------------------------
    for kind, geom, extra in (("frontage", frontage_geojson(), {"road_side": "SOUTH"}),
                              ("service_zone", zone_geojson(), {})):
        body = {"kind": kind, "geojson": geom, "updated_by": T_SURVEYOR}
        body.update(extra)
        st, _ = api("PUT", f"/survey/api/surveys/{survey_id}/geometry", body)
        check(st == 200, f"draw the {kind.replace('_', ' ')} -> 200 (got {st})")

    st, _ = api("PATCH", f"/survey/api/surveys/{survey_id}", {
        "mapping_confidence": "HIGH", "source_class": "VERIFIED_FIELD_SURVEY",
        "notes": "API workflow test.", "anomaly_type": []})
    check(st == 200, "save the quality section as a draft")

    st, ready = api("GET", f"/survey/api/surveys/{survey_id}/readiness")
    # A frontage photo is required, and at this point there is none.
    check(st == 200 and not ready.get("ready"),
          "readiness still blocks: the frontage photo has not been captured")
    check(any("photo" in b.lower() for b in ready.get("blockers", [])),
          "...and the missing photo is named as the blocker")

    # -- 7b. frontage photo ------------------------------------------------
    if _upload_photo(survey_id):
        ok("frontage photo uploaded and stored as DEVICE_CAMERA")
        st, d = api("GET", f"/survey/api/properties/{T_PROP}/survey")
        photos = d.get("photos") or []
        check(any(p.get("photo_type") == "FRONTAGE" for p in photos),
              "the frontage photo is linked to the property")
        check(any(p.get("capture_method") == "DEVICE_CAMERA" for p in photos),
              "a camera capture is distinguishable from an uploaded file")
        check(all(p.get("sha256") for p in photos if p.get("capture_method")),
              "every captured photo carries a SHA-256")

    st, ready = api("GET", f"/survey/api/surveys/{survey_id}/readiness")
    check(st == 200 and ready.get("ready"),
          f"readiness now reports submittable (blockers: {ready.get('blockers')})")
    g = ready.get("geometry") or {}
    check((g.get("frontage_unique_points") or 0) >= 2,
          f"PostGIS counts {g.get('frontage_unique_points')} distinct frontage points")
    check((g.get("zone_unique_points") or 0) >= 3,
          f"PostGIS counts {g.get('zone_unique_points')} distinct zone vertices")
    check(float(g.get("zone_area_m2") or 0) > 0,
          f"PostGIS measures the zone at {g.get('zone_area_m2')} m2")
    check(g.get("entrance_to_frontage_m") is not None,
          "PostGIS measures entrance-to-frontage distance in metres")

    # -- 7c. an entrance on the wrong building is blocked -------------------
    st, _ = api("PUT", f"/survey/api/surveys/{survey_id}/geometry",
                {"kind": "entrance", "geojson": far_entrance(), "updated_by": T_SURVEYOR})
    if st == 200:
        st2, ready2 = api("GET", f"/survey/api/surveys/{survey_id}/readiness")
        check(not ready2.get("ready") and
              any("from its own frontage" in b for b in ready2.get("blockers", [])),
              "an entrance ~600 m from its frontage blocks submission")
        st3, _ = api("POST", f"/survey/api/surveys/{survey_id}/submit",
                     {"surveyor_id": T_SURVEYOR})
        check(st3 == 422, f"...and the submit itself is refused (got {st3})")
    # put it back where it belongs
    api("PUT", f"/survey/api/surveys/{survey_id}/geometry",
        {"kind": "entrance", "geojson": entrance_geojson(moved_lat, moved_lon),
         "updated_by": T_SURVEYOR})

    # -- 8. submit ----------------------------------------------------------
    st, sub = api("POST", f"/survey/api/surveys/{survey_id}/submit", {"surveyor_id": T_SURVEYOR})
    check(st == 200, f"submit -> 200 (got {st})")
    st, d = api("GET", f"/survey/api/properties/{T_PROP}/survey")
    check((d.get("survey") or {}).get("survey_status") == "SUBMITTED",
          "the survey is now SUBMITTED")
    check(d["property"]["verification_status"] != "VERIFIED_FOR_OPERATION",
          "submitting alone does NOT clear the property for operation")

    # -- 9. reviewer sends it back ------------------------------------------
    st, _ = api("POST", f"/survey/api/surveys/{survey_id}/review", {
        "action": "CORRECTION_REQUIRED", "reviewer_id": T_REVIEWER,
        "review_notes": "Entrance looks like the neighbour's gate."})
    check(st == 200, f"return for correction -> 200 (got {st})")
    st, d = api("GET", f"/survey/api/properties/{T_PROP}/survey")
    check((d.get("survey") or {}).get("survey_status") == "CORRECTION_REQUIRED",
          "the survey came back as CORRECTION_REQUIRED")
    check(d["property"]["verification_status"] != "VERIFIED_FOR_OPERATION",
          "a returned survey leaves the property uncleared")

    # -- 10. a non-reviewer cannot review -----------------------------------
    st, _ = api("POST", f"/survey/api/surveys/{survey_id}/review", {
        "action": "APPROVE", "reviewer_id": T_SURVEYOR})
    check(st == 403, f"a surveyor cannot approve their own survey (got {st})")

    # -- 11. resubmit and approve -------------------------------------------
    st, _ = api("POST", f"/survey/api/surveys/{survey_id}/submit", {"surveyor_id": T_SURVEYOR})
    check(st == 200, "resubmit after correction")
    st, rev = api("POST", f"/survey/api/surveys/{survey_id}/review", {
        "action": "APPROVE", "reviewer_id": T_REVIEWER, "review_notes": "Matches the photo."})
    check(st == 200, f"approve -> 200 (got {st})")
    check((rev.get("property") or {}).get("verification_status") == "VERIFIED_FOR_OPERATION",
          "approval is what clears the property for operation")

    st, d = api("GET", f"/survey/api/properties/{T_PROP}/survey")
    g = d.get("geometry") or {}
    check(all((g.get(k) or {}).get("verified") for k in ("entrance", "frontage", "service_zone")),
          "approval marks entrance, frontage and service zone verified")

    # -- 12. geometry history -----------------------------------------------
    hist = d.get("history") or []
    check(len(hist) > 0, f"geometry edits left {len(hist)} history row(s)")
    check(any(h.get("geometry_kind") == "ENTRANCE" for h in hist),
          "the entrance's earlier version was kept, not overwritten in place")

    # -- 13. QA checks still run over the whole authority -------------------
    st, qa = api("POST", "/survey/api/qa/run", {})
    check(st == 200 and "checks" in (qa or {}), f"POST /qa/run -> 200 (got {st})")
    if st == 200:
        check(len(qa["checks"]) >= 8,
              f"{len(qa['checks'])} automated checks ran across the authority")

    st, issues = api("GET", "/survey/api/qa-issues?property_id=" + T_PROP + "&status=OPEN")
    if st == 200 and issues:
        st2, _ = api("PATCH", f"/survey/api/qa-issues/{issues[0]['issue_id']}",
                     {"status": "RESOLVED", "resolved_by": T_REVIEWER})
        check(st2 == 200, "a QA issue can be resolved through the API")

    # -- 13b. the REAL pilot properties are fully usable, read-only ---------
    # No synthetic rows exist any more, so these have to work on the data that
    # is actually there. Nothing below writes.
    st, d = api("GET", "/survey/api/properties/PROP-001/survey")
    if check(st == 200, f"GET the real PROP-001 survey detail -> 200 (got {st})"):
        check(d["property"]["property_id"] == "PROP-001", "...it is PROP-001")
        g = d.get("geometry") or {}
        check(all(g.get(k) for k in ("entrance", "frontage", "service_zone")),
              "...with its real entrance, frontage and service zone")
        check(len(d.get("photos") or []) >= 1, "...and its real frontage photo")
        t = d.get("thresholds") or {}
        check((t.get("gnss_accuracy_warn_m") or 0) > 0,
              f"...and the GNSS accuracy threshold ({t.get('gnss_accuracy_warn_m')} m)")
        check(t.get("entrance_proximity_max_m", 0) > t.get("entrance_proximity_ok_m", 0) > 0,
              "...and the proximity thresholds the submit gate uses")
        check(len((d.get("vocabulary") or {}).get("service_entity_type") or []) > 0,
              "...and the vocabulary the field form is built from")
        sid = (d.get("survey") or {}).get("survey_id")
        if sid:
            st2, r2 = api("GET", f"/survey/api/surveys/{sid}/readiness")
            check(st2 == 200 and (r2.get("thresholds") or {}).get("gnss_accuracy_warn_m"),
                  "readiness on a real pilot survey returns the GNSS threshold")

    # -- 13c(i). scope WITHOUT eager rows: the lazy path --------------------
    # An assignment may be created for its scope alone. No property_surveys
    # rows exist until a surveyor actually starts a property, and progress
    # still has to be right in the meantime.
    st, lazy_asg = api("POST", "/survey/api/assignments", {
        "admin_unit_id": T_UNIT_SOLO, "assigned_to": T_SURVEYOR,
        "include_properties": False})
    if check(st == 201, f"create an assignment with include_properties=false (got {st})"):
        lazy_id = lazy_asg["assignment_id"]
        check(lazy_asg.get("properties_in_scope", 0) >= 1,
              f"...its scope is real ({lazy_asg.get('properties_in_scope')} properties)")
        check(lazy_asg.get("survey_rows_created", 0) == 0,
              "...and it created no survey rows at all")
        lp = lazy_asg.get("assignment") or {}
        check(int(lp.get("outstanding_count") or 0) >= 1,
              f"...yet progress reports the work as outstanding, not done "
              f"({lp.get('outstanding_count')} outstanding of "
              f"{lp.get('total_properties')})")

        # starting the property is what creates the row - with the right link
        st2, lazy_row = api("POST", f"/survey/api/properties/{T_PROP_LAZY}/survey",
                            {"surveyor_id": T_SURVEYOR, "assignment_id": lazy_id})
        if check(st2 in (200, 201), f"starting it creates the survey row (got {st2})"):
            check(lazy_row.get("assignment_id") == lazy_id,
                  f"...carrying the correct assignment_id "
                  f"({lazy_row.get('assignment_id')})")
            check(lazy_row.get("survey_status") == "IN_PROGRESS",
                  f"...and it opens IN_PROGRESS (got {lazy_row.get('survey_status')})")

    # -- 13c(ii). a property with NO assignment at all still works ----------
    st, solo = api("POST", f"/survey/api/properties/{T_PROP_SOLO}/survey",
                   {"surveyor_id": T_SURVEYOR})          # no assignment_id at all
    solo_id = (solo or {}).get("survey_id") if isinstance(solo, dict) else None
    if check(st in (200, 201) and bool(solo_id),
             f"a survey starts with no assignment -> 201 (got {st})"):
        got_asg = (solo or {}).get("assignment_id")
        check(got_asg is None,
              "...and the survey records no assignment rather than inventing one"
              + (f" (got {got_asg!r} - is {T_PROP_SOLO} inside an assigned unit?)"
                 if got_asg else ""))
        st2, _ = api("POST", f"/survey/api/surveys/{solo_id}/location", {
            "latitude": LAT + 0.00036, "longitude": LON, "accuracy_m": 4.0,
            "source": "DEVICE_GNSS", "captured_by": T_SURVEYOR, "set_entrance": True})
        check(st2 == 200, f"...a device fix can be captured against it (got {st2})")
        st3, _ = api("PUT", f"/survey/api/surveys/{solo_id}/geometry",
                     {"kind": "frontage",
                      "geojson": {"type": "LineString",
                                  "coordinates": [[LON - D, LAT + 0.00036 - D],
                                                  [LON + D, LAT + 0.00036 - D]]},
                      "updated_by": T_SURVEYOR})
        check(st3 == 200, f"...and geometry can be saved against it (got {st3})")
        st4, ready = api("GET", f"/survey/api/surveys/{solo_id}/readiness")
        check(st4 == 200 and isinstance(ready.get("blockers"), list),
              "...and readiness reports on it without an assignment")

    # -- 13d. the module reports only real data -----------------------------
    st, ov = api("GET", "/survey/api/analytics/overview")
    if check(st == 200, f"GET analytics/overview -> 200 (got {st})"):
        t = ov["totals"]
        check(t["surveyed"] <= t["total_properties"] and t["verified"] <= t["surveyed"],
              "overview totals are internally consistent real counts")
    st, sc = api("GET", "/survey/api/analytics/scale")
    if check(st == 200, f"GET analytics/scale -> 200 (got {st})"):
        check(len(sc.get("path") or []) == 5,
              "the scale path names all five levels (pilot -> route -> ward -> zone -> city)")
        check(any(l["unit_type"] == "CITY" for l in sc.get("levels") or []),
              "...and the city level exists in the hierarchy")

    # -- 13e. repeated calls survive being PREPAREd -------------------------
    # psycopg promotes a query to a server-side PREPARE after 5 executions.
    # A parameter that cannot have its type inferred then breaks an endpoint
    # that had already worked five times, so one call proves nothing.
    prepared_ok = True
    for path in ("/survey/api/properties?limit=5",
                 "/survey/api/properties?status=APPROVED&limit=5",
                 "/survey/api/properties/PROP-001/survey",
                 "/survey/api/analytics/overview",
                 "/survey/api/surveys?survey_status=SUBMITTED&limit=5"):
        for _ in range(7):
            sc_, _b = api("GET", path)
            if sc_ != 200:
                prepared_ok = False
                bad(f"repeat call {sc_} on {path}")
                break
    check(prepared_ok, "survey endpoints survive being prepared (7 calls each)")

    # -- 14. the demo lane is untouched -------------------------------------
    st, lane = api("GET", "/properties?route_id="
                   + os.getenv("DEMO_ROUTE_ID", "ROUTE-DEMO-01"))
    check(st == 200 and len(lane) == 16,
          f"the 16-property demo lane is unchanged (got {len(lane) if st == 200 else '?'})")


# ---------------------------------------------------------------------------
def main() -> int:
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose

    if VERBOSE:
        print(f"\nSurvey workflow test against {API}\n")

    try:
        setup()
    except Exception as e:
        print(f"  \033[31mFAIL\033[0m  could not create test fixtures: {e}")
        return 1

    try:
        run()
    except Exception as e:  # a crash is a failure, but cleanup still has to happen
        bad(f"unhandled error: {type(e).__name__}: {e}")
    finally:
        try:
            teardown()
        except Exception as e:
            print(f"  warning: cleanup incomplete: {e}")

    total = len(PASS) + len(FAIL)
    if FAIL:
        print(f"\n{len(PASS)}/{total} survey workflow checks passed, "
              f"{len(FAIL)} failed\n")
        return 1
    if VERBOSE:
        print(f"\n{total}/{total} survey workflow checks passed\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

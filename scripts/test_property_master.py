#!/usr/bin/env python3
"""End-to-end test of the Property Registration workflow, over HTTP.

    python3 scripts/test_property_master.py            # quiet unless something fails
    python3 scripts/test_property_master.py -v         # print every step

What it proves, in the order a registration actually happens:

    duplicate check before saving -> property registered with a
    server-generated id -> lands in PENDING_SURVEY and NOT verified ->
    device location captured as a REFERENCE point, separate from survey
    geometry -> a poor fix is recorded and flagged rather than rejected or
    trusted -> property type and service entity type persist -> details
    edited with an audit trail -> geometry untouched by an administrative
    edit -> the field survey opens on the new property -> only the reviewer
    can produce VERIFIED_FOR_OPERATION.

It also checks, every run, that the real 16-property pilot lane is exactly
where it was and that no synthetic city rows have appeared.

Every property this test creates is deleted at the end, including when a
step fails, so the counts other checks assert on stay where they were.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

API = os.getenv("API", "http://127.0.0.1:8000").rstrip("/")
DEMO_ROUTE = os.getenv("DEMO_ROUTE_ID", "ROUTE-DEMO-01")

# Open ground ~2 km from the demo lane, so nothing registered here can fall
# inside a real service zone and perturb a lookup.
LAT, LON = 12.3130, 76.6600

# Properties this run created, newest first, for teardown.
CREATED: list[str] = []

PASS: list[str] = []
FAIL: list[str] = []
VERBOSE = False


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
    except Exception as e:
        return 0, str(e)


def db():
    from app.database import execute, fetch_all, fetch_one  # noqa: WPS433 (needs sys.path)
    return execute, fetch_all, fetch_one


PILOT_BEFORE: dict[str, str] = {}


def teardown() -> None:
    execute, _, _ = db()
    for pid in CREATED:
        for table in ("property_qa_issues", "property_geometry_history",
                      "property_photos", "property_entrances", "property_frontages",
                      "property_service_zones", "property_surveys",
                      "property_change_log"):
            execute(f"DELETE FROM {table} WHERE property_id = %s", (pid,))
        execute("DELETE FROM properties WHERE property_id = %s", (pid,))


# ===========================================================================
def run() -> None:
    _, fetch_all, fetch_one = db()

    # -- 0. baseline ------------------------------------------------------
    lane_before = fetch_one(
        "SELECT count(*) AS n FROM properties WHERE route_id = %s", (DEMO_ROUTE,))["n"]
    check(lane_before == 16,
          f"the real pilot lane holds 16 properties before anything is registered "
          f"(got {lane_before})")

    # Snapshot the pilot rows rather than asserting a hard-coded state. The
    # contract is "registration changed nothing", and only a before/after
    # comparison actually tests that - an assertion that PROP-001 equals some
    # literal tests the literal, and passes or fails for reasons that have
    # nothing to do with the code under test.
    global PILOT_BEFORE
    PILOT_BEFORE = {r["property_id"]: r["verification_status"] for r in fetch_all(
        "SELECT property_id, verification_status FROM properties "
        "WHERE route_id = %s ORDER BY property_id", (DEMO_ROUTE,))}
    say("pilot verification states: " +
        ", ".join(sorted({v for v in PILOT_BEFORE.values()})))

    st, summary = api("GET", "/properties/master/summary")
    check(st == 200, f"GET /properties/master/summary -> {st}")
    if st != 200:
        return
    base_total = summary["total"]
    say(f"baseline: {base_total} properties on record")

    # -- 1. vocabulary ----------------------------------------------------
    st, vocab = api("GET", "/properties/master/vocabulary")
    check(st == 200, f"GET /properties/master/vocabulary -> {st}")
    types = {t["value"] for t in vocab["property_types"]}
    entities = {t["value"] for t in vocab["service_entity_types"]}
    for want in ("INDEPENDENT_HOUSE", "APARTMENT", "SHOP", "COMMERCIAL_BUILDING",
                 "OFFICE", "SCHOOL", "HOSPITAL", "HOTEL", "MARKET",
                 "GATED_COMMUNITY", "INDUSTRIAL", "VACANT_PROPERTY", "OTHER"):
        check(want in types, f"property type vocabulary offers {want}")
    for want in ("INDIVIDUAL_PROPERTY", "BUILDING", "COMMON_COLLECTION_POINT",
                 "COMMERCIAL_COMPLEX", "COMMUNITY_COLLECTION_POINT", "OTHER"):
        check(want in entities, f"service entity vocabulary offers {want}")
    # The pilot rows still use the old vocabulary, so it must remain legal.
    check("RESIDENTIAL" in types,
          "the legacy property type the pilot rows use is still accepted")
    check(any(t.get("legacy") for t in vocab["property_types"]),
          "legacy values are marked as such so the create form can skip them")
    check(vocab["next_property_id"].startswith("PROP-"),
          f"the next property id is generated server-side ({vocab['next_property_id']})")
    check(vocab["thresholds"]["registration_accuracy_warn_m"] > 0,
          "the poor-accuracy threshold is configuration, not a magic number in the UI")
    check(len(vocab["admin_units"]) >= 1,
          "the administrative hierarchy is offered from the database, not hard-coded")

    # -- 2. duplicate check BEFORE saving ---------------------------------
    st, d = api("POST", "/properties/duplicate-check",
                {"house_number": "D001", "latitude": 12.2942563, "longitude": 76.6418649})
    check(st == 200, f"POST /properties/duplicate-check -> {st}")
    check(any(c["property_id"] == "PROP-001" for c in d["candidates"]),
          "a probe on top of PROP-001 with its house number surfaces PROP-001")
    check(d["decision"] == "POSSIBLE_DUPLICATE",
          f"...and is reported as a possible duplicate (got {d['decision']})")
    near = next((c for c in d["candidates"] if c["property_id"] == "PROP-001"), {})
    check(near.get("reasons"), "...with a human-readable reason, not just a score")
    check("merged automatically" in d["note"],
          "...and states plainly that nothing is merged automatically")

    st, d2 = api("POST", "/properties/duplicate-check",
                 {"house_number": "NOTHING-LIKE-THIS", "latitude": LAT, "longitude": LON})
    check(st == 200 and d2["decision"] == "CLEAR",
          "a probe on empty ground with a new house number is CLEAR")
    check(d2["candidates"] == [],
          "...and a legitimate new property is not blocked by phantom candidates")

    # -- 3. register ------------------------------------------------------
    body = {
        "house_number": "TEST-PM-01", "owner_name": "Property Master Test",
        "owner_phone": "9000000001", "street_name": "Test Ground Road",
        "locality": "Test Locality", "pincode": "570099",
        "property_type": "SHOP", "service_entity_type": "INDIVIDUAL_PROPERTY",
        "route_id": "ROUTE-TEST-PM", "created_by": "test-registrar",
        "captured_latitude": LAT, "captured_longitude": LON,
        "captured_accuracy_m": 6.0, "location_source": "DEVICE_GEOLOCATION",
    }
    st, created = api("POST", "/properties", body)
    if not check(st == 201, f"POST /properties -> {st} ({str(created)[:160]})"):
        return
    prop = created["property"]
    pid = prop["property_id"]
    CREATED.insert(0, pid)
    say(f"registered {pid}")

    check(re.fullmatch(r"PROP-\d{3,}", pid) is not None,
          f"the property id is server-generated in the existing PROP-nnn format ({pid})")
    check(pid == vocab["next_property_id"],
          f"...and is the id the form previewed ({vocab['next_property_id']})")
    check("property_id" not in body,
          "...because the client never sent one")
    check(prop["verification_status"] == "PENDING_SURVEY",
          f"a new property is PENDING_SURVEY (got {prop['verification_status']})")
    check(prop["verification_status"] != "VERIFIED_FOR_OPERATION",
          "...and is NOT cleared for operation just because it was registered")
    check(prop["property_type"] == "SHOP", "property type persisted")
    check(prop["service_entity_type"] == "INDIVIDUAL_PROPERTY",
          "service entity type persisted")
    check(prop["created_by"] == "test-registrar", "the acting user is recorded")
    check(prop["created_at"] is not None and prop["updated_at"] is not None,
          "created_at and updated_at are set automatically")
    check(prop["formatted_address"] and "TEST-PM-01" in prop["formatted_address"],
          "the display address is composed from its parts")
    check(prop["active"] is True, "a new record starts active")
    check(created["survey_url"].endswith(pid),
          "the response deep-links into the field survey for this property")

    # The registration fix is stored on the property, and NOT as geometry.
    check(abs(prop["captured_latitude"] - LAT) < 1e-9,
          "the registration fix is stored on the property")
    check(prop["location_source"] == "DEVICE_GEOLOCATION",
          "...tagged with where it came from")
    check(prop["captured_at"] is not None, "...and when it was taken")

    _, _, fetch_one2 = db()
    for table, label in (("property_entrances", "entrance"),
                         ("property_frontages", "frontage"),
                         ("property_service_zones", "service zone")):
        n = fetch_one2(f"SELECT count(*) AS n FROM {table} WHERE property_id = %s", (pid,))["n"]
        check(n == 0,
              f"registration did NOT create a {label} - that is the survey's job (got {n})")

    # -- 4. duplicate detection after the fact ----------------------------
    st, dup2 = api("POST", "/properties/duplicate-check",
                   {"house_number": "TEST-PM-01", "latitude": LAT, "longitude": LON})
    check(st == 200 and any(c["property_id"] == pid for c in dup2["candidates"]),
          "the newly registered property is itself found by a later duplicate check")
    st, dup3 = api("GET", f"/properties/{pid}/possible-duplicates")
    check(st == 200, f"GET /properties/{{id}}/possible-duplicates -> {st}")
    check(all(c["property_id"] != pid for c in dup3["candidates"]),
          "...and a property is never reported as a duplicate of itself")

    # -- 5. poor GPS accuracy ---------------------------------------------
    warn = vocab["thresholds"]["registration_accuracy_warn_m"]
    st, cap = api("POST", f"/properties/{pid}/capture-location",
                  {"latitude": LAT + 0.0001, "longitude": LON, "accuracy_m": warn + 45,
                   "source": "DEVICE_GEOLOCATION", "captured_by": "test-registrar"})
    check(st == 200, f"POST /properties/{{id}}/capture-location -> {st}")
    check(cap["accuracy"]["level"] == "POOR",
          f"a fix worse than {warn} m is reported as POOR (got {cap['accuracy']['level']})")
    check("field correction" in cap["accuracy"]["message"],
          "...with wording that points at the field survey, not at a rejection")
    check(cap["captured_accuracy_m"] == warn + 45,
          "...and the poor accuracy is RECORDED rather than discarded")
    check(cap["verification_status"] == "PENDING_SURVEY",
          "...and a poor fix never marks the property verified")

    st, cap2 = api("POST", f"/properties/{pid}/capture-location",
                   {"latitude": LAT, "longitude": LON, "accuracy_m": 4.0,
                    "source": "DEVICE_GEOLOCATION"})
    check(st == 200 and cap2["accuracy"]["level"] == "GOOD",
          "retaking the location replaces the poor fix with a good one")

    # -- 6. survey status seen from the registration side -----------------
    st, ss = api("GET", f"/properties/{pid}/survey-status")
    check(st == 200, f"GET /properties/{{id}}/survey-status -> {st}")
    check(ss["survey_status"] == "NOT_SURVEYED", "the new property is not surveyed yet")
    check(ss["gis"] == {"entrance": False, "frontage": False,
                        "service_zone": False, "frontage_photo": False},
          "every piece of GIS is reported missing")
    check(set(ss["missing"]) == {"entrance", "frontage", "service_zone", "frontage_photo"},
          "...and listed explicitly rather than left to be inferred")
    check(ss["ready_for_operation"] is False,
          "a registered-but-unsurveyed property is not ready for operation")
    check(ss["captured_location"] is not None,
          "the registration reference fix is visible from the survey-status view")
    check(ss["survey_url"] == f"/survey/field?property={pid}",
          "the field survey deep link carries the property")

    # -- 7. the field survey opens on it ----------------------------------
    st, fs = api("GET", f"/survey/api/properties/{pid}/survey")
    check(st == 200, f"the field survey API opens the new property -> {st}")
    if st == 200:
        check(fs["property"]["property_id"] == pid,
              "...and returns the property that was just registered")
        check(fs["survey"] is None,
              "...with no survey row yet, because nobody has started one")

    # -- 8. edit the administrative record --------------------------------
    st, upd = api("PATCH", f"/properties/{pid}",
                  {"owner_phone": "9000000099", "property_type": "COMMERCIAL_BUILDING",
                   "service_entity_type": "COMMERCIAL_COMPLEX",
                   "updated_by": "test-registrar-2"})
    check(st == 200, f"PATCH /properties/{{id}} -> {st}")
    check(upd["owner_phone"] == "9000000099", "the edited field persisted")
    check(upd["property_type"] == "COMMERCIAL_BUILDING", "property type can be corrected")
    check(upd["service_entity_type"] == "COMMERCIAL_COMPLEX",
          "service entity type can be corrected independently")
    check(upd["updated_by"] == "test-registrar-2", "the editing user is recorded")
    check(upd["verification_status"] == "PENDING_SURVEY",
          "an administrative edit does not change the verification state")

    st, hist = api("GET", f"/properties/{pid}/history")
    check(st == 200, f"GET /properties/{{id}}/history -> {st}")
    actions = [h["action"] for h in hist["items"]]
    fields = [h["field_name"] for h in hist["items"]]
    check("CREATED" in actions, "the audit trail records the registration")
    check("LOCATION_CAPTURED" in actions, "...and each location capture")
    check("owner_phone" in fields, "...and names the field that changed")
    phone_row = next((h for h in hist["items"] if h["field_name"] == "owner_phone"), {})
    check(phone_row.get("old_value") == "9000000001"
          and phone_row.get("new_value") == "9000000099",
          "...with the old and new value, so a change is reviewable")

    # An unknown vocabulary value is refused rather than written.
    st, _ = api("PATCH", f"/properties/{pid}", {"property_type": "NOT_A_REAL_TYPE"})
    check(st == 422, f"an unknown property type is refused (got {st})")
    st, _ = api("PATCH", f"/properties/{pid}", {"service_entity_type": "ALSO_NOT_REAL"})
    check(st == 422, f"an unknown service entity type is refused (got {st})")
    st, _ = api("GET", "/properties/PROP-DOES-NOT-EXIST/survey-status")
    check(st == 404, f"an unknown property is a 404, not a 500 (got {st})")

    # -- 9. deactivate / reactivate ---------------------------------------
    st, off = api("PATCH", f"/properties/{pid}",
                  {"active": False, "inactive_reason": "demolished (test)",
                   "updated_by": "test-registrar"})
    check(st == 200 and off["active"] is False, "a record can be deactivated")
    still = fetch_one2("SELECT count(*) AS n FROM properties WHERE property_id = %s",
                       (pid,))["n"]
    check(still == 1, "...and is still in the database - deactivated, never deleted")
    check(off["inactive_reason"] == "demolished (test)", "...with the reason recorded")
    st, on = api("PATCH", f"/properties/{pid}", {"active": True, "updated_by": "x"})
    check(st == 200 and on["active"] is True, "...and can be reactivated")
    check(on["inactive_reason"] is None,
          "...which clears the retirement reason rather than leaving it to confuse")

    # -- 10. the master list and its counts -------------------------------
    st, lst = api("GET", f"/properties/master?q={pid}")
    check(st == 200 and lst["total"] == 1,
          f"searching the master list by property id finds exactly one row "
          f"(got {lst.get('total') if st == 200 else st})")
    st, lst2 = api("GET", "/properties/master?q=9000000099")
    check(st == 200 and any(i["property_id"] == pid for i in lst2["items"]),
          "searching by phone number finds it")
    st, lst3 = api("GET", "/properties/master?q=Property%20Master%20Test")
    check(st == 200 and any(i["property_id"] == pid for i in lst3["items"]),
          "searching by owner name finds it")
    st, lst4 = api("GET", "/properties/master?verification_status=PENDING_SURVEY")
    check(st == 200 and any(i["property_id"] == pid for i in lst4["items"]),
          "filtering by verification status finds it")
    st, lst5 = api("GET", "/properties/master?route_id=ROUTE-TEST-PM")
    check(st == 200 and lst5["total"] == 1, "filtering by route finds it")

    st, s2 = api("GET", "/properties/master/summary")
    check(st == 200 and s2["total"] == base_total + 1,
          f"the master total went up by exactly one (got {s2.get('total')}, "
          f"was {base_total})")
    check(s2["pending_survey"] >= 1,
          "...and the new property counts as pending survey")
    check(s2["verified"] == summary["verified"],
          "...while the verified count is unchanged: registration verifies nothing")

    # -- 11. prepared-statement guard --------------------------------------
    # psycopg PREPAREs a query after five executions; a parameter PostgreSQL
    # cannot type at parse time only fails from the sixth call onward. Seven
    # calls per endpoint is the cheapest way to make that failure show up here
    # rather than in front of the Team Lead.
    for path in (f"/properties/master?q={pid}",
                 "/properties/master?verification_status=PENDING_SURVEY",
                 "/properties/master/summary",
                 f"/properties/{pid}/survey-status",
                 f"/properties/{pid}/possible-duplicates",
                 f"/properties/{pid}/master"):
        codes = {api("GET", path)[0] for _ in range(7)}
        check(codes == {200},
              f"{path.split('?')[0]} survives 7 consecutive calls (got {sorted(codes)})")

    # -- 12. verification_status is the reviewer's, and nobody else's ------
    # The registration screen must not be able to write it in either
    # direction: it cannot promote a property past a review it has not had,
    # and it cannot demote one that has been cleared for operation. The
    # allow-lists already omit the column; SYSTEM_OWNED makes it a rule
    # rather than a convention, and this proves the rule holds over HTTP.
    st, sneak = api("PATCH", f"/properties/{pid}",
                    {"verification_status": "VERIFIED_FOR_OPERATION",
                     "owner_email": "sneak@example.test", "updated_by": "test"})
    check(st == 200, f"a PATCH carrying verification_status is accepted -> {st}")
    check(sneak["owner_email"] == "sneak@example.test",
          "...its legitimate fields are applied")
    check(sneak["verification_status"] == "PENDING_SURVEY",
          f"...but registration cannot promote itself to verified "
          f"(got {sneak['verification_status']})")

    # The downgrade guard is exercised on the TEST's own property, promoted
    # here in SQL to stand in for a completed review. Running it against a
    # real pilot row would mean writing to pilot data to test that we do not
    # write to pilot data.
    execute2, _, _ = db()
    execute2("UPDATE properties SET verification_status = 'VERIFIED_FOR_OPERATION' "
             "WHERE property_id = %s", (pid,))
    st, edit = api("PATCH", f"/properties/{pid}",
                   {"owner_phone": "9000000077", "updated_by": "test-registrar"})
    check(st == 200 and edit["owner_phone"] == "9000000077",
          "an ordinary metadata edit on a verified property succeeds")
    check(st == 200 and edit["verification_status"] == "VERIFIED_FOR_OPERATION",
          f"...and does not disturb its verified status "
          f"(got {edit.get('verification_status')})")
    st, dg = api("PATCH", f"/properties/{pid}",
                 {"verification_status": "PENDING_SURVEY",
                  "owner_phone": "9000000078", "updated_by": "test-downgrade-attempt"})
    check(st == 200 and dg["verification_status"] == "VERIFIED_FOR_OPERATION",
          f"...and an edit explicitly asking to downgrade it is ignored "
          f"(got {dg.get('verification_status')})")
    gis_after = api("GET", f"/properties/{pid}/survey-status")[1]
    check(gis_after["gis"] == {"entrance": False, "frontage": False,
                               "service_zone": False, "frontage_photo": False},
          "...and no metadata edit ever invented geometry")
    execute2("UPDATE properties SET verification_status = 'PENDING_SURVEY' "
             "WHERE property_id = %s", (pid,))

    # -- 13. the pilot lane is exactly where it was ------------------------
    lane_after = fetch_one2(
        "SELECT count(*) AS n FROM properties WHERE route_id = %s", (DEMO_ROUTE,))["n"]
    check(lane_after == lane_before,
          f"the real pilot lane still holds {lane_before} properties (got {lane_after})")

    after = {r["property_id"]: r["verification_status"] for r in fetch_all(
        "SELECT property_id, verification_status FROM properties "
        "WHERE route_id = %s ORDER BY property_id", (DEMO_ROUTE,))}
    moved = {k: (PILOT_BEFORE[k], after.get(k))
             for k in PILOT_BEFORE if after.get(k) != PILOT_BEFORE[k]}
    check(not moved,
          f"no pilot property's verification_status moved during this run ({moved or 'none did'})")
    check(set(after) == set(PILOT_BEFORE),
          "...and the same 16 property ids are still there")

    synth = fetch_one2(
        "SELECT count(*) AS n FROM properties WHERE property_id ~ '^PROP-[0-9]{5}$'")["n"]
    check(synth == 0, f"no synthetic city properties were introduced (got {synth})")

    # -- 14. the pilot lane agrees with its own review record --------------
    # A property whose current survey is APPROVED, by a named reviewer, at a
    # recorded time, is by definition cleared for operation. If these two ever
    # disagree the database is telling two different stories about the same
    # property - which is exactly what database/reconcile_verification_status.sql
    # exists to fix.
    inconsistent = fetch_all(
        """
        SELECT p.property_id, p.verification_status, s.survey_status
        FROM properties p
        JOIN v_property_current_survey s ON s.property_id = p.property_id
        WHERE s.survey_status = 'APPROVED' AND s.review_status = 'APPROVED'
          AND s.reviewer_id IS NOT NULL AND s.reviewed_at IS NOT NULL
          AND p.verification_status <> 'VERIFIED_FOR_OPERATION'
          AND p.route_id = %s
        """, (DEMO_ROUTE,))
    check(not inconsistent,
          "every pilot property with an approved, reviewed survey is "
          f"VERIFIED_FOR_OPERATION ({[r['property_id'] for r in inconsistent] or 'all of them are'})")

    st, p1 = api("GET", "/properties/PROP-001/survey-status")
    check(st == 200, f"GET /properties/PROP-001/survey-status -> {st}")
    check(st == 200 and p1["missing"] == [],
          f"PROP-001 reports its real entrance, frontage, service zone and photo "
          f"(missing: {p1.get('missing') if st == 200 else '?'})")
    check(st == 200 and p1["gis"] == {"entrance": True, "frontage": True,
                                      "service_zone": True, "frontage_photo": True},
          "...each one read from its own table, not inferred from a status")
    check(st == 200 and p1["verification_status"] == PILOT_BEFORE.get("PROP-001"),
          f"...and PROP-001's verification_status is unchanged by this run "
          f"(was {PILOT_BEFORE.get('PROP-001')}, is "
          f"{p1.get('verification_status') if st == 200 else '?'})")
    check(st == 200 and p1["ready_for_operation"] ==
          (p1["verification_status"] == "VERIFIED_FOR_OPERATION"),
          "...and ready_for_operation follows verification_status, nothing else")


def main() -> int:
    global VERBOSE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose

    if VERBOSE:
        print(f"\nProperty Master test against {API}\n")

    try:
        run()
    except Exception as e:
        bad(f"unhandled error: {type(e).__name__}: {e}")
    finally:
        try:
            teardown()
        except Exception as e:
            print(f"  warning: cleanup incomplete: {e}")

    total = len(PASS) + len(FAIL)
    if FAIL:
        print(f"\n{len(PASS)}/{total} property master checks passed, "
              f"{len(FAIL)} failed")
        return 1
    print(f"\n{len(PASS)}/{total} property master checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

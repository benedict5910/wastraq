#!/usr/bin/env python3
"""
Phase 5 - simulated picker walk.

Replays a coordinate track (later: camera tracking + GNSS/IMU) through the
real GIS lookup, creates collection events, and raises one NOT_SEGREGATED
exception with evidence - the whole chain, end to end.

    vehicle -> approach -> service zone -> service zone
            -> boundary (deliberately ambiguous) -> return to vehicle

By default it walks the REAL surveyed lane from
`simulation/track_real_lane.json` (regenerate with
`python3 scripts/generate_real_lane.py`). If that file is absent it falls back
to the original synthetic Demo Lane track built in below, so the demo still
runs on a database that has not been migrated yet.

Usage:
    python3 simulation/simulate_picker.py
    python3 simulation/simulate_picker.py --api http://127.0.0.1:8000 --delay 0.6
    python3 simulation/simulate_picker.py --track synthetic
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
REAL_TRACK = os.path.join(HERE, "track_real_lane.json")

# Threshold above which the demo treats an association as good enough to
# record a collection. Below it we still show the decision, but collect
# nothing - the engine does not invent events.
COLLECT_CONFIDENCE = 0.90

PICKER = "PICKER-01"
TRACK_ID = "TRACK-SIM-001"
TRACK_NAME = "Demo Lane (synthetic)"

# Fallback: the original synthetic lane. (label, latitude, longitude, note)
SYNTHETIC_TRACK = [
    ("VEHICLE",  12.9698800, 77.5900500, "vehicle parked off the lane"),
    ("APPROACH", 12.9699800, 77.5900500, "picker steps out, crossing the road"),
    ("KERB",     12.9700100, 77.5901700, "on the kerb outside 12/2"),
    ("ZONE",     12.9700600, 77.5902800, "inside the service zone of 12/3"),
    ("ZONE",     12.9700650, 77.5903900, "inside the service zone of 12/4"),
    ("BOUNDARY", 12.9700250, 77.5904425, "standing on the 12/4 | 12/5 boundary"),
    ("RETURN",   12.9698800, 77.5906000, "walking back to the vehicle"),
]

BAR = "=" * 62


def load_track(which: str):
    """Prefer the generated real-lane track; fall back to the synthetic one."""
    global PICKER, TRACK_ID, TRACK_NAME
    if which != "synthetic" and os.path.exists(REAL_TRACK):
        with open(REAL_TRACK) as fh:
            d = json.load(fh)
        PICKER = d.get("picker_id", PICKER)
        TRACK_ID = d.get("track_id", TRACK_ID)
        TRACK_NAME = d.get("name", "real lane")
        return [(w["label"], w["latitude"], w["longitude"], w.get("note", ""))
                for w in d["waypoints"]]
    if which == "real":
        raise SystemExit(
            f"No real-lane track at {REAL_TRACK}.\n"
            "Generate it with: python3 scripts/generate_real_lane.py"
        )
    return SYNTHETIC_TRACK


class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def health(self) -> dict:
        return requests.get(self._url("/health/db"), timeout=10).json()

    def lookup(self, lat: float, lon: float) -> dict:
        r = requests.post(
            self._url("/gis/lookup"),
            json={"latitude": lat, "longitude": lon},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def create_event(self, **body) -> dict:
        r = requests.post(self._url("/collection-events"), json=body, timeout=10)
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code}: {r.text}")
        return r.json()

    def mark_non_segregated(self, event_id: str, **body) -> dict:
        r = requests.post(
            self._url(f"/collection-events/{event_id}/non-segregated"), json=body, timeout=10
        )
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code}: {r.text}")
        return r.json()

    def evidence(self, event_id: str) -> list[dict]:
        r = requests.get(self._url(f"/collection-events/{event_id}/evidence"), timeout=10)
        r.raise_for_status()
        return r.json()


def show_lookup(result: dict) -> None:
    print("Candidate properties:")
    if not result["candidates"]:
        print("  (none)")
    for c in result["candidates"]:
        where = "inside zone" if c["inside"] else f"{c['distance_m']:.2f} m away"
        print(f"  {c['property_id']}  [{c['zone_id']}]  {where}")
    print()
    print(f"Decision:   {result['decision']}")
    print(f"Best match: {result['property_id'] or '-'}")
    print(f"Confidence: {result['confidence']}")
    print(f"Method:     {result['method']}")
    print(f"Why:        {result['reason']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--delay", type=float, default=0.4, help="pause between waypoints (s)")
    ap.add_argument("--track", choices=("auto", "real", "synthetic"), default="auto",
                    help="which coordinate track to walk (default: real if generated)")
    args = ap.parse_args()

    track = load_track(args.track)
    api = Api(args.api)

    try:
        health = api.health()
    except Exception as exc:
        print(f"Cannot reach the backend at {args.api}: {exc}")
        print("Start it first:  uvicorn app.main:app --reload --app-dir backend")
        return 1

    print(BAR)
    print("WASTRAQ - simulated picker run")
    print(BAR)
    print(f"Lane:     {TRACK_NAME}")
    print(f"Backend:  {args.api}")
    print(f"PostGIS:  {health.get('postgis', '?').splitlines()[0]}")
    print(f"Loaded:   {health.get('counts')}")
    print()

    created: list[dict] = []
    seen_properties: set[str] = set()

    for step, (label, lat, lon, note) in enumerate(track, start=1):
        print(BAR)
        print(f"[{step}/{len(track)}] {label} - {note}")
        print(f"Picker {PICKER} detected")
        print()
        print("Position:")
        print(f"  {lat:.7f}, {lon:.7f}")
        print()

        result = api.lookup(lat, lon)
        show_lookup(result)
        print()

        if result["decision"] != "AUTO_ASSOCIATED":
            print("-> No collection recorded. The engine will not guess a property.")
        elif result["confidence"] < COLLECT_CONFIDENCE:
            print(
                f"-> Position understood but confidence {result['confidence']} < "
                f"{COLLECT_CONFIDENCE}; treated as pass-by, no collection recorded."
            )
        elif result["property_id"] in seen_properties:
            print(f"-> {result['property_id']} already collected on this run.")
        else:
            event = api.create_event(
                property_id=result["property_id"],
                picker_id=PICKER,
                track_id=TRACK_ID,
                collected=True,
                segregation_status="SEGREGATED",
                association_confidence=result["confidence"],
            )
            seen_properties.add(result["property_id"])
            created.append(event)
            print(f"Collection event created:  {event['event_id']}")
            print(f"Segregation:               {event['segregation_status']}")
            print(f"Review status:             {event['review_status']}")

        print()
        time.sleep(args.delay)

    # --- the exception path -------------------------------------------------
    print(BAR)
    print("NON-SEGREGATION EXCEPTION")
    print(BAR)

    if len(created) < 2:
        print("Not enough events created to demonstrate the exception path.")
        return 1

    target = created[-1]
    print(f"Picker {PICKER} taps the RFID tag at {target['property_id']}")
    print()

    updated = api.mark_non_segregated(
        target["event_id"],
        rfid_uid="RFID-DUMMY-A1B2C3D4",
        create_evidence=True,
        evidence_type="NON_SEGREGATION_PROOF",
    )

    print(f"Property:       {updated['property_id']}")
    print(f"Event:          {updated['event_id']}")
    print(f"Segregation:    {updated['segregation_status']}")
    print(f"RFID triggered: {updated['rfid_triggered']}")
    print(f"Review status:  {updated['review_status']}")
    print()
    print("Evidence saved:")
    for e in api.evidence(target["event_id"]):
        print(f"  {e['evidence_id']}  {e['evidence_type']}")
        print(f"    {e['file_path']}")
    print()

    print(BAR)
    print("RUN COMPLETE")
    print(BAR)
    for e in created:
        mark = "NOT_SEGREGATED" if e["event_id"] == target["event_id"] else "SEGREGATED"
        print(f"  {e['event_id']}  {e['property_id']}  {mark}")
    print()
    print(f"Dashboard: {args.api}/dashboard")
    return 0


if __name__ == "__main__":
    sys.exit(main())

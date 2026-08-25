#!/usr/bin/env python3
"""
Offline unit tests for the RealSense picker-tracking pipeline.

No camera, no pyrealsense2, no ultralytics, no database, no running backend.
Everything that is easy to get silently wrong - which pixel we measure, which
depth samples we trust, the pixel->metres maths, the smoothing, the track
lifetime and the API response shapes - is exercised here with synthetic
frames, so a regression shows up before anyone has to stand in front of a
camera.

Same convention as scripts/test_lookup_logic.py: plain script, prints a
table, exits non-zero on failure.

    python3 scripts/test_vision_logic.py
"""

from __future__ import annotations

import math
import os
import sys
import types
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

# --- stubs so the vision package imports with nothing installed -------------
# python-dotenv is a real dependency but not needed to read defaults.
if "dotenv" not in sys.modules:
    try:
        import dotenv  # noqa: F401
    except Exception:  # noqa: BLE001
        m = types.ModuleType("dotenv")
        m.load_dotenv = lambda *a, **k: None
        sys.modules["dotenv"] = m

# FastAPI only if it is genuinely absent (it is a real dependency of the
# project; this keeps the test runnable on a bare interpreter too).
try:  # noqa: SIM105
    import fastapi  # noqa: F401
except Exception:  # noqa: BLE001
    fa = types.ModuleType("fastapi")

    class _Router:
        def __init__(self, *a, **k):
            self.routes = []

        def _deco(self, *a, **k):
            def wrap(fn):
                self.routes.append(fn)
                return fn
            return wrap

        get = post = delete = put = patch = _deco

    class _HTTPException(Exception):
        def __init__(self, status_code=500, detail=""):
            self.status_code, self.detail = status_code, detail
            super().__init__(detail)

    def _Query(default=None, **k):
        return default

    fa.APIRouter = _Router
    fa.HTTPException = _HTTPException
    fa.Query = _Query
    resp = types.ModuleType("fastapi.responses")

    class _Response:
        def __init__(self, content=None, media_type=None, headers=None, **k):
            self.content, self.media_type, self.headers = content, media_type, headers

    class _Streaming(_Response):
        def __init__(self, content=None, media_type=None, headers=None, **k):
            super().__init__(content, media_type, headers)

    resp.Response = _Response
    resp.StreamingResponse = _Streaming
    fa.responses = resp
    sys.modules["fastapi"] = fa
    sys.modules["fastapi.responses"] = resp

from app.vision import geometry as geo          # noqa: E402
from app.vision import tracking as trk          # noqa: E402

FAILS: list[str] = []
CHECKS = 0
SECTION = ""


def section(name: str) -> None:
    global SECTION
    SECTION = name
    print(f"\n\033[1m{name}\033[0m")


def check(label: str, cond: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok    {label}")
    else:
        FAILS.append(f"{SECTION} / {label}" + (f" - {detail}" if detail else ""))
        print(f"  \033[31mFAIL\033[0m  {label}" + (f"  ({detail})" if detail else ""))


def near(a, b, tol=1e-6) -> bool:
    return a is not None and abs(a - b) <= tol


# ---------------------------------------------------------------- helpers --

INTR = geo.Intrinsics(width=640, height=480, ppx=320.0, ppy=240.0,
                      fx=600.0, fy=600.0, model=geo.DISTORTION_NONE)


def frame(width=640, height=480, fill=0):
    """A plain list-of-lists depth frame - proves geometry.py needs no numpy."""
    return [[fill] * width for _ in range(height)]


def paint(f, u, v, half, value):
    for y in range(v - half, v + half + 1):
        if 0 <= y < len(f):
            for x in range(u - half, u + half + 1):
                if 0 <= x < len(f[0]):
                    f[y][x] = value


# ============================================================== 1. intrinsics
section("1. Intrinsics and deprojection")

x, y, z = geo.deproject(INTR, 320.0, 240.0, 3.0)
check("principal point deprojects to x=0, y=0, z=depth",
      near(x, 0.0) and near(y, 0.0) and near(z, 3.0), f"got {x:.4f},{y:.4f},{z:.4f}")

xr, _, _ = geo.deproject(INTR, 420.0, 240.0, 3.0)
xl, _, _ = geo.deproject(INTR, 220.0, 240.0, 3.0)
check("pixel RIGHT of centre gives +x", xr > 0, f"x={xr:.4f}")
check("pixel LEFT of centre gives -x", xl < 0, f"x={xl:.4f}")
check("left/right are symmetric about the centre", near(xr, -xl, 1e-9))
check("+x magnitude is depth * (du/fx)", near(xr, 3.0 * 100.0 / 600.0, 1e-9), f"{xr}")

_, yd, _ = geo.deproject(INTR, 320.0, 340.0, 3.0)
check("pixel BELOW centre gives +y (native: +y is down)", yd > 0, f"y={yd:.4f}")

za, zb = geo.deproject(INTR, 400.0, 300.0, 2.0)[2], geo.deproject(INTR, 400.0, 300.0, 5.0)[2]
check("z is exactly the measured depth", near(za, 2.0) and near(zb, 5.0))

# --- distortion models -----------------------------------------------------
# The colour stream on a D400 reports inverse_brown_conrady (usually with
# zero coefficients after factory rectification); the depth stream reports
# none. Both paths are checked.

COEFFS = (0.12, -0.05, 0.001, 0.002, 0.01)
IBC = geo.Intrinsics(640, 480, 320.0, 240.0, 600.0, 600.0,
                     geo.DISTORTION_INVERSE_BROWN_CONRADY, COEFFS)
IBC0 = geo.Intrinsics(640, 480, 320.0, 240.0, 600.0, 600.0,
                      geo.DISTORTION_INVERSE_BROWN_CONRADY, (0, 0, 0, 0, 0))

check("zero coefficients make inverse_brown_conrady identical to 'none'",
      geo.deproject(IBC0, 410.0, 190.0, 2.5) == geo.deproject(INTR, 410.0, 190.0, 2.5))


def rsutil_ibc(intr, u, v, d):
    """Literal transcription of rsutil.h's INVERSE_BROWN_CONRADY branch.

    Deliberately a second, independent copy: if geometry.deproject ever
    drifts from librealsense - a swapped coeff index, an added iteration -
    this disagrees loudly instead of quietly changing everyone's metres.
    """
    x = (u - intr.ppx) / intr.fx
    y = (v - intr.ppy) / intr.fy
    c = list(intr.coeffs)
    r2 = x * x + y * y
    f = 1 + c[0] * r2 + c[1] * r2 * r2 + c[4] * r2 * r2 * r2
    ux = x * f + 2 * c[2] * x * y + c[3] * (r2 + 2 * x * x)
    uy = y * f + 2 * c[3] * x * y + c[2] * (r2 + 2 * y * y)
    return (d * ux, d * uy, d)


got = geo.deproject(IBC, 455.0, 168.0, 3.10)
want = rsutil_ibc(IBC, 455.0, 168.0, 3.10)
check("inverse_brown_conrady matches rsutil.h exactly",
      max(abs(a - b) for a, b in zip(got, want)) < 1e-12, f"{got} vs {want}")

# brown_conrady deprojection IS a true inverse - iterate then reproject.
BC = geo.Intrinsics(640, 480, 320.0, 240.0, 600.0, 600.0,
                    geo.DISTORTION_BROWN_CONRADY, COEFFS)


def project_bc(intr, p):
    """Standard Brown-Conrady forward distortion (radial + tangential)."""
    px, py = p[0] / p[2], p[1] / p[2]
    c = list(intr.coeffs)
    r2 = px * px + py * py
    f = 1 + c[0] * r2 + c[1] * r2 * r2 + c[4] * r2 * r2 * r2
    dx = px * f + 2 * c[2] * px * py + c[3] * (r2 + 2 * px * px)
    dy = py * f + 2 * c[3] * px * py + c[2] * (r2 + 2 * py * py)
    return (dx * intr.fx + intr.ppx, dy * intr.fy + intr.ppy)


P = (0.42, -0.18, 3.10)
pu, pv = project_bc(BC, P)
back = geo.deproject(BC, pu, pv, P[2])
err = max(abs(back[0] - P[0]), abs(back[1] - P[1]))
check("brown_conrady deprojection inverts the forward distortion", err < 1e-6,
      f"max axis error {err:.2e} m")

try:
    geo.deproject(
        geo.Intrinsics(640, 480, 320.0, 240.0, 600.0, 600.0,
                       geo.DISTORTION_MODIFIED_BROWN_CONRADY, COEFFS),
        400.0, 300.0, 3.0)
    check("a forward-only distortion model is refused, not fudged", False,
          "no exception raised")
except geo.UndeprojectableModel:
    check("a forward-only distortion model is refused, not fudged", True)

check("Intrinsics.to_dict is JSON-shaped",
      isinstance(INTR.to_dict()["coeffs"], list) and INTR.to_dict()["fx"] == 600.0)


# ================================================================= 2. anchors
section("2. Anchor pixel selection")

BBOX = (200.0, 100.0, 280.0, 400.0)          # 80 x 300 px person
gu, gv = geo.ground_anchor(BBOX, 640, 480, 0.06)
check("ground anchor is horizontally centred", gu == 240, f"u={gu}")
check("ground anchor sits above the bottom edge", 100 < gv < 400, f"v={gv}")
check("ground anchor is inset by ~6% of box height", gv == round(400 - 0.06 * 300),
      f"v={gv}")

tu, tv = geo.torso_anchor(BBOX, 640, 480)
check("torso anchor is centred and in the upper body", tu == 240 and tv == 220,
      f"({tu},{tv})")
check("torso anchor is above the ground anchor", tv < gv)

eu, ev = geo.ground_anchor((600.0, 400.0, 700.0, 600.0), 640, 480)
check("anchor is clamped inside the frame", 0 <= eu < 640 and 0 <= ev < 480,
      f"({eu},{ev})")


# =========================================================== 3. depth sampling
section("3. Robust depth sampling")

SCALE = 0.001   # D400 default: raw uint16 in millimetres

f0 = frame()
s = geo.sample_depth(f0, 320, 240, depth_scale=SCALE)
check("an all-zero window is INVALID, not 0 metres away",
      s.valid is False and s.depth_m is None, s.reason)
check("invalid sample says why", s.reason == "TOO_FEW_VALID_PIXELS", s.reason)

f1 = frame()
paint(f1, 320, 240, 5, 3000)
s = geo.sample_depth(f1, 320, 240, depth_scale=SCALE)
check("a clean window returns the depth in metres", near(s.depth_m, 3.0, 1e-6), str(s.depth_m))
check("valid sample reports how many pixels it used", s.used_count >= 6, str(s.used_count))

# a window straddling the subject (3 m) and the wall behind (6 m)
f2 = frame()
for yy in range(235, 246):
    for xx in range(316, 325):
        f2[yy][xx] = 3000 if xx <= 320 else 6000
s = geo.sample_depth(f2, 320, 240, depth_scale=SCALE)
check("background is rejected, not averaged in",
      s.valid and near(s.depth_m, 3.0, 0.05), f"got {s.depth_m}")
check("the mid-point between the two surfaces is never returned",
      not near(s.depth_m or 0, 4.5, 0.4), str(s.depth_m))

f3 = frame()
paint(f3, 320, 240, 1, 3000)          # only 9 valid pixels
s = geo.sample_depth(f3, 320, 240, depth_scale=SCALE, min_valid=20)
check("too few valid pixels -> invalid", not s.valid, s.reason)

f4 = frame()
paint(f4, 320, 240, 5, 40)            # 0.04 m: below the near limit
s = geo.sample_depth(f4, 320, 240, depth_scale=SCALE, min_depth_m=0.3)
check("depth below the near clip is rejected", not s.valid, str(s.depth_m))

f5 = frame()
paint(f5, 320, 240, 5, 30000)         # 30 m: beyond the far clip
s = geo.sample_depth(f5, 320, 240, depth_scale=SCALE, max_depth_m=10.0)
check("depth beyond the far clip is rejected", not s.valid, str(s.depth_m))

f6 = frame()
paint(f6, 320, 240, 5, 2000)
f6[240][320] = 0                      # one hole right on the anchor
s = geo.sample_depth(f6, 320, 240, depth_scale=SCALE)
check("a hole exactly on the anchor pixel is survivable",
      s.valid and near(s.depth_m, 2.0, 1e-6), str(s.depth_m))

f7 = frame()
paint(f7, 5, 5, 5, 2500)
s = geo.sample_depth(f7, 2, 2, depth_scale=SCALE)
check("a window clipped by the frame edge still works", s.valid, s.reason)

s = geo.sample_depth(frame(), 320, 240, depth_scale=SCALE)
check("invalid samples carry no depth value", s.depth_m is None)


# ======================================================= 4. measure_person
section("4. measure_person - anchor fallback")

fa_ = frame()
gu, gv = geo.ground_anchor(BBOX, 640, 480)
paint(fa_, gu, gv, 6, 3500)
sample, pos = geo.measure_person(fa_, BBOX, INTR, depth_scale=SCALE)
check("ground anchor used when the feet have depth",
      sample.valid and sample.source == geo.GROUND, sample.source)
check("position is returned in metres", pos is not None and near(pos[2], 3.5, 1e-6),
      str(pos))
check("a box left of centre gives -x",
      geo.measure_person(fa_, BBOX, INTR, depth_scale=SCALE)[1][0] < 0)

fb = frame()
tu, tv = geo.torso_anchor(BBOX, 640, 480)
paint(fb, tu, tv, 6, 3200)            # feet missing, torso present
sample, pos = geo.measure_person(fb, BBOX, INTR, depth_scale=SCALE)
check("falls back to the torso anchor when the feet have no depth",
      sample.valid and sample.source == geo.TORSO, sample.source)
check("the fallback is reported, never hidden", sample.source == "TORSO")

sample, pos = geo.measure_person(frame(), BBOX, INTR, depth_scale=SCALE)
check("no usable depth anywhere -> no position invented",
      pos is None and not sample.valid, str(pos))
check("the failure reason names both attempts", "TORSO_" in sample.reason, sample.reason)


# ================================================================ 5. smoothing
section("5. Smoothing")

e = trk.Ema3(alpha=0.5, max_jump_m=1.0)
check("first sample is taken as-is (no warm-up lag)",
      e.update((1.0, 0.0, 3.0)) == (1.0, 0.0, 3.0))

v = e.update((1.4, 0.0, 3.0))    # 0.4 m step: below the 1.0 m gate
check("a small step is smoothed, not jumped to", near(v[0], 1.2, 1e-9), str(v))
check("...and is not flagged as a snap", e.snapped is False)

e2 = trk.Ema3(alpha=0.5, max_jump_m=1.0)
e2.update((0.0, 0.0, 3.0))
v = e2.update((5.0, 0.0, 3.0))
check("a large step snaps to raw instead of lagging", near(v[0], 5.0, 1e-9), str(v))
check("the snap is flagged for debugging", e2.snapped is True)

e3 = trk.Ema3(alpha=0.4, max_jump_m=10.0)
e3.update((0.0, 0.0, 0.0))
for _ in range(25):
    out = e3.update((1.0, 1.0, 1.0))
check("smoothing converges on a held position", near(out[0], 1.0, 1e-3), str(out))

e4 = trk.Ema3(alpha=0.4, max_jump_m=10.0)
e4.update((0.0, 0.0, 0.0))
steps = 0
while steps < 100:
    steps += 1
    o = e4.update((1.0, 0.0, 0.0))
    if o[0] > 0.9:
        break
check("smoothing reaches 90% within ~6 frames (0.2 s at 30 fps)", steps <= 6,
      f"{steps} frames")


# ====================================================== 6. track + trajectory
section("6. Track state and trajectory buffer")

t0 = trk.utcnow()
st = trk.TrackState(track_id=1, first_seen=t0, last_seen=t0)
check("label is TRACK-01 style", st.label == "TRACK-01", st.label)
check("label is zero-padded for two digits", trk.track_label(7) == "TRACK-07")


class FakeSample:
    def __init__(self, valid=True, depth=3.0, source="GROUND", reason="OK"):
        self.valid, self.depth_m, self.source, self.reason = valid, depth, source, reason
        self.anchor = (320, 400)


for i in range(10):
    ts = t0 + timedelta(seconds=i * 0.1)
    st.observe(ts, (10, 10, 50, 200), 0.9, FakeSample(depth=3.0 + i * 0.01),
               (0.1 * i, 1.0, 3.0), trajectory_seconds=1.0)

check("trajectory accumulates points", len(st.trajectory) > 0, str(len(st.trajectory)))
check("trajectory is trimmed to its time window",
      all((st.trajectory[-1][0] - p[0]).total_seconds() <= 1.0 for p in st.trajectory))
check("trajectory is in time order",
      all(st.trajectory[i][0] <= st.trajectory[i + 1][0] for i in range(len(st.trajectory) - 1)))
check("trajectory stores x and z only (top-down)", len(st.trajectory[0]) == 3)

before = len(st.trajectory)
st.observe(t0 + timedelta(seconds=1.1), (10, 10, 50, 200), 0.9,
           FakeSample(valid=False, depth=None, reason="TOO_FEW_VALID_PIXELS"), None,
           trajectory_seconds=1.0)
check("a depth-invalid frame keeps the track alive", st.frames == 11, str(st.frames))
check("...and reports no position rather than a stale one",
      st.raw is None and st.smoothed is None)
check("...and does not extend the trajectory with a guess",
      len(st.trajectory) <= before)
check("...and counts the miss", st.depth_misses == 1, str(st.depth_misses))

st.observe(t0 + timedelta(seconds=1.2), (10, 10, 50, 200), 0.9, FakeSample(),
           (0.9, 1.0, 3.0), trajectory_seconds=1.0)
check("recovery restores a position", st.smoothed is not None)
check("the miss counter resets on recovery", st.depth_misses == 0)

d = st.to_dict()
for key in ("track_id", "label", "timestamp", "bbox", "detection_confidence",
            "depth_valid", "depth_m", "camera_position_raw",
            "camera_position_smoothed", "trajectory"):
    check(f"to_dict carries {key!r}", key in d)
check("raw and smoothed are both kept for debugging",
      d["camera_position_raw"] is not None and d["camera_position_smoothed"] is not None)
check("positions serialize as x_m/y_m/z_m",
      set(d["camera_position_raw"]) == {"x_m", "y_m", "z_m"})
check("trajectory points serialize as t/x_m/z_m",
      set(d["trajectory"][0]) == {"t", "x_m", "z_m"})
check("to_dict(include_trajectory=False) omits the trail",
      "trajectory" not in st.to_dict(include_trajectory=False))


# =============================================================== 7. TrackStore
section("7. TrackStore lifetime")

store = trk.TrackStore(live_ttl_s=1.0, retire_s=3.0, trajectory_seconds=5.0)
now = trk.utcnow()
store.observe(1, now, (0, 0, 10, 10), 0.9, FakeSample(), (0.0, 1.0, 3.0))
store.observe(2, now, (0, 0, 10, 10), 0.8, FakeSample(), (1.0, 1.0, 4.0))
check("multiple simultaneous tracks are independent", len(store.live(now)) == 2)
check("track ids are not renumbered", {s.track_id for s in store.live(now)} == {1, 2})

later = now + timedelta(seconds=1.5)
store.observe(1, later, (0, 0, 10, 10), 0.9, FakeSample(), (0.2, 1.0, 3.0))
live = store.live(later)
check("a track not seen recently drops out of 'live'",
      [s.track_id for s in live] == [1], str([s.track_id for s in live]))
check("...but its history is still held for recovery",
      store.get(2) is not None)

snap = store.snapshot(now=later)
check("snapshot returns plain dicts", isinstance(snap, list) and snap and isinstance(snap[0], dict))
check("snapshot is live-only by default", len(snap) == 1, str(len(snap)))
check("snapshot(live_only=False) includes retained tracks",
      len(store.snapshot(now=later, live_only=False)) == 2)

much_later = now + timedelta(seconds=10)
store.prune(much_later)
check("a track past the retire window is dropped", store.get(2) is None)

store2 = trk.TrackStore(max_tracks=3, retire_s=999)
for i in range(10):
    store2.observe(i, trk.utcnow(), (0, 0, 10, 10), 0.5, FakeSample(), (0.0, 0.0, 1.0))
check("the store is bounded (no slow leak in the backend process)",
      len(store2.all()) <= 3, str(len(store2.all())))


# ================================================== 8. API response contracts
section("8. API response shapes")

from app.vision import api as vapi              # noqa: E402
from app.vision.pipeline import pipeline        # noqa: E402

status = vapi.vision_status()
for key in ("camera_connected", "color_stream_active", "depth_stream_active",
            "detector_loaded", "tracker_active", "fps", "latest_frame_timestamp"):
    check(f"/vision/status has {key!r}", key in status)
check("status with no camera reports camera_connected=false",
      status["camera_connected"] is False)
check("status with no camera does NOT raise", status["state"] == "STOPPED", status["state"])
check("status exposes the coordinate convention", "x_m" in status["convention"])
check("status exposes the tuning it is actually using",
      status["config"]["depth_window_px"] > 0)

tracks = vapi.vision_tracks(trajectory=True, include_stale=False)
for key in ("camera_connected", "state", "frame_timestamp", "fps", "count",
            "convention", "tracks"):
    check(f"/vision/tracks has {key!r}", key in tracks)
check("no camera -> zero tracks, not an error", tracks["count"] == 0)

# inject a synthetic track and re-read through the route function
pipeline.store.clear()
pipeline.store.observe(1, trk.utcnow(), (100, 50, 180, 300), 0.91,
                       FakeSample(depth=3.72), (0.84, 1.1, 3.72))
tracks = vapi.vision_tracks(trajectory=True, include_stale=False)
check("an injected track is reported", tracks["count"] == 1, str(tracks["count"]))
row = tracks["tracks"][0]
check("the phase-1 demo fields are all present",
      row["label"] == "TRACK-01"
      and row["detection_confidence"] == 0.91
      and row["depth_valid"] is True
      and near(row["depth_m"], 3.72, 1e-9)
      and near(row["camera_position_smoothed"]["x_m"], 0.84, 1e-9)
      and near(row["camera_position_smoothed"]["z_m"], 3.72, 1e-9),
      str(row))

# pydantic contract - the models must accept exactly what the routes emit
try:
    from app.vision.schemas import TracksResponse, VisionStatus
    VisionStatus(**status)
    parsed = TracksResponse(**tracks)
    check("TracksResponse validates the /vision/tracks payload", parsed.count == 1)
    check("VisionStatus validates the /vision/status payload", True)
    check("TrackOut round-trips the track dict",
          parsed.tracks[0].label == "TRACK-01"
          and parsed.tracks[0].camera_position_smoothed is not None)
except ImportError as exc:                                   # pragma: no cover
    check("pydantic models validate the payloads", False, f"pydantic missing: {exc}")

single = vapi.vision_track(1)
check("/vision/tracks/{id} returns one track", single["track_id"] == 1)
try:
    vapi.vision_track(999)
    check("/vision/tracks/{id} 404s for an unknown id", False, "no exception raised")
except Exception as exc:                                     # noqa: BLE001
    check("/vision/tracks/{id} 404s for an unknown id",
          getattr(exc, "status_code", None) == 404, str(exc))

# calibration: expected vs measured
pipeline.calibration_samples.clear()
s = vapi.calibration_capture(
    types.SimpleNamespace(label="3 m forward", expected_x_m=0.0, expected_z_m=3.0,
                          track_id=1))
check("calibration reports the measured position", near(s["measured_z_m"], 3.72, 1e-9))
check("calibration reports the error, signed", near(s["error_z_m"], 0.72, 1e-9), str(s))
check("calibration reports total error",
      near(s["error_total_m"], math.hypot(0.84, 0.72), 1e-4), str(s["error_total_m"]))
check("calibration samples are listed", len(vapi.calibration_list()) == 1)
check("calibration samples can be cleared", vapi.calibration_clear()["cleared"] == 1)

pipeline.store.clear()
s = vapi.calibration_capture(
    types.SimpleNamespace(label="nobody there", expected_x_m=0.0, expected_z_m=2.0,
                          track_id=None))
check("calibration with no live track says so rather than inventing a number",
      s["measured_x_m"] is None and "no live track" in s["note"], str(s))
pipeline.calibration_samples.clear()


# ============================================== 9. existing system untouched
section("9. Existing system safety")

import app.config as appcfg                     # noqa: E402
for key in ("SEARCH_RADIUS_M", "AUTO_MAX_DISTANCE_M", "AMBIGUITY_MARGIN_M",
            "MIN_AUTO_CONFIDENCE", "METRIC_SRID", "DEMO_ROUTE_ID",
            "GNSS_ACCURACY_WARN_M", "DUPLICATE_RADIUS_M"):
    check(f"config still exposes {key!r}", hasattr(appcfg.settings, key))
check("the demo lane route id is unchanged",
      appcfg.settings.DEMO_ROUTE_ID == "ROUTE-DEMO-01", appcfg.settings.DEMO_ROUTE_ID)
check("the camera never starts on its own", appcfg.settings.VISION_AUTOSTART is False)
check("importing the vision package pulls in no hardware SDK",
      "pyrealsense2" not in sys.modules and "ultralytics" not in sys.modules)
check("the vision pipeline is not running after an import", pipeline.running is False)


# ========================================== 10. simulated end-to-end pipeline
section("10. Simulated end-to-end (synthetic RGB-D, no camera)")

try:
    import numpy as np
    import cv2  # noqa: F401
    HAVE_CV = True
except Exception as exc:  # noqa: BLE001
    HAVE_CV = False
    print(f"  \033[33mskip\033[0m  numpy/cv2 not installed here ({exc});"
          " run this again after ./scripts/add_realsense_picker_tracking.sh")

if HAVE_CV:
    from app.vision.pipeline import VisionPipeline

    W, H, WALL_MM, SCALE_ = 640, 480, 8000, 0.001

    def scene(person_px, person_depth_m, box_w=80, box_h=300):
        """A synthetic aligned RGB-D frameset: one person in front of a wall."""
        colour = np.full((H, W, 3), 40, dtype=np.uint8)
        depth = np.full((H, W), WALL_MM, dtype=np.uint16)
        cx, feet_v = person_px
        x1, x2 = int(cx - box_w / 2), int(cx + box_w / 2)
        y1, y2 = int(feet_v - box_h), int(feet_v)
        depth[max(y1, 0):min(y2, H), max(x1, 0):min(x2, W)] = int(person_depth_m * 1000)
        colour[max(y1, 0):min(y2, H), max(x1, 0):min(x2, W)] = (200, 180, 160)
        return colour, depth, (float(x1), float(y1), float(x2), float(y2))

    class FakeDetector:
        """Stands in for YOLO+ByteTrack: same contract, no model download."""
        def __init__(self):
            self.bbox = None
            self.calls = 0

        def track(self, frame):
            self.calls += 1
            return [] if self.bbox is None else [
                {"track_id": 1, "bbox": self.bbox, "confidence": 0.91}]

    p = VisionPipeline()
    p.intrinsics = INTR
    p.depth_scale = SCALE_
    det = FakeDetector()

    def step(cx, depth_m):
        colour, depth, bbox = scene((cx, 400), depth_m)
        det.bbox = bbox
        p._process(colour, depth, det)
        return p.store.get(1)

    # Test A - dead centre, 3 m away
    for _ in range(6):
        t = step(320, 3.0)
    check("A: a person 3 m dead ahead reads X ~ 0", abs(t.smoothed[0]) < 0.05,
          f"X={t.smoothed[0]:+.3f}")
    check("A: ...and Z ~ 3 m", abs(t.smoothed[2] - 3.0) < 0.05, f"Z={t.smoothed[2]:.3f}")
    check("A: the track is TRACK-01 with valid depth",
          t.label == "TRACK-01" and t.depth_valid)

    # Test B - move camera-right
    x_before = t.smoothed[0]
    for _ in range(6):
        t = step(460, 3.0)
    check("B: moving camera-RIGHT increases X", t.smoothed[0] > x_before + 0.4,
          f"{x_before:+.3f} -> {t.smoothed[0]:+.3f}")
    check("B: ...while Z stays sensible", abs(t.smoothed[2] - 3.0) < 0.2,
          f"Z={t.smoothed[2]:.3f}")

    # Test C - move camera-left
    for _ in range(8):
        t = step(180, 3.0)
    check("C: moving camera-LEFT gives a negative X", t.smoothed[0] < -0.4,
          f"X={t.smoothed[0]:+.3f}")

    # Test D - walk toward the camera
    zs = []
    for d in (4.0, 3.5, 3.0, 2.5, 2.0):
        for _ in range(4):
            t = step(320, d)
        zs.append(t.smoothed[2])
    check("D: walking TOWARD the camera decreases Z monotonically",
          all(zs[i] > zs[i + 1] for i in range(len(zs) - 1)),
          " -> ".join(f"{z:.2f}" for z in zs))

    # Test E - walk away
    zs = []
    for d in (2.0, 3.0, 4.0, 5.0):
        for _ in range(4):
            t = step(320, d)
        zs.append(t.smoothed[2])
    check("E: walking AWAY increases Z monotonically",
          all(zs[i] < zs[i + 1] for i in range(len(zs) - 1)),
          " -> ".join(f"{z:.2f}" for z in zs))

    # Test F - id persistence and the trail
    check("F: the track id stayed TRACK-01 throughout", set(s.track_id for s in p.store.all()) == {1})
    check("F: a trajectory trail was accumulated", len(t.trajectory) > 10,
          str(len(t.trajectory)))
    check("F: the trail is (t, x, z) triples", len(t.trajectory[0]) == 3)

    # accuracy of the whole chain against ground truth
    t = None
    for _ in range(8):
        t = step(320 + 200, 2.5)     # 200 px right of centre at 2.5 m
    want_x = 2.5 * 200 / 600.0
    check("the full chain reproduces the geometric ground truth",
          abs(t.smoothed[0] - want_x) < 0.06,
          f"measured {t.smoothed[0]:.3f} vs expected {want_x:.3f}")

    # the annotated frame really is produced
    jpeg, seq = p.latest_jpeg()
    check("an annotated JPEG frame is produced", jpeg is not None and len(jpeg) > 1000,
          str(len(jpeg) if jpeg else 0))
    check("the JPEG is a real JPEG", bool(jpeg) and jpeg[:2] == b"\xff\xd8")
    check("frames counted and fps measured", p.frames_processed > 20 and p.fps > 0,
          f"{p.frames_processed} frames, {p.fps} fps")

    # person disappears -> the track goes stale but is not instantly destroyed
    det.bbox = None
    colour, depth, _ = scene((320, 400), 3.0)
    p._process(colour, depth, det)
    check("a detection gap does not immediately destroy the track",
          p.store.get(1) is not None)

    # depth goes bad (wall only) -> depth_valid false, no invented position
    p2 = VisionPipeline()
    p2.intrinsics = INTR
    p2.depth_scale = SCALE_
    det2 = FakeDetector()
    blank = np.zeros((H, W), dtype=np.uint16)
    colour = np.full((H, W, 3), 40, dtype=np.uint8)
    det2.bbox = (280.0, 100.0, 360.0, 400.0)
    p2._process(colour, blank, det2)
    t2 = p2.store.get(1)
    check("a person with no usable depth still becomes a track", t2 is not None)
    check("...reported as depth_valid=false", t2.depth_valid is False)
    check("...with a null position, not a guess",
          t2.raw is None and t2.smoothed is None)
    check("...and the status stays answerable", p2.status()["active_tracks"] == 1)


# ================================= 11. stream profile negotiation (no camera)
section("11. Stream profile negotiation")

from app.vision.pipeline import (                       # noqa: E402
    COLOR_FORMAT_PREFERENCE, DEPTH_FORMAT_PREFERENCE, describe_profile,
    enumerate_stream_profiles, select_stream_profile)


def _p(stream, w, h, fmt, fps, sensor="s"):
    return {"sensor": sensor, "stream": stream, "format": fmt,
            "width": w, "height": h, "fps": fps, "index": 0}


# A D455-shaped catalogue: 16:9 native, a 4:3 mode, several rates.
D455 = [
    _p("depth", 1280, 720, "z16", 30), _p("depth", 848, 480, "z16", 90),
    _p("depth", 848, 480, "z16", 30), _p("depth", 848, 480, "z16", 15),
    _p("depth", 640, 480, "z16", 30), _p("depth", 640, 480, "z16", 15),
    _p("depth", 640, 360, "z16", 30), _p("depth", 424, 240, "z16", 30),
    _p("color", 1280, 800, "yuyv", 30), _p("color", 1280, 720, "bgr8", 30),
    _p("color", 848, 480, "bgr8", 60), _p("color", 640, 480, "bgr8", 30),
    _p("color", 640, 480, "rgb8", 30), _p("color", 640, 480, "yuyv", 30),
    _p("color", 640, 480, "bgr8", 15), _p("color", 424, 240, "bgr8", 30),
    _p("infrared", 640, 480, "y8", 30),
]

sel = select_stream_profile(D455, "depth", 640, 480, 15, DEPTH_FORMAT_PREFERENCE)
check("an exactly published depth profile is chosen exactly",
      (sel["width"], sel["height"], sel["fps"], sel["format"]) == (640, 480, 15, "z16"),
      describe_profile(sel))

sel = select_stream_profile(D455, "color", 640, 480, 15, COLOR_FORMAT_PREFERENCE)
check("an exactly published colour profile is chosen exactly",
      (sel["width"], sel["height"], sel["fps"]) == (640, 480, 15), describe_profile(sel))
check("...preferring BGR8, which needs no conversion downstream",
      sel["format"] == "bgr8", sel["format"])

sel = select_stream_profile(D455, "color", 640, 480, 30, COLOR_FORMAT_PREFERENCE)
check("resolution is honoured before format preference",
      (sel["width"], sel["height"]) == (640, 480) and sel["fps"] == 30,
      describe_profile(sel))

# 20 fps is not published anywhere: it must fall to 15, never up to 30.
sel = select_stream_profile(D455, "depth", 640, 480, 20, DEPTH_FORMAT_PREFERENCE)
check("an unpublished frame rate falls DOWN, never up (USB bandwidth)",
      sel["fps"] == 15, describe_profile(sel))

# Nothing at all at 640x480 -> the same aspect ratio wins over pixel count.
NARROW = [_p("depth", 848, 480, "z16", 30), _p("depth", 320, 240, "z16", 30)]
sel = select_stream_profile(NARROW, "depth", 640, 480, 30, DEPTH_FORMAT_PREFERENCE)
check("with no exact size, the same aspect ratio is preferred over raw pixel count",
      (sel["width"], sel["height"]) == (320, 240), describe_profile(sel))

check("a stream the device does not publish returns None, never a guess",
      select_stream_profile(D455, "fisheye", 640, 480, 30, ("raw8",)) is None)
check("a format the device does not publish returns None",
      select_stream_profile(D455, "depth", 640, 480, 30, ("bgr8",)) is None)
check("an empty catalogue returns None",
      select_stream_profile([], "depth", 640, 480, 30, DEPTH_FORMAT_PREFERENCE) is None)
check("depth selection never returns a colour profile",
      select_stream_profile(D455, "depth", 640, 480, 30,
                            DEPTH_FORMAT_PREFERENCE)["stream"] == "depth")
check("selection is deterministic across repeated calls",
      describe_profile(select_stream_profile(D455, "color", 800, 600, 25,
                                             COLOR_FORMAT_PREFERENCE))
      == describe_profile(select_stream_profile(D455, "color", 800, 600, 25,
                                                COLOR_FORMAT_PREFERENCE)))
check("describe_profile is readable and includes every field that matters",
      describe_profile(_p("depth", 640, 480, "z16", 15))
      == "depth 640x480 Z16 @ 15",
      describe_profile(_p("depth", 640, 480, "z16", 15)))
check("describe_profile survives a missing profile", describe_profile(None) == "(none)")


class _FakeSP:
    def __init__(self, stream, fmt, w, h, fps, video=True):
        self._s, self._f, self._w, self._h, self._fps = stream, fmt, w, h, fps
        self._video = video

    def is_video_stream_profile(self):
        return self._video

    def as_video_stream_profile(self):
        return self

    def stream_type(self):
        return f"stream.{self._s}"

    def format(self):
        return f"format.{self._f}"

    def width(self):
        return self._w

    def height(self):
        return self._h

    def fps(self):
        return self._fps

    def stream_index(self):
        return 0


class _FakeSensor:
    def __init__(self, name, profs):
        self.name, self.profs = name, profs

    def get_info(self, _):
        return self.name

    def get_stream_profiles(self):
        return self.profs


class _FakeDevice:
    def __init__(self, sensors):
        self.sensors = sensors

    def query_sensors(self):
        return self.sensors


_rs = types.SimpleNamespace(camera_info=types.SimpleNamespace(name="name"))
_dev = _FakeDevice([
    _FakeSensor("Stereo Module", [_FakeSP("depth", "z16", 640, 480, 15),
                                  _FakeSP("motion", "motion_xyz32f", 0, 0, 200,
                                          video=False)]),
    _FakeSensor("RGB Camera", [_FakeSP("color", "bgr8", 640, 480, 15)]),
])
enum = enumerate_stream_profiles(_rs, _dev)
check("enumeration reads both sensors and skips non-video profiles",
      len(enum) == 2, str(enum))
check("...and normalises the SDK's enum reprs to plain lowercase names",
      {e["stream"] for e in enum} == {"depth", "color"}
      and {e["format"] for e in enum} == {"z16", "bgr8"}, str(enum))
check("...so the selector works straight off an enumerated catalogue",
      describe_profile(select_stream_profile(
          enum, "depth", 640, 480, 15, DEPTH_FORMAT_PREFERENCE))
      == "depth 640x480 Z16 @ 15")

check("startup and runtime frame timeouts are separate settings",
      appcfg.settings.VISION_STARTUP_TIMEOUT_MS
      > appcfg.settings.VISION_FRAME_TIMEOUT_MS,
      f"{appcfg.settings.VISION_STARTUP_TIMEOUT_MS} vs "
      f"{appcfg.settings.VISION_FRAME_TIMEOUT_MS}")
check("the first frame gets at least 10 s",
      appcfg.settings.VISION_STARTUP_TIMEOUT_MS >= 10000)
check("some frames are discarded as warm-up before anything is processed",
      appcfg.settings.VISION_WARMUP_FRAMES >= 5)
check("the diagnostic starts from a conservative frame rate",
      appcfg.settings.VISION_DIAG_FPS <= 15)


# ===================================================================== report
print()
print("=" * 62)
if FAILS:
    print(f"\033[31m{len(FAILS)} of {CHECKS} CHECKS FAILED\033[0m")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print(f"\033[32mALL {CHECKS} CHECKS PASSED\033[0m")
sys.exit(0)

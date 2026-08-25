# Camera perception — phase 1: RealSense picker tracking

**What this phase proves, and nothing more:** a real person moving in front of
the Intel RealSense becomes a *persistent track* whose physical X / Z movement
can be watched live, in **camera-local metres**.

It stops there deliberately. There is no vehicle and no GNSS yet, so there is
no honest camera → world transform to make. Inventing a latitude/longitude
here would be precisely the "nearest GPS point" shortcut the whole Property
Master / service-zone design exists to avoid.

```
RealSense D4xx
  ├── colour 640×480 @30            ─┐
  └── depth  640×480 @30, ALIGNED   ─┴─→ one frameset, one pixel grid
                    │
                    ├─→ YOLO (person class only) + ByteTrack   → track_id, bbox, conf
                    │
                    ├─→ ground-contact anchor pixel            → (u, v)
                    ├─→ robust depth window around it          → depth_m or depth_valid=false
                    ├─→ rs2 deprojection                        → x_m, y_m, z_m  (camera frame)
                    ├─→ EMA + jump gate                         → smoothed position
                    └─→ 12 s trajectory ring buffer             → the phase-2 input
                                    │
                    in-memory TrackStore (nothing persisted)
                                    │
        /vision/status   /vision/tracks   /vision/stream.mjpeg   /picker-tracking
```

---

## Coordinate convention

The **native librealsense optical frame**, unchanged. No sign is flipped
anywhere in this codebase.

| axis | direction | meaning here |
|------|-----------|--------------|
| `+x` | camera **right** | lateral displacement. Walk to the camera's right → x grows. |
| `-x` | camera **left**  | |
| `+y` | **down** | native convention. Not "up". Carried through untouched so a later ground-plane fit does not have to undo an invention of ours. |
| `+z` | **forward**, away from the lens | the range. Walk toward the camera → z shrinks. |

The origin is the colour sensor's optical centre. In this phase the camera is
a **fixed stationary reference frame** — move the camera and every number
moves with it. `GET /vision/status` returns this convention as data, so a
client never has to guess.

---

## The two decisions that matter

### 1. Which pixel we measure

Not the bounding-box centre. The point that matters is where the person is
**standing**, because phase 2 turns exactly that into a position on the ground
beside a property. The box centre floats at chest height and jumps whenever the
detector clips a head or a foot.

So: **bottom-centre of the box, lifted by 6 % of the box height**
(`VISION_ANCHOR_INSET`). The very last rows of a person box routinely sit on
the floor *behind* them, or on box padding; the lift keeps the sample on the
ankles and shins.

If that yields nothing usable — feet occluded by a bin, cut off at the frame
edge, swallowed by the floor plane — it falls back **once** to a torso anchor
40 % down the box, whose range is within a few centimetres of the standing
range. Which anchor was used is reported per track (`anchor_source`), never
hidden.

### 2. Which depth values we trust

A single pixel is not a measurement on a stereo depth camera. The method, in
`backend/app/vision/geometry.py:sample_depth`:

1. Read a **9×9 window** centred on the anchor (`VISION_DEPTH_WINDOW`).
2. Scale raw uint16 → metres and drop anything outside **0.3–10 m**. On a
   RealSense, `0` means *no data*; it must never be averaged in as "0 metres
   away".
3. Fewer than **6** valid pixels (`VISION_DEPTH_MIN_VALID`) → `depth_valid:
   false`. We do not build a coordinate out of two lucky pixels.
4. **Reject the background.** A window near someone's feet straddles the person
   and the floor several metres behind them, so a plain median can land in the
   empty gap between the two surfaces. Take the **25th percentile** — which
   sits on the *near* surface, i.e. the person — and keep only samples within
   **0.30 m** of it (`VISION_DEPTH_CLUSTER_TOL_M`).
5. Return the **median of what is left**, plus its spread as a jitter hint.

When there is no trustworthy depth the track still exists and still appears in
the API. Its `camera_position_raw` and `camera_position_smoothed` are `null`
and `depth_reason` says why. It is never given a stale or invented coordinate.

### Smoothing

Per-axis EMA, `α = 0.4`, **with a jump gate**: a frame-to-frame move of
≥ `1.2 m` snaps straight to the raw value instead of easing towards it. Standing
still is steady; walking is not laggy. 90 % of a step is reached in ~6 frames
(≈ 0.2 s at 30 fps) — measured in the test suite, not asserted by hand. Raw and
smoothed are both kept and both exposed.

---

## API

| method | path | what |
|---|---|---|
| `GET`  | `/vision/status` | camera / colour / depth / detector / tracker flags, fps, latest frame time, intrinsics, depth scale, every tuning value in force, the convention, and `last_error`. **Always answers**, camera or no camera. |
| `GET`  | `/vision/tracks` | live tracks. `?trajectory=false` to drop the trails, `?include_stale=true` to see tracks that are retained but not currently live. |
| `GET`  | `/vision/tracks/{id}` | one track, with its trail. |
| `POST` | `/vision/start` / `/vision/stop` | grab / release the camera. Idempotent. |
| `GET`  | `/vision/stream.mjpeg` | annotated live feed (multipart MJPEG, frame-driven). |
| `GET`  | `/vision/frame.jpg` | the latest annotated frame as one JPEG. |
| `GET`/`POST`/`DELETE` | `/vision/calibration/samples` | expected-vs-measured spot checks. |
| `GET`  | `/health/vision` | is the vision package even importable, and what is it doing. |

One track:

```json
{
  "track_id": 1, "label": "TRACK-01",
  "timestamp": "2026-08-22T06:00:00+00:00",
  "bbox": [220, 90, 300, 410],
  "detection_confidence": 0.91,
  "depth_valid": true, "depth_m": 3.72,
  "depth_reason": "OK", "anchor_px": [260, 391], "anchor_source": "GROUND",
  "camera_position_raw":      { "x_m": 0.86, "y_m": 1.20, "z_m": 3.75 },
  "camera_position_smoothed": { "x_m": 0.84, "y_m": 1.18, "z_m": 3.72 },
  "trajectory": [ { "t": "...", "x_m": 0.81, "z_m": 3.70 } ]
}
```

`track_id` is a **camera track**, not a worker. `TRACK-01` today and
`TRACK-01` tomorrow are unrelated people. Worker identity is a later phase.

---

## Install and run

```bash
./scripts/add_realsense_picker_tracking.sh     # deps, weights, tests, restart
./scripts/run_backend.sh                       # then open the page
open http://127.0.0.1:8000/picker-tracking
```

The camera does **not** start with the backend (`VISION_AUTOSTART=0` by
default). Press **Start camera** on the page, or `POST /vision/start`.

Offline tests, no hardware:

```bash
.venv/bin/python scripts/test_vision_logic.py
```

### Hardware test — staged

No backend, no browser, no database. Use this first when something is wrong,
because it tells you *which* layer failed and stops there.

**On this Mac the camera needs elevated execution** — without it librealsense
fails with `failed to set power state`. That is a macOS USB-permission
property of this machine, not of the application; do not "fix" it by loosening
anything else. The command for this machine is:

```bash
sudo .venv/bin/python scripts/test_realsense_picker_tracking.py --view
```

Six stages, each printing `PASS` or `FAIL`, in order of increasing
complexity, so a detector problem can never be mistaken for a camera problem:

| stage | what it proves | what it deliberately excludes |
|---|---|---|
| 1/6 Dependencies | imports, device present, USB link speed, published profiles | — |
| 2/6 Depth-only | depth frames actually arrive | colour, align, YOLO, tracker, OpenCV |
| 3/6 Color-only | RGB frames actually arrive | depth, align, YOLO, tracker |
| 4/6 Combined RGB-D | both streams together fit the link | alignment, YOLO, tracker |
| 5/6 Alignment | `rs.align(color)` produces matching frames | YOLO, tracker |
| 6/6 Detection/tracking | YOLO + ByteTrack on real frames, with depth | — |

Flags:

```bash
sudo .venv/bin/python scripts/test_realsense_picker_tracking.py --stage 2
sudo .venv/bin/python scripts/test_realsense_picker_tracking.py --fps 30
sudo .venv/bin/python scripts/test_realsense_picker_tracking.py --width 848 --height 480
sudo .venv/bin/python scripts/test_realsense_picker_tracking.py --probe-seconds 5
sudo .venv/bin/python scripts/test_realsense_picker_tracking.py --seconds 60 --quiet
```

The default rate is the conservative **15 fps**, not 30. Prove the low rate
works before assuming the high one does — on a link that has quietly
negotiated down to USB 2.1, dual-stream 30 fps is simply not deliverable, and
the symptom is a pipeline that starts cleanly and then never produces a frame.
Stage 1 prints the USB descriptor for exactly that reason.

### When frames never arrive

`Frame didn't arrive within 2000` with zero frames processed, while
`sudo /opt/homebrew/bin/rs-capture` works, means the difference is the
*configuration*, not the hardware — `rs-capture` starts with no config at all
and takes whatever the device recommends. Three things now guard that gap:

- **Profiles are negotiated, never guessed.** `RealSenseSource.open()`
  enumerates every profile the device publishes and picks the closest
  supported match (exact resolution first, then aspect ratio, then a rate at
  or *below* the request — never silently faster). The chosen profile is
  logged and appears in `/vision/status`.
- **The first frame gets its own timeout.** Startup costs a sensor power-up,
  auto-exposure convergence and a macOS UVC negotiation — seconds, not
  milliseconds. `VISION_STARTUP_TIMEOUT_MS` (10 s) covers the first frameset;
  `VISION_FRAME_TIMEOUT_MS` (2 s) covers steady state. Sharing one 2 s timeout
  between the two is what turns a slow start into a permanent failure.
- **Warm-up frames are discarded.** The first `VISION_WARMUP_FRAMES` (10)
  framesets are read and thrown away before anything looks at one; they are
  dark, half-exposed and sometimes missing a stream. YOLO never sees them.

If stage 2 passes at 15 fps but the live page still starves at 30, pin the
backend to the rate that works:

```bash
echo 'VISION_FPS=15' >> .env
```

### pyrealsense2 on Apple Silicon

**There is no macOS arm64 wheel on PyPI.** `pip install pyrealsense2` cannot
work on this Mac, and the installer says so instead of pretending. Build
librealsense with its Python bindings against this project's `.venv`:

```bash
brew install cmake libusb pkg-config
git clone --depth 1 https://github.com/IntelRealSense/librealsense.git ~/librealsense
cd ~/librealsense && mkdir -p build && cd build
cmake .. \
  -DBUILD_PYTHON_BINDINGS=bool:true \
  -DPYTHON_EXECUTABLE=~/Documents/wastraq-demo/.venv/bin/python \
  -DBUILD_EXAMPLES=false -DBUILD_GRAPHICAL_EXAMPLES=false \
  -DCMAKE_BUILD_TYPE=Release
make -j"$(sysctl -n hw.ncpu)"
cp wrappers/python/pyrealsense2*.so \
   "$(ls -d ~/Documents/wastraq-demo/.venv/lib/python3.*/site-packages)/"
```

Then re-run the installer. Everything else — the backend, the page, the API,
the tests — works without it and reports `camera_connected: false`.

---

## Calibration

Stand on a measured spot, pick it from the dropdown on the page, press
**Capture**. The panel shows expected vs measured X / Z and the error. Do the
four standard positions:

| stand | expect |
|---|---|
| 2 m forward | X ≈ 0, Z ≈ 2 |
| 4 m forward | X ≈ 0, Z ≈ 4 |
| 2 m forward + 1 m **left** | X ≈ **−1**, Z ≈ 2 |
| 2 m forward + 1 m **right** | X ≈ **+1**, Z ≈ 2 |

Z is measured to the **lens**, not the tripod leg or the wall. X is measured to
the optical axis, which is not the centre of the camera body. Errors under
~0.15 m are green on the panel.

If X has the wrong **sign**, the camera is physically mounted facing the other
way — nothing in the code flips it.

---

## Configuration

All env-overridable, all defaulted in `backend/app/config.py`. Nothing needs to
be set for the demo to work.

| variable | default | |
|---|---|---|
| `VISION_AUTOSTART` | `0` | start the camera with the backend |
| `VISION_WIDTH` / `_HEIGHT` / `_FPS` | `640` / `480` / `30` | requested stream; negotiated down to a published profile if needed |
| `VISION_FRAME_TIMEOUT_MS` | `2000` | steady-state frame wait |
| `VISION_STARTUP_TIMEOUT_MS` | `10000` | the **first** frameset only |
| `VISION_WARMUP_FRAMES` | `10` | discarded after `start()` |
| `VISION_DIAG_FPS` | `15` | conservative rate the staged hardware test starts from |
| `VISION_MODEL` | `yolov8n.pt` | resolved against `models/` first |
| `VISION_CONF` | `0.35` | detection threshold |
| `VISION_IMGSZ` | `640` | inference size |
| `VISION_TRACKER` | `bytetrack.yaml` | or `botsort.yaml` |
| `VISION_DEVICE` | *(auto)* | `mps`, `cpu`, … |
| `VISION_DEPTH_WINDOW` | `9` | px, square |
| `VISION_DEPTH_MIN_VALID` | `6` | px that must be usable |
| `VISION_DEPTH_CLUSTER_TOL_M` | `0.30` | near-surface band |
| `VISION_DEPTH_MIN_M` / `_MAX_M` | `0.3` / `10.0` | clip |
| `VISION_ANCHOR_INSET` | `0.06` | lift off the box bottom |
| `VISION_SMOOTH_ALPHA` | `0.4` | EMA |
| `VISION_SMOOTH_MAX_JUMP_M` | `1.2` | snap threshold |
| `VISION_TRAJECTORY_S` | `12` | trail length |
| `VISION_TRACK_TTL_S` | `1.5` | stops being "live" |
| `VISION_TRACK_RETIRE_S` | `6.0` | history discarded |
| `VISION_STREAM_ENABLED` | `1` | encode the MJPEG feed |
| `VISION_JPEG_QUALITY` | `70` | |

Tuning that is actually in force is echoed by `/vision/status` and shown on the
page, so a stale `.env` cannot quietly disagree with what you are watching.

---

## Failure behaviour

| situation | what happens |
|---|---|
| no camera plugged in | `camera_connected: false`, `state: NO_CAMERA`, retry with backoff. **The backend stays up.** |
| `pyrealsense2` missing | same, plus `missing_dependencies: ["pyrealsense2"]`. The thread stops rather than retrying something that cannot succeed. |
| `ultralytics` missing | `detector_loaded: false`, `state: ERROR`, `last_error` names it. |
| camera unplugged mid-run | `state: DEGRADED`, camera reopened automatically, tracker state reset. |
| person visible, depth bad | track exists, `depth_valid: false`, positions `null`, `depth_reason` says which check failed. |
| detection briefly lost | track drops out of *live* after 1.5 s but keeps its history for 6 s, so a short occlusion recovers the same id and the same trail. |
| several people | several independent track records. `track_id` is never assumed to be a worker id. |
| one bad frame | logged, skipped; the thread does not die. |
| the whole vision package fails to import | `main.py` catches it — `/vision/*` disappears, the property system is untouched. |

---

## What this phase does **not** do

GNSS · IMU · vehicle pose · latitude/longitude · camera→world transform · GIS
property association · service-zone matching · collection-episode state machine
· RFID · non-segregation gesture · MediaPipe · evidence clip extraction ·
worker identity · 360° camera.

Not persisted either: **no database table, no migration, no row**. The
16-property pilot lane, its geometry and its verification states are neither
read nor written by any of this.

---

## What phase 2 should consume

1. **`TrackStore` trajectories** — `backend/app/vision/tracking.py`. Already
   timestamped, smoothed, bounded (x, z) in camera metres. That is the input to
   a dwell/approach detector.
2. **`depth_valid` as a first-class signal.** A collection episode built on
   frames with no trustworthy depth should be *low confidence*, not silently
   equal to one built on good frames.
3. **The camera→world seam is one function.** Everything above is camera-local
   and says so. When there is a vehicle pose, one transform applied to the
   trajectory produces a world trajectory; nothing else in this phase changes.
4. **`track_id` ≠ worker.** Whatever binds a track to a person (RFID proximity,
   re-identification, an assignment) belongs above this layer.
5. **Association stays multi-signal.** A world trajectory is one input to
   service-zone matching alongside the verified Property Master. It does not
   become "nearest property to the camera dot".

---

## Files

```
backend/app/vision/__init__.py     router export; no hardware import
backend/app/vision/geometry.py     intrinsics, deprojection, anchors, depth sampling  (pure)
backend/app/vision/tracking.py     EMA, trajectory buffer, TrackStore                 (pure)
backend/app/vision/schemas.py      pydantic contract
backend/app/vision/pipeline.py     camera thread, detector, overlay, MJPEG
backend/app/vision/api.py          /vision/* routes
backend/app/static/picker-tracking.html   live page: feed + top-down + calibration
backend/requirements-vision.txt    ultralytics, opencv, pyrealsense2
scripts/test_vision_logic.py       offline suite, no hardware
scripts/test_realsense_picker_tracking.py   manual hardware test
scripts/add_realsense_picker_tracking.sh    installer
models/yolov8n.pt                  fetched by the installer
```

`geometry.py` and `tracking.py` import nothing outside the standard library —
that is what makes the whole decision layer testable with no camera attached.

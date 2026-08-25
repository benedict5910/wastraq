#!/usr/bin/env python3
"""
Manual hardware test: RealSense -> person detection -> track -> X / Z metres.

This is the one test that needs the camera physically plugged in. It is
STAGED: six independent checks that each print PASS or FAIL, run in order of
increasing complexity, and stop at the first failure. The point is that
"no frames" can never again be blamed on YOLO, and a YOLO problem can never
be blamed on the camera.

    1/6  Dependencies      imports + device inventory (USB type, profiles)
    2/6  Depth-only        depth stream alone: no colour, no align, no model
    3/6  Color-only        colour stream alone
    4/6  Combined RGB-D    both streams, still no alignment
    5/6  Alignment         rs.align(color) on top of the combined stream
    6/6  Detection/track   YOLO + ByteTrack + depth -> live X / Z metres

On this Mac the camera needs elevated execution (librealsense otherwise
fails with "failed to set power state"), so the command is:

    sudo .venv/bin/python scripts/test_realsense_picker_tracking.py --view

Useful flags:

    --stage 2          run stages 1..2 only, then stop
    --fps 30           try a faster profile (default is the conservative 15)
    --width/--height   request a different resolution (default 640x480)
    --probe-seconds 5  how long each streaming stage samples for
    --seconds 60       stop stage 6 after N seconds (default: until Ctrl-C)
    --view             OpenCV window with the annotated frame (stage 6)
    --quiet            stage 6 prints only when the track set changes

Nothing is written to the database. No property, survey or GIS state is read
or touched by this script.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")

STAGES = ("Dependencies", "Depth-only", "Color-only",
          "Combined RGB-D", "Alignment", "Detection/tracking")
N_STAGES = len(STAGES)


def heading(n: int) -> None:
    print(f"\n{BOLD}{n}/{N_STAGES}  {STAGES[n - 1]}{RESET}")


def passed() -> None:
    print(f"{GREEN}PASS{RESET}")


def failed(msg: str = "") -> None:
    print(f"{RED}FAIL{RESET}" + (f"  {msg}" if msg else ""))


def line(msg: str) -> None:
    print(f"  {msg}")


def ok(msg: str) -> None:
    print(f"  {GREEN}ok{RESET}    {msg}")


def bad(msg: str) -> None:
    print(f"  {RED}FAIL{RESET}  {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}warn{RESET}  {msg}")


def stop(stage: int, why: str, hints: list[str] | None = None) -> int:
    """Announce which stage failed and get out. Never mask the real layer."""
    print()
    print("=" * 62)
    print(f"{RED}STOPPED AT STAGE {stage}/{N_STAGES} - {STAGES[stage - 1]}{RESET}")
    print(f"  {why}")
    for h in hints or []:
        print(f"  - {h}")
    print("=" * 62)
    return 1


# --------------------------------------------------------------------------
# streaming probe shared by stages 2-5
# --------------------------------------------------------------------------

def probe(source, seconds: float, want_color: bool, want_depth: bool):
    """Read frames for N seconds. -> (frames, fps, first_error, shapes)."""
    frames = 0
    errors = 0
    first_error = None
    shapes: dict = {}
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            colour, depth = source.read()
        except Exception as exc:  # noqa: BLE001
            errors += 1
            if first_error is None:
                first_error = f"{type(exc).__name__}: {exc}"
            if errors >= 3:
                break
            continue
        frames += 1
        if want_color and colour is not None:
            shapes["color"] = tuple(colour.shape)
        if want_depth and depth is not None:
            shapes["depth"] = tuple(depth.shape)
            shapes["_depth_arr"] = depth
    elapsed = max(time.time() - t0, 1e-6)
    return frames, frames / elapsed, first_error, shapes


def centre_depth_m(depth, depth_scale: float, half: int = 20):
    """Median of the valid (non-zero) depth pixels in the centre window."""
    h, w = depth.shape[:2]
    cy, cx = h // 2, w // 2
    win = depth[max(cy - half, 0):cy + half, max(cx - half, 0):cx + half]
    vals = [float(v) * depth_scale for v in win.reshape(-1).tolist() if v > 0]
    if not vals:
        return None, 0, int(win.size)
    return statistics.median(vals), len(vals), int(win.size)


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", type=int, default=N_STAGES,
                    help=f"run stages 1..N only (1-{N_STAGES}, default all)")
    ap.add_argument("--width", type=int, default=None)
    ap.add_argument("--height", type=int, default=None)
    ap.add_argument("--fps", type=int, default=None,
                    help="requested frame rate (default: the conservative "
                         "VISION_DIAG_FPS, normally 15)")
    ap.add_argument("--probe-seconds", type=float, default=3.0,
                    help="how long each streaming stage samples for")
    ap.add_argument("--seconds", type=float, default=0,
                    help="stop stage 6 after N seconds (default: until Ctrl-C)")
    ap.add_argument("--view", action="store_true",
                    help="stage 6: also open an OpenCV window")
    ap.add_argument("--quiet", action="store_true",
                    help="stage 6: only print when the track set changes")
    args = ap.parse_args()

    print(f"{BOLD}Wastraq - RealSense picker tracking, staged hardware test{RESET}")
    print(f"{DIM}camera-local coordinates only: no GNSS, no property association,"
          f" nothing written to the database{RESET}")

    # ================================================== 1/6  Dependencies ==
    heading(1)
    missing = []
    for name, hint in (("pyrealsense2", "librealsense SDK python bindings"),
                       ("numpy", "arrays"),
                       ("cv2", "OpenCV"),
                       ("ultralytics", "YOLO detector + ByteTrack")):
        try:
            __import__(name)
            ok(f"{name}  ({hint})")
        except Exception as exc:  # noqa: BLE001
            bad(f"{name} NOT importable - {exc}")
            missing.append(name)
    if missing:
        failed(f"missing: {', '.join(missing)}")
        return stop(1, f"cannot import {', '.join(missing)}", [
            "run ./scripts/add_realsense_picker_tracking.sh first",
            "pyrealsense2 has no Apple-Silicon wheel; docs/VISION.md has the "
            "librealsense build",
        ])

    import pyrealsense2 as rs  # noqa: E402

    from app.config import settings                     # noqa: E402
    from app.vision import geometry as geo              # noqa: E402
    from app.vision.pipeline import (                   # noqa: E402
        COLOR_FORMAT_PREFERENCE, DEPTH_FORMAT_PREFERENCE, PersonDetector,
        RealSenseSource, describe_profile, enumerate_stream_profiles,
        select_stream_profile)
    from app.vision.tracking import TrackStore, utcnow  # noqa: E402

    width = args.width or settings.VISION_WIDTH
    height = args.height or settings.VISION_HEIGHT
    fps = args.fps or settings.VISION_DIAG_FPS

    # -- device inventory: what is actually on the bus, and at what speed --
    try:
        devices = list(rs.context().query_devices())
    except Exception as exc:  # noqa: BLE001
        failed(str(exc))
        return stop(1, f"librealsense could not query devices: {exc}", [
            "on this Mac the camera needs sudo: "
            "sudo .venv/bin/python scripts/test_realsense_picker_tracking.py",
        ])
    if not devices:
        failed("no RealSense device on USB")
        return stop(1, "no RealSense device found", [
            "USB 3 port, and the cable that came with the camera",
            "nothing else holding the device (realsense-viewer, rs-capture, "
            "a running backend)",
            "sudo is required on this Mac",
        ])

    dev = devices[0]
    info = {}
    for attr in ("name", "serial_number", "firmware_version",
                 "usb_type_descriptor"):
        try:
            info[attr] = dev.get_info(getattr(rs.camera_info, attr))
        except Exception:  # noqa: BLE001
            info[attr] = "?"
    ok(f"device        {info['name']}  serial {info['serial_number']}")
    ok(f"firmware      {info['firmware_version']}")
    usb = str(info["usb_type_descriptor"])
    if usb.startswith("3"):
        ok(f"USB           {usb}")
    else:
        warn(f"USB           {usb}  <- NOT USB 3. The device publishes a much "
             "smaller profile list and will not sustain dual streams at 30 fps. "
             "Try the other port / the bundled cable.")

    profiles = enumerate_stream_profiles(rs, dev)
    n_depth = len([p for p in profiles if p["stream"] == "depth"])
    n_color = len([p for p in profiles if p["stream"] == "color"])
    ok(f"profiles      {len(profiles)} published  ({n_depth} depth, {n_color} color)")

    match_depth = select_stream_profile(profiles, "depth", width, height, fps,
                                        DEPTH_FORMAT_PREFERENCE)
    match_color = select_stream_profile(profiles, "color", width, height, fps,
                                        COLOR_FORMAT_PREFERENCE)
    line(f"{DIM}requested     {width}x{height} @ {fps}{RESET}")
    line(f"{DIM}depth match   {describe_profile(match_depth)}{RESET}")
    line(f"{DIM}color match   {describe_profile(match_color)}{RESET}")
    if match_depth is None or match_color is None:
        failed("the device publishes no usable profile for one of the streams")
        return stop(1, "no matching stream profile", [
            f"depth profiles published: {n_depth}",
            f"color profiles published: {n_color}",
        ])
    if (match_depth["width"], match_depth["height"]) != (width, height) or \
            match_depth["fps"] != fps:
        warn("the requested depth profile is not published; the closest "
             "supported one will be used instead")
    passed()

    if args.stage < 2:
        return 0

    # =================================================== 2/6  Depth-only ==
    # No colour, no alignment, no YOLO, no tracker, no OpenCV window. If this
    # fails while rs-capture works, the problem is the profile or the startup
    # sequence and nothing else.
    heading(2)
    depth_src = RealSenseSource(width, height, fps,
                                enable_color=False, enable_depth=True, align=False)
    try:
        depth_src.open()
    except Exception as exc:  # noqa: BLE001
        failed(f"{type(exc).__name__}: {exc}")
        line(f"{DIM}selected profile: {describe_profile(depth_src.depth_profile)}{RESET}")
        for row in depth_src.supported("depth")[:12]:
            line(f"{DIM}  published: {row}{RESET}")
        depth_src.close()
        return stop(2, f"depth stream would not start or deliver: {exc}", [
            f"startup timeout was {depth_src.startup_timeout_ms} ms",
            "rs-capture works because it starts with NO config and takes the "
            "device's own recommended profile - try --fps 15, then --fps 6",
            "raise the wait further with VISION_STARTUP_TIMEOUT_MS=20000",
        ])

    line(f"profile: {describe_profile(depth_src.depth_profile)}")
    line(f"first frame after {depth_src.first_frame_ms:.0f} ms "
         f"(startup timeout {depth_src.startup_timeout_ms} ms)")
    line(f"warm-up frames discarded: {depth_src.warmup_frames_read}")
    line(f"depth scale: {depth_src.depth_scale} m/unit")

    frames, achieved, err, shapes = probe(depth_src, args.probe_seconds,
                                          want_color=False, want_depth=True)
    if frames == 0:
        failed(err or "no frames")
        depth_src.close()
        return stop(2, "the depth stream started but delivered no frames", [
            f"first error: {err}",
            "try --fps 15 (or --fps 6), and check nothing else holds the device",
        ])
    dshape = shapes["depth"]
    line(f"frame dimensions: {dshape[1]}x{dshape[0]}")
    centre, valid_px, total_px = centre_depth_m(shapes["_depth_arr"],
                                                depth_src.depth_scale or 0.001)
    if centre is None:
        warn("centre region has no valid depth - point the camera at "
             "something 1-6 m away; the stream itself is fine")
    else:
        line(f"centre-region depth: {centre:.3f} m "
             f"({valid_px}/{total_px} pixels valid)")
    line(f"frames: {frames} in {args.probe_seconds:.0f} s "
         f"({achieved:.1f} fps achieved, {depth_src.depth_profile['fps']} requested)")
    depth_src.close()
    line("pipeline stopped cleanly")
    passed()

    if args.stage < 3:
        return 0

    # =================================================== 3/6  Color-only ==
    heading(3)
    color_src = RealSenseSource(width, height, fps,
                                enable_color=True, enable_depth=False, align=False)
    try:
        color_src.open()
    except Exception as exc:  # noqa: BLE001
        failed(f"{type(exc).__name__}: {exc}")
        for row in color_src.supported("color")[:12]:
            line(f"{DIM}  published: {row}{RESET}")
        color_src.close()
        return stop(3, f"colour stream would not start or deliver: {exc}", [
            "depth alone worked, so this is the RGB sensor or its format",
            "try --fps 15, or a different resolution with --width/--height",
        ])

    line(f"profile: {describe_profile(color_src.color_profile)}")
    line(f"first frame after {color_src.first_frame_ms:.0f} ms")
    frames, achieved, err, shapes = probe(color_src, args.probe_seconds,
                                          want_color=True, want_depth=False)
    if frames == 0:
        failed(err or "no frames")
        color_src.close()
        return stop(3, "the colour stream started but delivered no frames",
                    [f"first error: {err}"])
    line(f"frame shape: {shapes.get('color')}")
    line(f"frames: {frames} in {args.probe_seconds:.0f} s ({achieved:.1f} fps achieved)")
    color_src.close()
    passed()

    if args.stage < 4:
        return 0

    # =============================================== 4/6  Combined RGB-D ==
    # Both streams, still no alignment, no model. This is the stage that fails
    # when the two profiles are individually fine but their combination
    # exceeds what the USB link will carry.
    heading(4)
    both = RealSenseSource(width, height, fps,
                           enable_color=True, enable_depth=True, align=False)
    try:
        both.open()
    except Exception as exc:  # noqa: BLE001
        failed(f"{type(exc).__name__}: {exc}")
        both.close()
        return stop(4, f"depth+colour together would not start: {exc}", [
            "each stream worked alone, so this is a bandwidth or combination "
            "problem, not a broken sensor",
            f"currently at {fps} fps - try --fps 6",
            "if USB showed as 2.1 above, that is almost certainly the cause",
        ])

    line(f"profile: {both.profile_summary()}")
    line(f"first frame after {both.first_frame_ms:.0f} ms")
    frames, achieved, err, shapes = probe(both, args.probe_seconds,
                                          want_color=True, want_depth=True)
    seen = {k: v for k, v in shapes.items() if not k.startswith("_")}
    if frames == 0 or "color" not in shapes or "depth" not in shapes:
        failed(err or "one of the two streams never arrived")
        both.close()
        return stop(4, "combined RGB-D delivered no complete frameset",
                    [f"first error: {err}", f"shapes seen: {seen}"])
    line(f"color frame: {shapes['color']}   depth frame: {shapes['depth']}")
    line(f"frames: {frames} in {args.probe_seconds:.0f} s ({achieved:.1f} fps achieved)")
    both.close()
    passed()

    if args.stage < 5:
        return 0

    # ==================================================== 5/6  Alignment ==
    heading(5)
    src = RealSenseSource(width, height, fps,
                          enable_color=True, enable_depth=True, align=True)
    try:
        src.open()
    except Exception as exc:  # noqa: BLE001
        failed(f"{type(exc).__name__}: {exc}")
        src.close()
        return stop(5, f"aligned stream would not start: {exc}",
                    ["unaligned RGB-D worked, so this is rs.align itself"])

    line(f"profile: {src.profile_summary()}  + rs.align(color)")
    frames, achieved, err, shapes = probe(src, args.probe_seconds,
                                          want_color=True, want_depth=True)
    if frames == 0:
        failed(err or "no aligned frames")
        src.close()
        return stop(5, "alignment produced no framesets", [f"first error: {err}"])
    cshape, dshape = shapes["color"], shapes["depth"]
    if dshape[:2] != cshape[:2]:
        failed(f"aligned depth {dshape[:2]} != colour {cshape[:2]}")
        src.close()
        return stop(5, "aligned depth does not match the colour frame size", [
            "every bounding box would index the wrong depth pixels",
        ])
    ok(f"aligned depth frame valid   {dshape[1]}x{dshape[0]}")
    ok(f"colour frame valid          {cshape[1]}x{cshape[0]}")
    centre, valid_px, total_px = centre_depth_m(shapes["_depth_arr"],
                                                src.depth_scale or 0.001)
    if centre is not None:
        ok(f"centre-region depth         {centre:.3f} m "
           f"({valid_px}/{total_px} px valid)")
    intr = src.intrinsics
    assert intr is not None
    ok(f"intrinsics                  fx={intr.fx:.1f} fy={intr.fy:.1f} "
       f"ppx={intr.ppx:.1f} ppy={intr.ppy:.1f} model={intr.model}")
    if intr.model == geo.DISTORTION_MODIFIED_BROWN_CONRADY:
        warn("this stream reports a forward-only distortion model; deprojection "
             "will refuse it. Report this - it is unusual for a colour stream.")
    line(f"frames: {frames} in {args.probe_seconds:.0f} s ({achieved:.1f} fps achieved)")
    passed()

    if args.stage < 6:
        src.close()
        return 0

    # ============================================ 6/6  Detection/tracking ==
    heading(6)
    det = PersonDetector(settings.VISION_MODEL, settings.VISION_CONF,
                         settings.VISION_IMGSZ, settings.VISION_TRACKER,
                         settings.VISION_DEVICE)
    try:
        det.load()
    except Exception as exc:  # noqa: BLE001
        failed(f"detector failed to load: {exc}")
        src.close()
        return stop(6, f"YOLO would not load: {exc}", [
            "the camera is fine - all five stream stages passed",
            f"weights: {settings.VISION_MODEL} under {settings.VISION_MODEL_DIR}",
        ])
    ok(f"detector loaded  {det.resolved_path} (person class only, "
       f"conf >= {settings.VISION_CONF})")
    ok(f"tracker active   {settings.VISION_TRACKER}")
    print(f"{DIM}  Stand in front of the camera. Walk left / right / toward / "
          f"away. Ctrl-C to stop.{RESET}\n")

    store = TrackStore(live_ttl_s=settings.VISION_TRACK_TTL_S,
                       retire_s=settings.VISION_TRACK_RETIRE_S,
                       trajectory_seconds=settings.VISION_TRAJECTORY_S)
    started = time.time()
    frames = 0
    read_failures = 0
    fps_live = 0.0
    last_t = None
    last_print = 0.0
    last_signature = None
    seen_detections = 0
    seen_valid = 0

    try:
        while True:
            if args.seconds and time.time() - started >= args.seconds:
                break
            try:
                colour, depth = src.read()
            except Exception as exc:  # noqa: BLE001
                read_failures += 1
                warn(f"frame read failed: {exc}")
                if read_failures >= 10:
                    break
                continue

            now = time.time()
            if last_t:
                inst = 1.0 / max(now - last_t, 1e-6)
                fps_live = inst if fps_live == 0 else 0.85 * fps_live + 0.15 * inst
            last_t = now
            frames += 1

            ts = utcnow()
            rows = []
            for d in det.track(colour):
                seen_detections += 1
                sample, pos = geo.measure_person(
                    depth, d["bbox"], intr,
                    depth_scale=src.depth_scale or 1.0,
                    window=settings.VISION_DEPTH_WINDOW,
                    min_valid=settings.VISION_DEPTH_MIN_VALID,
                    cluster_tol_m=settings.VISION_DEPTH_CLUSTER_TOL_M,
                    min_depth_m=settings.VISION_DEPTH_MIN_M,
                    max_depth_m=settings.VISION_DEPTH_MAX_M,
                    anchor_inset=settings.VISION_ANCHOR_INSET,
                )
                st = store.observe(d["track_id"], ts, d["bbox"], d["confidence"],
                                   sample, pos)
                rows.append(st)
                if st.depth_valid:
                    seen_valid += 1

            live = store.live(ts)
            signature = tuple(s.track_id for s in live)

            # print at ~4 Hz, or immediately when the track set changes
            if (now - last_print > 0.25 and not args.quiet) or signature != last_signature:
                last_print = now
                last_signature = signature
                print(f"\033[2K{DIM}{fps_live:5.1f} fps   active tracks: "
                      f"{len(live)}{RESET}")
                if not live:
                    print("\033[2K  (nobody detected)")
                for s in live:
                    if s.smoothed and s.depth_valid:
                        x, _y, z = s.smoothed
                        rx, _ry, rz = s.raw if s.raw else (0, 0, 0)
                        print(f"\033[2K  {s.label}  conf {s.detection_confidence:.2f}   "
                              f"X: {x:+6.2f} m   Z: {z:6.2f} m   "
                              f"Depth: {s.depth_m:5.2f} m   "
                              f"{DIM}[{s.anchor_source.lower()}  raw {rx:+.2f}/{rz:.2f}]{RESET}")
                    else:
                        print(f"\033[2K  {s.label}  conf {s.detection_confidence:.2f}   "
                              f"{YELLOW}depth invalid{RESET}  ({s.depth_reason})")
                print(f"\033[2K{DIM}{'-' * 72}{RESET}")

            if args.view:
                import cv2
                img = colour.copy()
                for s in rows:
                    if not s.bbox:
                        continue
                    x1, y1, x2, y2 = [int(v) for v in s.bbox]
                    col = (90, 200, 90) if s.depth_valid else (60, 160, 240)
                    cv2.rectangle(img, (x1, y1), (x2, y2), col, 2)
                    if s.smoothed and s.depth_valid:
                        txt = f"{s.label} X{s.smoothed[0]:+.2f} Z{s.smoothed[2]:.2f}"
                    else:
                        txt = f"{s.label} depth invalid"
                    cv2.putText(img, txt, (x1, max(y1 - 6, 12)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1, cv2.LINE_AA)
                cv2.imshow("wastraq - picker tracking (q to quit)", img)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    except KeyboardInterrupt:
        print("\n(interrupted)")
    finally:
        src.close()
        if args.view:
            try:
                import cv2
                cv2.destroyAllWindows()
            except Exception:  # noqa: BLE001
                pass

    elapsed = max(time.time() - started, 1e-6)
    print()
    line(f"frames through YOLO      : {frames}  ({frames / elapsed:.1f} fps average)")
    line(f"detections into ByteTrack: {seen_detections}")
    line(f"tracks seen              : {len(store.all())}")
    line(f"frames with valid depth  : {seen_valid}")
    if read_failures:
        line(f"frame read failures      : {read_failures}")

    if frames == 0:
        failed("no frames reached the detector")
        return stop(6, "the aligned stream stopped delivering once YOLO was in "
                       "the loop",
                    ["stages 2-5 passed, so this is timing, not the camera - "
                     "try --fps 6"])
    if seen_detections == 0:
        failed("no person was ever detected")
        return stop(6, "frames arrived but YOLO found no person", [
            "stand fully in frame, 1.5-6 m away, reasonably lit",
            f"lower the threshold: VISION_CONF={settings.VISION_CONF} is current",
        ])
    if seen_valid == 0:
        failed("a person was tracked but never got a valid depth reading")
        return stop(6, "detections arrived with no usable depth", [
            "stand 1.5-6 m from the camera, whole body visible",
            "shiny floors and glass return no depth",
        ])
    passed()

    print()
    print("=" * 62)
    print(f"{GREEN}ALL {N_STAGES} STAGES PASSED{RESET} - real frames from the "
          f"physical D455 went through YOLO + ByteTrack with valid depth.")
    print(f"  profile   : {src.profile_summary()}")
    print(f"  achieved  : {frames / elapsed:.1f} fps")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())

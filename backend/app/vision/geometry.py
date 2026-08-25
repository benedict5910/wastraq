"""Camera geometry for the picker-tracking pipeline.

Deliberately pure Python: no pyrealsense2, no OpenCV, no numpy import at
module level. Everything in here is the part that is easy to get silently
wrong - which pixel we measure, which depth samples we trust, and how a pixel
plus a depth becomes metres - so it has to be testable with no camera plugged
in. See scripts/test_vision_logic.py.

Coordinate convention
---------------------
We keep the NATIVE librealsense optical frame and never flip a sign quietly:

    +x = camera right      (subject moves to the operator's right -> x grows)
    -x = camera left
    +y = camera DOWN       (this is the native convention, not "up")
    +z = forward, away from the camera lens

For the top-down demo only x (lateral) and z (forward range) matter. y is
carried through unchanged so that a later phase can use it for a ground-plane
fit without having to undo a convention we invented here.

Frames are indexed frame[v][u] - row (y) first, column (x) second - which is
true for both a numpy 2-D array and a plain list of lists.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

# librealsense distortion model names, kept as strings so this module never
# has to import the SDK just to compare an enum.
DISTORTION_NONE = "none"
DISTORTION_MODIFIED_BROWN_CONRADY = "modified_brown_conrady"
DISTORTION_INVERSE_BROWN_CONRADY = "inverse_brown_conrady"
DISTORTION_BROWN_CONRADY = "brown_conrady"
DISTORTION_FTHETA = "ftheta"
DISTORTION_KANNALA_BRANDT4 = "kannala_brandt4"


@dataclass
class Intrinsics:
    """Pinhole intrinsics of the stream a pixel was measured on.

    After aligning depth to colour these are the COLOUR intrinsics: the
    aligned depth frame is resampled onto the colour image grid, so a pixel
    (u, v) means the same thing in both.
    """

    width: int
    height: int
    ppx: float
    ppy: float
    fx: float
    fy: float
    model: str = DISTORTION_NONE
    coeffs: tuple[float, float, float, float, float] = (0.0, 0.0, 0.0, 0.0, 0.0)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["coeffs"] = list(self.coeffs)
        return d

    @classmethod
    def from_rs(cls, intr: Any) -> "Intrinsics":
        """Build from a pyrealsense2.intrinsics without importing the SDK."""
        coeffs = tuple(float(c) for c in list(intr.coeffs)[:5])
        while len(coeffs) < 5:
            coeffs = coeffs + (0.0,)
        return cls(
            width=int(intr.width),
            height=int(intr.height),
            ppx=float(intr.ppx),
            ppy=float(intr.ppy),
            fx=float(intr.fx),
            fy=float(intr.fy),
            model=str(intr.model).rsplit(".", 1)[-1].lower(),
            coeffs=coeffs,  # type: ignore[arg-type]
        )


class UndeprojectableModel(ValueError):
    """RS2_DISTORTION_MODIFIED_BROWN_CONRADY is forward-only, as in librealsense."""


def deproject(intr: Intrinsics, u: float, v: float, depth_m: float) -> tuple[float, float, float]:
    """Pixel + depth -> 3-D point in the camera's own frame, in metres.

    A faithful port of rs2_deproject_pixel_to_point from librealsense's
    rsutil.h, branch for branch. It is reimplemented rather than called
    through the SDK for one reason: this way the maths is covered by tests
    that run with no camera attached. The pipeline logs the distortion model
    the device actually reported, so an unexpected one is visible rather than
    silently mishandled.

    Note the counter-intuitive part, which is librealsense's and not ours:
    for INVERSE_BROWN_CONRADY the *deprojection* applies the polynomial
    forwards. That model's stored coefficients already describe the
    image->ray direction, which is exactly why it is the deprojectable one.
    """
    x = (u - intr.ppx) / intr.fx
    y = (v - intr.ppy) / intr.fy

    model = (intr.model or DISTORTION_NONE).lower()
    c = list(intr.coeffs) + [0.0] * 5

    if model == DISTORTION_MODIFIED_BROWN_CONRADY:
        raise UndeprojectableModel(
            "cannot deproject from a forward-distorted image "
            "(RS2_DISTORTION_MODIFIED_BROWN_CONRADY)")

    if model == DISTORTION_INVERSE_BROWN_CONRADY:
        # What a D400 colour stream normally reports. Single pass, not a loop.
        r2 = x * x + y * y
        f = 1 + c[0] * r2 + c[1] * r2 * r2 + c[4] * r2 * r2 * r2
        ux = x * f + 2 * c[2] * x * y + c[3] * (r2 + 2 * x * x)
        uy = y * f + 2 * c[3] * x * y + c[2] * (r2 + 2 * y * y)
        x, y = ux, uy
    elif model == DISTORTION_BROWN_CONRADY:
        # A genuine inverse: iterate until the undistorted ray reprojects to
        # the pixel we were given. 10 iterations, as in librealsense.
        xo, yo = x, y
        for _ in range(10):
            r2 = x * x + y * y
            icdist = 1.0 / (1.0 + ((c[4] * r2 + c[1]) * r2 + c[0]) * r2)
            dx = 2 * c[2] * x * y + c[3] * (r2 + 2 * x * x)
            dy = 2 * c[3] * x * y + c[2] * (r2 + 2 * y * y)
            x = (xo - dx) * icdist
            y = (yo - dy) * icdist
    elif model == DISTORTION_KANNALA_BRANDT4:
        rd = math.hypot(x, y)
        if rd < 1e-9:
            rd = 1e-9
        theta = rd
        for _ in range(4):
            theta2 = theta * theta
            f = theta * (1 + theta2 * (c[0] + theta2 * (c[1] + theta2 * (c[2] + theta2 * c[3])))) - rd
            df = 1 + theta2 * (3 * c[0] + theta2 * (5 * c[1] + theta2 * (7 * c[2] + 9 * theta2 * c[3])))
            theta -= f / df
        r = math.tan(theta)
        x *= r / rd
        y *= r / rd
    # DISTORTION_NONE needs no correction at all - and the depth stream
    # itself reports exactly that.

    return (depth_m * x, depth_m * y, depth_m)


# ---------------------------------------------------------------- anchors --

GROUND = "GROUND"
TORSO = "TORSO"


def clamp_pixel(u: float, v: float, width: int, height: int) -> tuple[int, int]:
    return (
        int(min(max(round(u), 0), max(width - 1, 0))),
        int(min(max(round(v), 0), max(height - 1, 0))),
    )


def ground_anchor(bbox: Sequence[float], width: int, height: int,
                  inset_frac: float = 0.06) -> tuple[int, int]:
    """Bottom-centre of the box, lifted slightly off the very last row.

    Why not the box centre: the point we actually care about is where the
    person is STANDING, because the next phase turns that into a position on
    the ground next to a property. The box centre floats at chest height and
    moves whenever the detector clips the head or the feet.

    Why not the exact bottom edge: the last row or two of a person box
    routinely lands on the floor behind them, or on the box's own padding, so
    the depth there is the floor's, not theirs. Lifting by ~6% of the box
    height keeps the measurement on the ankles/shins.
    """
    x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
    u = (x1 + x2) / 2.0
    v = y2 - inset_frac * max(y2 - y1, 1.0)
    return clamp_pixel(u, v, width, height)


def torso_anchor(bbox: Sequence[float], width: int, height: int,
                 frac: float = 0.40) -> tuple[int, int]:
    """Fallback anchor: upper torso, ~40% down the box.

    Used only when the ground anchor produced no usable depth (feet occluded
    by a bin, cut off by the frame edge, or swallowed by the floor plane).
    The z it reports is the person's torso range, which is within a few
    centimetres of their standing range - far better than reporting nothing.
    Which anchor was used is reported per track, never hidden.
    """
    x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
    u = (x1 + x2) / 2.0
    v = y1 + frac * max(y2 - y1, 1.0)
    return clamp_pixel(u, v, width, height)


# ----------------------------------------------------------- depth sampling --


@dataclass
class DepthSample:
    valid: bool
    depth_m: float | None = None
    considered: int = 0          # pixels in the window
    valid_count: int = 0         # pixels with a usable raw depth
    used_count: int = 0          # pixels that survived cluster rejection
    reason: str = ""
    anchor: tuple[int, int] | None = None
    source: str = ""             # GROUND / TORSO
    spread_m: float | None = None  # max-min of the used samples, a jitter hint


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else 0.5 * (s[mid - 1] + s[mid])


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated percentile of an already-sorted list."""
    if not sorted_vals:
        raise ValueError("empty")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def sample_depth(
    frame: Any,
    u: int,
    v: int,
    *,
    depth_scale: float = 1.0,
    window: int = 9,
    min_valid: int = 6,
    cluster_tol_m: float = 0.30,
    min_depth_m: float = 0.3,
    max_depth_m: float = 10.0,
) -> DepthSample:
    """Robust depth at (u, v), in metres, or an explicit invalid result.

    The method, and why each step is there:

    1. Read a `window` x `window` square centred on the anchor pixel. One
       pixel is not a measurement on a stereo depth camera - the D400 leaves
       holes anywhere the IR pattern was washed out or occluded.
    2. Multiply by the device depth scale (raw uint16 -> metres) and drop
       everything outside [min_depth_m, max_depth_m]. Zero means "no data" on
       a RealSense; it must never be averaged in as "0 metres away".
    3. If fewer than `min_valid` pixels survived, return valid=False. We do
       not invent a coordinate from two lucky pixels.
    4. Reject the background. A window near someone's feet straddles the
       person and the floor several metres behind them, so a plain median can
       land in the gap between the two. Take the 25th percentile - which sits
       on the NEAR surface, i.e. the person - and keep only samples within
       `cluster_tol_m` of it.
    5. Return the median of what is left, plus its spread as a jitter hint.

    `frame` is indexed frame[v][u], so a numpy array and a list of lists both
    work; that is what lets the tests run with no camera.
    """
    half = max(int(window), 1) // 2
    vals: list[float] = []
    considered = 0

    try:
        h = len(frame)
        w = len(frame[0]) if h else 0
    except Exception:                                    # pragma: no cover
        return DepthSample(False, reason="UNREADABLE_FRAME", anchor=(u, v))

    for yy in range(v - half, v + half + 1):
        if yy < 0 or yy >= h:
            continue
        row = frame[yy]
        for xx in range(u - half, u + half + 1):
            if xx < 0 or xx >= w:
                continue
            considered += 1
            raw = row[xx]
            if raw is None:
                continue
            d = float(raw) * depth_scale
            if d <= 0.0 or d < min_depth_m or d > max_depth_m:
                continue
            vals.append(d)

    if len(vals) < min_valid:
        return DepthSample(
            False, considered=considered, valid_count=len(vals),
            reason="TOO_FEW_VALID_PIXELS", anchor=(u, v),
        )

    vals.sort()
    near = _percentile(vals, 0.25)
    kept = [d for d in vals if abs(d - near) <= cluster_tol_m]

    if len(kept) < min_valid:
        return DepthSample(
            False, considered=considered, valid_count=len(vals),
            used_count=len(kept), reason="NO_STABLE_SURFACE", anchor=(u, v),
        )

    return DepthSample(
        True,
        depth_m=round(_median(kept), 4),
        considered=considered,
        valid_count=len(vals),
        used_count=len(kept),
        reason="OK",
        anchor=(u, v),
        spread_m=round(kept[-1] - kept[0], 4),
    )


def measure_person(
    depth_frame: Any,
    bbox: Sequence[float],
    intr: Intrinsics,
    *,
    depth_scale: float = 1.0,
    window: int = 9,
    min_valid: int = 6,
    cluster_tol_m: float = 0.30,
    min_depth_m: float = 0.3,
    max_depth_m: float = 10.0,
    anchor_inset: float = 0.06,
) -> tuple[DepthSample, tuple[float, float, float] | None]:
    """Full bbox -> (depth sample, camera-frame metres) with a torso fallback.

    Returns (sample, None) when no anchor yielded a trustworthy depth. The
    caller then reports depth_valid=false and a null position rather than a
    made-up coordinate.
    """
    w, h = intr.width, intr.height

    u, v = ground_anchor(bbox, w, h, anchor_inset)
    s = sample_depth(depth_frame, u, v, depth_scale=depth_scale, window=window,
                     min_valid=min_valid, cluster_tol_m=cluster_tol_m,
                     min_depth_m=min_depth_m, max_depth_m=max_depth_m)
    s.source = GROUND

    if not s.valid:
        u2, v2 = torso_anchor(bbox, w, h)
        s2 = sample_depth(depth_frame, u2, v2, depth_scale=depth_scale, window=window,
                          min_valid=min_valid, cluster_tol_m=cluster_tol_m,
                          min_depth_m=min_depth_m, max_depth_m=max_depth_m)
        s2.source = TORSO
        if s2.valid:
            s = s2
        else:
            # Report the ground attempt - it is the one we wanted - but say
            # that the fallback failed too.
            s.reason = f"{s.reason}+TORSO_{s2.reason}"
            return s, None

    assert s.depth_m is not None
    au, av = s.anchor if s.anchor else (u, v)
    return s, deproject(intr, au, av, s.depth_m)


CONVENTION = {
    "frame": "realsense_camera_optical",
    "x_m": "+x is camera RIGHT, -x is camera LEFT",
    "y_m": "+y is camera DOWN (native librealsense convention, not flipped)",
    "z_m": "+z is FORWARD, away from the lens; this is the range",
    "units": "metres",
    "origin": "the colour sensor's optical centre; the camera is treated as a "
              "fixed stationary reference frame in this phase",
    "note": "these are camera-LOCAL coordinates. No GNSS, no vehicle pose, no "
            "latitude/longitude is derived from them in this phase.",
}

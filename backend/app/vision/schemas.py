"""Pydantic contract for the live picker-tracking API.

These mirror backend/app/schemas.py in style. They describe a LIVE camera
observation - nothing here is written to PostgreSQL in this phase.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Vec3(BaseModel):
    """A point in the camera's own optical frame, in metres."""

    x_m: float = Field(..., description="+right / -left of the camera")
    y_m: float = Field(..., description="+down (native librealsense convention)")
    z_m: float = Field(..., description="+forward from the lens - the range")


class TrajectoryPoint(BaseModel):
    t: str
    x_m: float
    z_m: float


class TrackOut(BaseModel):
    track_id: int = Field(..., description="Camera track id. NOT a worker id.")
    label: str = Field(..., examples=["TRACK-01"])
    timestamp: str | None = None
    first_seen: str | None = None
    age_s: float = 0.0
    since_seen_s: float = 0.0
    frames: int = 0

    bbox: list[float] | None = Field(None, description="[x1, y1, x2, y2] in colour-frame pixels")
    detection_confidence: float = 0.0

    depth_valid: bool = False
    depth_m: float | None = None
    depth_reason: str = ""
    anchor_px: list[int] | None = None
    anchor_source: Literal["GROUND", "TORSO"] | None = None

    camera_position_raw: Vec3 | None = None
    camera_position_smoothed: Vec3 | None = None

    trajectory: list[TrajectoryPoint] = []


class TracksResponse(BaseModel):
    camera_connected: bool
    state: str
    frame_timestamp: str | None = None
    fps: float = 0.0
    count: int = 0
    convention: dict[str, Any]
    tracks: list[TrackOut] = []


class IntrinsicsOut(BaseModel):
    width: int
    height: int
    ppx: float
    ppy: float
    fx: float
    fy: float
    model: str
    coeffs: list[float]


class VisionStatus(BaseModel):
    # The six flags the demo checklist asks for, plus the honest extras.
    camera_connected: bool = False
    color_stream_active: bool = False
    depth_stream_active: bool = False
    detector_loaded: bool = False
    tracker_active: bool = False
    fps: float = 0.0
    latest_frame_timestamp: str | None = None

    state: Literal[
        "STOPPED", "STARTING", "RUNNING", "NO_CAMERA", "ERROR", "DEGRADED"
    ] = "STOPPED"
    running: bool = False
    last_error: str | None = None
    # Import-time problems (no pyrealsense2 wheel, no ultralytics) surface
    # here instead of taking the whole backend down.
    missing_dependencies: list[str] = []

    active_tracks: int = 0
    known_tracks: int = 0
    frames_processed: int = 0
    depth_scale: float | None = None
    intrinsics: IntrinsicsOut | None = None
    convention: dict[str, Any] = {}
    config: dict[str, Any] = {}
    model: str | None = None
    tracker: str | None = None
    uptime_s: float = 0.0


class CalibrationSampleIn(BaseModel):
    label: str = Field(..., examples=["2 m forward, 1 m left"])
    expected_x_m: float
    expected_z_m: float
    track_id: int | None = Field(
        None, description="Omit to use the single live track, if there is exactly one."
    )


class CalibrationSampleOut(BaseModel):
    label: str
    captured_at: str
    track_id: int | None = None
    expected_x_m: float
    expected_z_m: float
    measured_x_m: float | None = None
    measured_z_m: float | None = None
    error_x_m: float | None = None
    error_z_m: float | None = None
    error_total_m: float | None = None
    depth_valid: bool = False
    note: str = ""

"""/vision/* - live picker tracking (camera-local coordinates only).

Route naming follows the existing backend: a JSON API under its own prefix
(`/gis`, `/survey/api`, here `/vision`), with the human page mounted on a
separate top-level path in main.py so a page URL can never shadow an API one.

Nothing here reads or writes PostgreSQL. A camera track is not a collection
event and not a property association, and this phase deliberately keeps it
that way.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from . import geometry as geo
from .pipeline import pipeline
from .schemas import (CalibrationSampleIn, CalibrationSampleOut, TracksResponse,
                      VisionStatus)
from .tracking import utcnow

router = APIRouter(prefix="/vision", tags=["vision"])


@router.get("/status", response_model=VisionStatus)
def vision_status():
    """Is the camera up, is the detector loaded, how fast is it going.

    Always answers. A camera that is unplugged is `camera_connected: false`,
    not an HTTP error - the caller is a dashboard, and the backend stays up
    whatever the hardware is doing.
    """
    return pipeline.status()


@router.get("/tracks", response_model=TracksResponse)
def vision_tracks(
    trajectory: bool = Query(True, description="Include each track's recent X/Z trail."),
    include_stale: bool = Query(
        False, description="Also return tracks not seen recently but not yet retired."),
):
    """Currently tracked people, in camera-local metres.

    A track with `depth_valid: false` still appears - it exists, we just
    cannot say where it is. Its `camera_position_*` are null rather than a
    stale or invented coordinate.
    """
    now = utcnow()
    tracks = pipeline.store.snapshot(
        now=now, live_only=not include_stale, include_trajectory=trajectory)
    st = pipeline.status()
    return {
        "camera_connected": st["camera_connected"],
        "state": st["state"],
        "frame_timestamp": st["latest_frame_timestamp"],
        "fps": st["fps"],
        "count": len(tracks),
        "convention": geo.CONVENTION,
        "tracks": tracks,
    }


@router.get("/tracks/{track_id}")
def vision_track(track_id: int):
    st = pipeline.store.get(track_id)
    if st is None:
        raise HTTPException(status_code=404, detail=f"No track {track_id}")
    return st.to_dict(include_trajectory=True)


@router.post("/start")
def vision_start():
    """Start the camera thread. Idempotent."""
    result = pipeline.start()
    result["status"] = pipeline.status()
    return result


@router.post("/stop")
def vision_stop():
    """Release the camera. Idempotent; the backend keeps running."""
    result = pipeline.stop()
    result["status"] = pipeline.status()
    return result


@router.get("/frame.jpg")
def vision_frame():
    """The most recent annotated frame, as a single JPEG."""
    jpeg, _ = pipeline.latest_jpeg()
    if jpeg is None:
        raise HTTPException(status_code=503, detail="No frame yet - is the camera running?")
    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@router.get("/stream.mjpeg")
def vision_stream():
    """Annotated live feed as multipart MJPEG.

    Frame-driven, not polled: the generator blocks on the pipeline's
    condition variable, so the browser gets a frame when there IS one and
    the camera loop is never slowed down by the viewer.
    """
    boundary = "wqframe"

    def frames():
        seq = -1
        while True:
            jpeg, seq_now = pipeline.wait_for_jpeg(seq, timeout=2.0)
            if not pipeline.running and jpeg is None:
                break
            if jpeg is None or seq_now == seq:
                continue  # timed out waiting; loop and re-check
            seq = seq_now
            yield (b"--" + boundary.encode() + b"\r\n"
                   b"Content-Type: image/jpeg\r\n"
                   b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                   + jpeg + b"\r\n")

    return StreamingResponse(
        frames(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------- calibration --
# Deliberately small: stand at a known spot, say where you are, get the error
# back. That is the whole feature. It is not a calibration suite and it does
# not change any parameter - it only measures.


@router.get("/calibration/samples", response_model=list[CalibrationSampleOut])
def calibration_list():
    return pipeline.calibration_samples


@router.post("/calibration/samples", response_model=CalibrationSampleOut)
def calibration_capture(req: CalibrationSampleIn):
    """Capture the current measured X/Z against where you say you are standing."""
    live = pipeline.store.live()
    track = None
    note = ""

    if req.track_id is not None:
        track = pipeline.store.get(req.track_id)
        if track is None:
            raise HTTPException(status_code=404, detail=f"No track {req.track_id}")
    elif len(live) == 1:
        track = live[0]
    elif not live:
        note = "no live track at capture time"
    else:
        note = f"{len(live)} live tracks - pass track_id to disambiguate"

    sample: dict = {
        "label": req.label,
        "captured_at": utcnow().isoformat(),
        "track_id": track.track_id if track else None,
        "expected_x_m": req.expected_x_m,
        "expected_z_m": req.expected_z_m,
        "measured_x_m": None,
        "measured_z_m": None,
        "error_x_m": None,
        "error_z_m": None,
        "error_total_m": None,
        "depth_valid": bool(track and track.depth_valid and track.smoothed),
        "note": note,
    }

    if track is not None and track.smoothed is not None and track.depth_valid:
        mx, _my, mz = track.smoothed
        sample.update(
            measured_x_m=round(mx, 4),
            measured_z_m=round(mz, 4),
            error_x_m=round(mx - req.expected_x_m, 4),
            error_z_m=round(mz - req.expected_z_m, 4),
            error_total_m=round(
                ((mx - req.expected_x_m) ** 2 + (mz - req.expected_z_m) ** 2) ** 0.5, 4),
        )
    elif track is not None and not sample["note"]:
        sample["note"] = f"track {track.label} has no valid depth: {track.depth_reason}"

    pipeline.calibration_samples.append(sample)
    del pipeline.calibration_samples[:-50]   # keep the panel bounded
    return sample


@router.delete("/calibration/samples")
def calibration_clear():
    n = len(pipeline.calibration_samples)
    pipeline.calibration_samples.clear()
    return {"cleared": n}

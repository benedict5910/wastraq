"""Track state: smoothing, trajectory buffer and the in-memory track store.

Nothing here touches hardware or the database. A camera track is a live,
throwaway observation - it is not a worker, not a collection episode and not
a property association, and it is deliberately not persisted. Phase 2 will
consume the trajectory buffer; until then a restart losing it costs nothing.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


def track_label(track_id: int) -> str:
    """TRACK-01 style label. A camera track id, never a worker identity."""
    return f"TRACK-{int(track_id):02d}"


# ------------------------------------------------------------- smoothing --


@dataclass
class Ema3:
    """Per-axis exponential moving average with a jump gate.

    Raw RealSense depth jitters by a few centimetres frame to frame, and the
    detector's box wobbles on top of that, so the un-smoothed dot visibly
    buzzes. A plain EMA fixes the buzz but adds lag, and lag is worse than
    jitter here: the whole point of the demo is that the dot follows you.

    The gate is the compromise. Below `max_jump_m` of movement in one update
    we smooth normally; above it we assume the person really moved (or the
    anchor snapped from feet to torso) and snap straight to the raw value.
    So standing still is steady, and walking is not laggy.
    """

    alpha: float = 0.4
    max_jump_m: float = 1.2
    value: tuple[float, float, float] | None = None
    snapped: bool = False

    def update(self, raw: tuple[float, float, float]) -> tuple[float, float, float]:
        if self.value is None:
            self.value = raw
            self.snapped = True
            return self.value
        px, py, pz = self.value
        rx, ry, rz = raw
        jump = max(abs(rx - px), abs(ry - py), abs(rz - pz))
        if jump >= self.max_jump_m:
            self.value = raw
            self.snapped = True
            return self.value
        a = self.alpha
        self.snapped = False
        self.value = (
            px + a * (rx - px),
            py + a * (ry - py),
            pz + a * (rz - pz),
        )
        return self.value

    def reset(self) -> None:
        self.value = None
        self.snapped = False


# ------------------------------------------------------------ track state --


@dataclass
class TrackState:
    track_id: int
    first_seen: datetime
    last_seen: datetime
    frames: int = 0
    bbox: tuple[float, float, float, float] | None = None
    detection_confidence: float = 0.0

    depth_valid: bool = False
    depth_m: float | None = None
    depth_reason: str = ""
    anchor_px: tuple[int, int] | None = None
    anchor_source: str = ""

    raw: tuple[float, float, float] | None = None
    smoothed: tuple[float, float, float] | None = None

    ema: Ema3 = field(default_factory=Ema3)
    trajectory: Deque[tuple[datetime, float, float]] = field(default_factory=deque)

    # how many consecutive frames arrived with no usable depth
    depth_misses: int = 0

    @property
    def label(self) -> str:
        return track_label(self.track_id)

    def observe(
        self,
        ts: datetime,
        bbox: tuple[float, float, float, float],
        confidence: float,
        depth_sample: Any | None,
        position: tuple[float, float, float] | None,
        *,
        trajectory_seconds: float = 12.0,
    ) -> None:
        self.last_seen = ts
        self.frames += 1
        self.bbox = tuple(float(b) for b in bbox)  # type: ignore[assignment]
        self.detection_confidence = float(confidence)

        if depth_sample is not None:
            self.depth_valid = bool(depth_sample.valid)
            self.depth_m = depth_sample.depth_m
            self.depth_reason = depth_sample.reason
            self.anchor_px = depth_sample.anchor
            self.anchor_source = depth_sample.source
        else:
            self.depth_valid = False
            self.depth_m = None
            self.depth_reason = "NO_DEPTH_FRAME"
            self.anchor_px = None
            self.anchor_source = ""

        if position is None:
            # The track still exists - we just cannot say where it is. Keep
            # the last known position out of the response entirely rather
            # than letting a stale coordinate look live.
            self.depth_misses += 1
            self.raw = None
            self.smoothed = None
            return

        self.depth_misses = 0
        self.raw = position
        self.smoothed = self.ema.update(position)
        self.trajectory.append((ts, self.smoothed[0], self.smoothed[2]))
        self.trim_trajectory(ts, trajectory_seconds)

    def trim_trajectory(self, now: datetime, seconds: float) -> None:
        cutoff = now.timestamp() - seconds
        while self.trajectory and self.trajectory[0][0].timestamp() < cutoff:
            self.trajectory.popleft()

    def age_s(self, now: datetime | None = None) -> float:
        now = now or utcnow()
        return round((now - self.first_seen).total_seconds(), 3)

    def since_seen_s(self, now: datetime | None = None) -> float:
        now = now or utcnow()
        return round((now - self.last_seen).total_seconds(), 3)

    def to_dict(self, *, now: datetime | None = None,
                include_trajectory: bool = True) -> dict[str, Any]:
        now = now or utcnow()

        def vec(p: tuple[float, float, float] | None) -> dict[str, float] | None:
            if p is None:
                return None
            return {"x_m": round(p[0], 4), "y_m": round(p[1], 4), "z_m": round(p[2], 4)}

        d: dict[str, Any] = {
            "track_id": self.track_id,
            "label": self.label,
            "timestamp": iso(self.last_seen),
            "first_seen": iso(self.first_seen),
            "age_s": self.age_s(now),
            "since_seen_s": self.since_seen_s(now),
            "frames": self.frames,
            "bbox": list(self.bbox) if self.bbox else None,
            "detection_confidence": round(self.detection_confidence, 4),
            "depth_valid": self.depth_valid,
            "depth_m": self.depth_m,
            "depth_reason": self.depth_reason,
            "anchor_px": list(self.anchor_px) if self.anchor_px else None,
            "anchor_source": self.anchor_source or None,
            "camera_position_raw": vec(self.raw),
            "camera_position_smoothed": vec(self.smoothed),
        }
        if include_trajectory:
            d["trajectory"] = [
                {"t": iso(t), "x_m": round(x, 4), "z_m": round(z, 4)}
                for (t, x, z) in self.trajectory
            ]
        return d


# ------------------------------------------------------------ track store --


class TrackStore:
    """Thread-safe, bounded, in-memory. The camera thread writes, HTTP reads.

    Two timers, not one:
      * `live_ttl_s`   - a track not seen for this long stops being reported
                         as live. Short.
      * `retire_s`     - only now is its history thrown away. Longer, so a
                         person walking behind a bin for a second comes back
                         with the same id and the same trail instead of
                         restarting as a new track.
    """

    def __init__(
        self,
        *,
        live_ttl_s: float = 1.5,
        retire_s: float = 6.0,
        trajectory_seconds: float = 12.0,
        max_tracks: int = 32,
    ) -> None:
        self.live_ttl_s = live_ttl_s
        self.retire_s = retire_s
        self.trajectory_seconds = trajectory_seconds
        self.max_tracks = max_tracks
        self._tracks: dict[int, TrackState] = {}
        self._lock = threading.Lock()

    # -- writes (camera thread) -------------------------------------------
    def observe(
        self,
        track_id: int,
        ts: datetime,
        bbox: tuple[float, float, float, float],
        confidence: float,
        depth_sample: Any | None,
        position: tuple[float, float, float] | None,
    ) -> TrackState:
        with self._lock:
            st = self._tracks.get(track_id)
            if st is None:
                st = TrackState(track_id=track_id, first_seen=ts, last_seen=ts)
                self._tracks[track_id] = st
            st.observe(ts, bbox, confidence, depth_sample, position,
                       trajectory_seconds=self.trajectory_seconds)
            self._prune_locked(ts)
            return st

    def prune(self, now: datetime | None = None) -> int:
        with self._lock:
            return self._prune_locked(now or utcnow())

    def _prune_locked(self, now: datetime) -> int:
        dead = [tid for tid, st in self._tracks.items()
                if st.since_seen_s(now) > self.retire_s]
        for tid in dead:
            del self._tracks[tid]
        if len(self._tracks) > self.max_tracks:
            # Keep the most recently seen. A runaway tracker must not become
            # a slow memory leak inside the backend process.
            keep = sorted(self._tracks.values(), key=lambda s: s.last_seen,
                          reverse=True)[: self.max_tracks]
            keep_ids = {s.track_id for s in keep}
            for tid in list(self._tracks):
                if tid not in keep_ids:
                    del self._tracks[tid]
                    dead.append(tid)
        return len(dead)

    def clear(self) -> None:
        with self._lock:
            self._tracks.clear()

    # -- reads (HTTP thread) ----------------------------------------------
    def live(self, now: datetime | None = None) -> list[TrackState]:
        now = now or utcnow()
        with self._lock:
            return sorted(
                (s for s in self._tracks.values() if s.since_seen_s(now) <= self.live_ttl_s),
                key=lambda s: s.track_id,
            )

    def all(self, now: datetime | None = None) -> list[TrackState]:
        with self._lock:
            return sorted(self._tracks.values(), key=lambda s: s.track_id)

    def get(self, track_id: int) -> TrackState | None:
        with self._lock:
            return self._tracks.get(track_id)

    def snapshot(self, *, now: datetime | None = None, live_only: bool = True,
                 include_trajectory: bool = True) -> list[dict[str, Any]]:
        now = now or utcnow()
        src = self.live(now) if live_only else self.all(now)
        return [s.to_dict(now=now, include_trajectory=include_trajectory) for s in src]

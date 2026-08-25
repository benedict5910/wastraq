"""Domain constants and ID helpers.

No ORM by design (see database.py). These are the shapes that move between
the SQL layer and the API layer.
"""

from dataclasses import dataclass
from datetime import datetime

from .database import fetch_one

# --- controlled vocabularies -------------------------------------------------
SEGREGATION_STATUSES = ("SEGREGATED", "NOT_SEGREGATED")
REVIEW_STATUSES = ("AUTO_CONFIRMED", "NEEDS_REVIEW", "REVIEWED_OK", "REVIEWED_REJECTED")
EVIDENCE_TYPES = (
    "COLLECTION_PROOF",
    "NON_SEGREGATION_PROOF",
    "VIDEO_CLIP",
    "CAMERA_FRAME",
)
DECISIONS = ("AUTO_ASSOCIATED", "AMBIGUOUS", "NO_MATCH")


@dataclass
class Candidate:
    property_id: str
    zone_id: str
    distance_m: float
    inside: bool


# --- human-readable sequential IDs (EVENT-001, EVID-001) ---------------------
def _next_id(table: str, column: str, prefix: str, width: int = 3) -> str:
    """Next ID of the form PREFIX-007. Demo-grade: fine for single-writer use."""
    row = fetch_one(
        f"""
        SELECT COALESCE(MAX(NULLIF(regexp_replace({column}, '^{prefix}-', ''), '')::int), 0) AS n
        FROM {table}
        WHERE {column} ~ '^{prefix}-[0-9]+$'
        """
    )
    n = (row or {}).get("n", 0) + 1
    return f"{prefix}-{n:0{width}d}"


def next_event_id() -> str:
    return _next_id("collection_events", "event_id", "EVENT")


def next_evidence_id() -> str:
    return _next_id("evidence", "evidence_id", "EVID")


def fake_evidence_path(event_id: str, evidence_type: str, when: datetime) -> str:
    """Placeholder path. Later this becomes the real camera/video artefact."""
    stamp = when.strftime("%Y%m%d-%H%M%S")
    ext = "mp4" if evidence_type == "VIDEO_CLIP" else "jpg"
    return f"/evidence/{when:%Y/%m/%d}/{event_id}_{evidence_type}_{stamp}.{ext}"

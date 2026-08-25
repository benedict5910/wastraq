from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from ..database import execute, fetch_all, fetch_one
from ..models import fake_evidence_path, next_evidence_id
from ..schemas import EvidenceCreate, EvidenceOut

router = APIRouter(tags=["evidence"])


@router.get("/collection-events/{event_id}/evidence", response_model=list[EvidenceOut])
def list_evidence(event_id: str):
    if not fetch_one("SELECT 1 AS ok FROM collection_events WHERE event_id = %s", (event_id,)):
        raise HTTPException(status_code=404, detail=f"Unknown event {event_id}")
    return fetch_all(
        "SELECT * FROM evidence WHERE event_id = %s ORDER BY captured_at", (event_id,)
    )


@router.post(
    "/collection-events/{event_id}/evidence", response_model=EvidenceOut, status_code=201
)
def add_evidence(event_id: str, req: EvidenceCreate):
    """Attach evidence to an event. File paths are placeholders in the demo;
    later this is where the real camera frame / clip lands."""
    if not fetch_one("SELECT 1 AS ok FROM collection_events WHERE event_id = %s", (event_id,)):
        raise HTTPException(status_code=404, detail=f"Unknown event {event_id}")
    captured = req.captured_at or datetime.now(timezone.utc)
    return execute(
        """
        INSERT INTO evidence (evidence_id, event_id, evidence_type, file_path, captured_at, verified)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *;
        """,
        (
            next_evidence_id(),
            event_id,
            req.evidence_type,
            req.file_path or fake_evidence_path(event_id, req.evidence_type, captured),
            captured,
            req.verified,
        ),
    )


@router.get("/evidence", response_model=list[EvidenceOut])
def all_evidence(limit: int = 200):
    return fetch_all("SELECT * FROM evidence ORDER BY captured_at DESC LIMIT %s", (limit,))

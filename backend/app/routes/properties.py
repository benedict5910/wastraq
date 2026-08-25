import os

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from ..config import settings
from ..database import fetch_all, fetch_one
from ..gis import get_property_with_gis
from ..schemas import PropertyDetailOut, PropertyOut

router = APIRouter(prefix="/properties", tags=["properties"])


@router.get("", response_model=list[PropertyOut])
def list_properties(route_id: str | None = Query(None)):
    if route_id:
        return fetch_all(
            "SELECT * FROM properties WHERE route_id = %s ORDER BY property_id", (route_id,)
        )
    return fetch_all("SELECT * FROM properties ORDER BY property_id")


@router.get("/{property_id}", response_model=PropertyDetailOut)
def get_property(property_id: str):
    row = get_property_with_gis(property_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Unknown property {property_id}")
    return row


@router.get("/{property_id}/events")
def property_events(property_id: str):
    return fetch_all(
        """
        SELECT * FROM collection_events
        WHERE property_id = %s
        ORDER BY collection_time DESC
        """,
        (property_id,),
    )


@router.get("/{property_id}/photo", include_in_schema=True, tags=["properties"])
def property_photo(property_id: str):
    """Serve the surveyed frontage photo.

    Survey QA, human verification and dispute review only. This is
    deliberately NOT part of the association path - a picker coordinate is
    matched against `property_service_zones`, never against a photo.
    """
    row = fetch_one(
        """
        SELECT file_path FROM property_photos
        WHERE property_id = %s AND photo_type = 'FRONTAGE'
        LIMIT 1
        """,
        (property_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"No frontage photo for {property_id}")

    path = os.path.realpath(os.path.expanduser(row["file_path"]))
    root = os.path.realpath(settings.PHOTO_DIR)
    # Only ever serve files from the configured photo directory.
    if os.path.commonpath([path, root]) != root:
        raise HTTPException(status_code=403, detail="Photo path is outside PHOTO_DIR")
    if not os.path.isfile(path):
        raise HTTPException(
            status_code=404,
            detail=f"Photo file missing on disk: {row['file_path']}",
        )
    return FileResponse(path)


@router.get("/{property_id}/photo-info", tags=["properties"])
def property_photo_info(property_id: str):
    row = fetch_one(
        "SELECT * FROM property_photos WHERE property_id = %s AND photo_type = 'FRONTAGE'",
        (property_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"No frontage photo for {property_id}")
    row["exists_on_disk"] = os.path.isfile(os.path.expanduser(row["file_path"]))
    row["purpose"] = "survey QA / human verification / dispute review - not live recognition"
    return row

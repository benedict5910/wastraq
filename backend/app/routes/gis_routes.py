from fastapi import APIRouter

from ..database import fetch_all
from ..gis import lookup_property
from ..schemas import LookupRequest, LookupResponse

router = APIRouter(prefix="/gis", tags=["gis"])


@router.post("/lookup", response_model=LookupResponse)
def gis_lookup(req: LookupRequest):
    """Associate a picker coordinate with a property via service-zone polygons.

    Returns AUTO_ASSOCIATED, AMBIGUOUS or NO_MATCH. It will never pick a
    property just because it happens to be the closest thing on the map.
    """
    return lookup_property(req.latitude, req.longitude, req.search_radius_m)


@router.get("/layers/service-zones")
def service_zones_geojson():
    """GeoJSON FeatureCollection - handy for the dashboard map and for QGIS."""
    rows = fetch_all(
        """
        SELECT z.zone_id, z.property_id, z.version, z.verified,
               p.house_number, p.owner_name,
               ST_AsGeoJSON(z.geometry)::json AS geom
        FROM property_service_zones z
        JOIN properties p ON p.property_id = z.property_id
        ORDER BY z.property_id
        """
    )
    return _fc(rows, id_key="zone_id")


@router.get("/layers/entrances")
def entrances_geojson():
    rows = fetch_all(
        """
        SELECT e.entrance_id, e.property_id, e.verified,
               ST_AsGeoJSON(e.geometry)::json AS geom
        FROM property_entrances e ORDER BY e.property_id
        """
    )
    return _fc(rows, id_key="entrance_id")


@router.get("/layers/frontages")
def frontages_geojson():
    rows = fetch_all(
        """
        SELECT f.frontage_id, f.property_id, f.road_side, f.verified,
               ST_AsGeoJSON(f.geometry)::json AS geom
        FROM property_frontages f ORDER BY f.property_id
        """
    )
    return _fc(rows, id_key="frontage_id")


def _fc(rows: list[dict], id_key: str) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": r[id_key],
                "geometry": r.pop("geom"),
                "properties": r,
            }
            for r in rows
        ],
    }

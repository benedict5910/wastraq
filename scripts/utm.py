"""
Minimal WGS84 <-> UTM (EPSG:326xx) transverse-Mercator projection.

Self-contained because the demo machine may not have pyproj installed. The
series is Snyder (USGS PP1395), accurate to about a millimetre well inside a
zone - far beyond what this demo needs, and exercised by a round-trip check in
`selftest()`.

The Wastraq demo lane sits at ~76.64 E, which is UTM zone 43N = EPSG:32643.
All metre-based geometry construction happens in that projected CRS; results
are converted back to EPSG:4326 before they are stored.
"""

from __future__ import annotations

import math

# WGS84
A = 6378137.0
F = 1 / 298.257223563
E2 = 2 * F - F * F
EP2 = E2 / (1 - E2)
K0 = 0.9996
FALSE_EASTING = 500000.0


def zone_for_lon(lon: float) -> int:
    return int(math.floor((lon + 180.0) / 6.0)) + 1


def epsg_for(lat: float, lon: float) -> int:
    z = zone_for_lon(lon)
    return (32600 if lat >= 0 else 32700) + z


def _central_meridian(zone: int) -> float:
    return -183.0 + 6.0 * zone


def _meridian_arc(phi: float) -> float:
    return A * (
        (1 - E2 / 4 - 3 * E2**2 / 64 - 5 * E2**3 / 256) * phi
        - (3 * E2 / 8 + 3 * E2**2 / 32 + 45 * E2**3 / 1024) * math.sin(2 * phi)
        + (15 * E2**2 / 256 + 45 * E2**3 / 1024) * math.sin(4 * phi)
        - (35 * E2**3 / 3072) * math.sin(6 * phi)
    )


def to_utm(lat: float, lon: float, zone: int) -> tuple[float, float]:
    """(lat, lon) degrees -> (easting, northing) metres in the given zone."""
    phi = math.radians(lat)
    lam = math.radians(lon)
    lam0 = math.radians(_central_meridian(zone))

    sin_p, cos_p, tan_p = math.sin(phi), math.cos(phi), math.tan(phi)
    N = A / math.sqrt(1 - E2 * sin_p * sin_p)
    T = tan_p * tan_p
    C = EP2 * cos_p * cos_p
    a_ = (lam - lam0) * cos_p
    M = _meridian_arc(phi)

    x = FALSE_EASTING + K0 * N * (
        a_
        + (1 - T + C) * a_**3 / 6
        + (5 - 18 * T + T * T + 72 * C - 58 * EP2) * a_**5 / 120
    )
    y = K0 * (
        M
        + N * tan_p * (
            a_**2 / 2
            + (5 - T + 9 * C + 4 * C * C) * a_**4 / 24
            + (61 - 58 * T + T * T + 600 * C - 330 * EP2) * a_**6 / 720
        )
    )
    if lat < 0:
        y += 10000000.0
    return x, y


def from_utm(x: float, y: float, zone: int, northern: bool = True) -> tuple[float, float]:
    """(easting, northing) metres -> (lat, lon) degrees."""
    if not northern:
        y -= 10000000.0
    lam0 = math.radians(_central_meridian(zone))

    M = y / K0
    mu = M / (A * (1 - E2 / 4 - 3 * E2**2 / 64 - 5 * E2**3 / 256))
    e1 = (1 - math.sqrt(1 - E2)) / (1 + math.sqrt(1 - E2))
    phi1 = (
        mu
        + (3 * e1 / 2 - 27 * e1**3 / 32) * math.sin(2 * mu)
        + (21 * e1**2 / 16 - 55 * e1**4 / 32) * math.sin(4 * mu)
        + (151 * e1**3 / 96) * math.sin(6 * mu)
        + (1097 * e1**4 / 512) * math.sin(8 * mu)
    )
    sin1, cos1, tan1 = math.sin(phi1), math.cos(phi1), math.tan(phi1)
    C1 = EP2 * cos1 * cos1
    T1 = tan1 * tan1
    N1 = A / math.sqrt(1 - E2 * sin1 * sin1)
    R1 = A * (1 - E2) / (1 - E2 * sin1 * sin1) ** 1.5
    D = (x - FALSE_EASTING) / (N1 * K0)

    phi = phi1 - (N1 * tan1 / R1) * (
        D**2 / 2
        - (5 + 3 * T1 + 10 * C1 - 4 * C1 * C1 - 9 * EP2) * D**4 / 24
        + (61 + 90 * T1 + 298 * C1 + 45 * T1 * T1 - 252 * EP2 - 3 * C1 * C1) * D**6 / 720
    )
    lam = lam0 + (
        D
        - (1 + 2 * T1 + C1) * D**3 / 6
        + (5 - 2 * C1 + 28 * T1 - 3 * C1 * C1 + 8 * EP2 + 24 * T1 * T1) * D**5 / 120
    ) / cos1
    return math.degrees(phi), math.degrees(lam)


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance on a sphere of the WGS84 mean radius (sanity check)."""
    R = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def selftest(verbose: bool = False) -> bool:
    """Round-trip and scale checks. Returns True if the projection is sane."""
    ok = True
    cases = [
        (12.2943, 76.6415),   # the Wastraq lane, Mysuru
        (12.9700, 77.5905),   # the old synthetic lane, Bengaluru
        (0.0, 75.0),          # equator on the zone-43 central meridian
        (28.6139, 77.2090),   # Delhi
    ]
    for lat, lon in cases:
        z = zone_for_lon(lon)
        x, y = to_utm(lat, lon, z)
        la, lo = from_utm(x, y, z)
        err_m = haversine_m(lat, lon, la, lo)
        if err_m > 0.001:
            ok = False
        if verbose:
            print(f"  round-trip {lat:>9.4f},{lon:>9.4f} zone {z} -> {err_m*1000:.4f} mm")

    # Projected ground distance must match the ellipsoidal short-line distance
    # to within a centimetre over the length of the demo lane (~82 m).
    # (A spherical haversine is NOT the right yardstick here - it is off by
    # ~0.1% at this latitude because it ignores the ellipsoid.)
    z = 43
    a = (12.2942563, 76.6418649)
    b = (12.2943477, 76.6411172)
    xa, ya = to_utm(*a, z)
    xb, yb = to_utm(*b, z)
    k = 0.5 * (_point_scale(a[0], a[1], z) + _point_scale(b[0], b[1], z))
    d_ground = math.hypot(xb - xa, yb - ya) / k
    d_ellip = _local_ellipsoidal_m(*a, *b)
    if abs(d_ground - d_ellip) > 0.01:
        ok = False
    if verbose:
        print(f"  82 m check: UTM ground {d_ground:.4f} m vs ellipsoidal "
              f"{d_ellip:.4f} m  (delta {abs(d_ground-d_ellip)*1000:.1f} mm)")
    return ok


def _local_ellipsoidal_m(lat1, lon1, lat2, lon2) -> float:
    """Short-line distance using the local radii of curvature (mm-accurate < 1 km)."""
    phi = math.radians(0.5 * (lat1 + lat2))
    s = math.sin(phi)
    w = 1 - E2 * s * s
    N = A / math.sqrt(w)                     # prime vertical
    M = A * (1 - E2) / (w**1.5)              # meridional
    dy = math.radians(lat2 - lat1) * M
    dx = math.radians(lon2 - lon1) * N * math.cos(phi)
    return math.hypot(dx, dy)


def _point_scale(lat: float, lon: float, zone: int) -> float:
    """UTM point scale factor k at (lat, lon)."""
    phi = math.radians(lat)
    a_ = math.radians(lon - _central_meridian(zone)) * math.cos(phi)
    T = math.tan(phi) ** 2
    C = EP2 * math.cos(phi) ** 2
    return K0 * (1 + (1 + C) * a_**2 / 2 + (5 - 4 * T) * a_**4 / 24)


if __name__ == "__main__":
    print("UTM self-test:")
    print("PASS" if selftest(verbose=True) else "FAIL")

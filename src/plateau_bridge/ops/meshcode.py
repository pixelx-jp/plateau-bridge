"""Standard regional mesh codes (JIS X 0410) — the join key to J-SHIS.

J-SHIS publishes its probabilistic ground-motion and shallow-ground data on the
**250 m mesh** (1/4 地域メッシュ). To attach those nationwide values to a
building we compute the building centroid's 250 m mesh code and join — no
spatial intersection needed (the mesh tiling is a pure function of lat/lon).

Mesh hierarchy (each level appends digits):

- 1st mesh  (~80 km): ``pp uu``           — 4 digits
- 2nd mesh  (~10 km): ``+ q v``           — 6 digits
- 3rd mesh  (~ 1 km): ``+ r w``           — 8 digits   (this 8-digit prefix is
  the same code PLATEAU uses in ``building_uid``, e.g. ``53394611``)
- half mesh ( 500 m): ``+ m`` (1..4)      — 9 digits
- quarter   ( 250 m): ``+ n`` (1..4)      — 10 digits

Reference point (unit-tested): Tokyo Station 35.6812, 139.7671 → ``5339461132``.
"""

from __future__ import annotations


def meshcode_250m(lat: float, lon: float) -> str:
    """Return the 10-digit JIS 250 m (1/4) mesh code for a WGS84 lat/lon.

    Deterministic; valid for Japan's coordinate range. Raises ``ValueError`` on
    non-finite or clearly out-of-range input (so a bad centroid never silently
    joins to the wrong cell).
    """
    if not (_finite(lat) and _finite(lon)):
        raise ValueError(f"non-finite coordinate: lat={lat!r} lon={lon!r}")
    if not (20.0 <= lat <= 46.5 and 122.0 <= lon <= 154.0):
        raise ValueError(f"coordinate outside Japan mesh range: lat={lat} lon={lon}")

    # 1st mesh
    p = int(lat * 1.5)              # 40-minute latitude bands
    u = int(lon - 100.0)           # 1-degree longitude bands
    lat_rem = lat * 60.0 - p * 40.0  # remaining minutes of latitude
    lon_rem = (lon - 100.0 - u) * 60.0  # remaining minutes of longitude

    # 2nd mesh (lat band 5 min, lon band 7.5 min)
    q = int(lat_rem / 5.0)
    v = int(lon_rem / 7.5)
    lat_rem -= q * 5.0
    lon_rem -= v * 7.5

    # 3rd mesh (lat 30 sec, lon 45 sec) — work in seconds from here
    lat_sec = lat_rem * 60.0
    lon_sec = lon_rem * 60.0
    r = int(lat_sec / 30.0)
    w = int(lon_sec / 45.0)
    lat_sec -= r * 30.0
    lon_sec -= w * 45.0

    # half mesh (250 m parent): lat 15 sec, lon 22.5 sec → m ∈ {1,2,3,4}
    lat_half = int(lat_sec / 15.0)
    lon_half = int(lon_sec / 22.5)
    m = lat_half * 2 + lon_half + 1
    lat_sec -= lat_half * 15.0
    lon_sec -= lon_half * 22.5

    # quarter mesh (250 m): lat 7.5 sec, lon 11.25 sec → n ∈ {1,2,3,4}
    lat_q = int(lat_sec / 7.5)
    lon_q = int(lon_sec / 11.25)
    n = lat_q * 2 + lon_q + 1

    return f"{p:02d}{u:02d}{q:d}{v:d}{r:d}{w:d}{m:d}{n:d}"


def _finite(x: float) -> bool:
    return x == x and x not in (float("inf"), float("-inf"))

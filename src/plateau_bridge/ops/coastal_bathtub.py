"""Coastline-connected "bathtub" reference for 津波 / 高潮 (terrain susceptibility).

A **deterministic, terrain-derived reference** for the residual coastal "no data"
— low-lying buildings with no official 浸水想定. NOT an official projection and
it does NOT supersede the official 想定 (the API/map only apply it where official
coverage is absent). It answers: "if the sea rose to the prefecture's maximum
modelled 想定 height, which sea-connected low ground would it reach?".

It is an *enhanced* bathtub, not the naive one: (1) only cells **8-connected to
the sea** flood (disconnected inland depressions are NOT flagged — the classic
bathtub over-prediction error, cf. Flow-Tub, Kasmalkar 2023); (2) the water level
**attenuates inland** ~20 cm/km with euclidean distance from the coast (Vafeidis
2019 NHESS 19:973; ignoring it ~doubles exposed area). The seed is the caller-
supplied **water-surface height (T.P.)** = the prefecture's max 想定 津波高/基準水位
— NOT 浸水深 (ground-relative depth): the bathtub compares water-surface elevation
to DEM ground elevation, both T.P.

Honesty/limits (must be labelled): static fill, no flow dynamics/timing; the
euclidean attenuation is an APPROXIMATION of true hydraulic path distance
(conservative, not a travel-distance claim); "low" is never "safe". Sea = GSI
**nodata only** (ocean is encoded nodata; real ≤0 m is land — ゼロメートル地帯 —
that must stay flood-RECEIVING, not flood-source). ``fetch`` is injectable.
"""

from __future__ import annotations

import logging

import numpy as np

from plateau_bridge.ops.hand import Fetch, _default_fetch, fetch_dem_mosaic, lonlat_to_3857

log = logging.getLogger(__name__)

# Inundation depth (m, attenuated water level − ground) → relative susceptibility band.
BATHTUB_HIGH_M = 3.0
BATHTUB_MED_M = 0.5
# Inland attenuation of the water level. ~20 cm/km is the Flow-Tub default
# (Kasmalkar 2023 MethodsX 12:102524; Vafeidis 2019 NHESS 19:973, range 5–100).
ATTENUATION_M_PER_M = 0.0002


def bathtub_level(depth_m: float) -> str:
    if depth_m >= BATHTUB_HIGH_M:
        return "high"
    if depth_m >= BATHTUB_MED_M:
        return "medium"
    return "low"


def compute_bathtub(
    elev: np.ndarray, seed_m: float, *, px_real: float = 10.0,
    attenuation: float = ATTENUATION_M_PER_M,
) -> np.ndarray:
    """Connectivity + attenuation bathtub inundation depth (m); NaN where dry.

    Sea = **nodata only**, connected to the grid border (GSI encodes ocean as
    nodata; real ≤0 m is land). Local water level decays inland with euclidean
    distance to the sea: ``level = seed_m − attenuation·(dist_px·px_real)`` (an
    APPROXIMATION of hydraulic path distance — conservative, not a travel claim).
    A land cell floods iff ``elev < level`` AND it is 8-connected (through
    floodable/sea cells) to the sea. Depth = level − elev; disconnected inland
    low ground stays dry (NaN).
    """
    from scipy import ndimage

    if elev.size == 0 or seed_m <= 0:
        return np.full(elev.shape, np.nan, dtype=np.float32)

    nodata = ~np.isfinite(elev)
    # GSI ocean = nodata; do NOT treat ≤0 m land as sea. Keep only border-connected
    # nodata as sea (interior nodata holes / mid-grid fetch gaps are not ocean).
    lab, n = ndimage.label(nodata)
    if n == 0:
        return np.full(elev.shape, np.nan, dtype=np.float32)  # full DEM, no ocean → no flood
    border = set(lab[0, :]) | set(lab[-1, :]) | set(lab[:, 0]) | set(lab[:, -1])
    border.discard(0)
    if not border:
        return np.full(elev.shape, np.nan, dtype=np.float32)
    sea = np.isin(lab, list(border))

    # attenuated water level: seed minus decay over euclidean distance to the sea.
    dist_px = ndimage.distance_transform_edt(~sea)
    level = (seed_m - attenuation * (dist_px * px_real)).astype(np.float32)

    # floodable land = below its local level; keep components 8-connected to the sea.
    region = sea | ((~nodata) & (elev < level) & (level > 0))
    rlab, rn = ndimage.label(region, structure=np.ones((3, 3)))
    if rn == 0:
        return np.full(elev.shape, np.nan, dtype=np.float32)
    sea_labels = set(np.unique(rlab[sea]))
    sea_labels.discard(0)
    flooded = np.isin(rlab, list(sea_labels)) & (~sea) & (~nodata)

    depth = np.full(elev.shape, np.nan, dtype=np.float32)
    depth[flooded] = (level[flooded] - elev[flooded]).astype(np.float32)
    depth[depth <= 0] = np.nan
    return depth


def apply_coastal_bathtub(
    buildings,
    kind: str,
    *,
    seed_m: float,
    enabled: bool = False,
    zoom: int = 14,
    coast_buffer_deg: float = 0.04,
    fetch: Fetch = _default_fetch,
    source_id: str = "gsi-dem-bathtub",
):
    """Add ``<kind>_susceptibility_*`` columns from a connectivity+attenuation bathtub.

    ``kind`` ∈ {tsunami, storm_surge}; ``seed_m`` = the prefecture's max 想定
    **water-surface height (T.P.)** (caller-supplied). Opt-in. Honesty: covered=false
    ⇒ level/depth null; only sea-connected low ground floods; apply ONLY where the
    official ``<kind>_covered`` is absent (it never supersedes 想定).

    ``coast_buffer_deg`` expands the DEM fetch bbox beyond the building footprint so
    a waterfront ward's adjacent **bay/ocean nodata** is in view to seed the flood-
    fill — without it a coastal ward whose bbox excludes the bay would get NO sea
    seed and (correctly per nodata-only sea) flood nothing, silently erasing the
    0 m zone. ~0.04° ≈ 4 km.
    """
    import math

    import geopandas as gpd

    out: gpd.GeoDataFrame = buildings.copy()
    n = len(out)
    out[f"{kind}_susceptibility_covered"] = False
    out[f"{kind}_susceptibility_level"] = None
    out[f"{kind}_susceptibility_depth_m"] = np.nan
    out[f"{kind}_susceptibility_source_ids"] = ""
    out[f"{kind}_susceptibility_coverage_confidence"] = "unknown"
    if not enabled or n == 0 or seed_m <= 0:
        return out

    b = coast_buffer_deg
    minlon = float(out["centroid_lon"].min()) - b
    maxlon = float(out["centroid_lon"].max()) + b
    minlat = float(out["centroid_lat"].min()) - b
    maxlat = float(out["centroid_lat"].max()) + b
    elev, x0, y0, px = fetch_dem_mosaic((minlon, minlat, maxlon, maxlat), zoom, fetch=fetch)
    px_real = px * math.cos(math.radians((minlat + maxlat) / 2))
    depth_grid = compute_bathtub(elev, seed_m, px_real=px_real)
    H, W = depth_grid.shape

    levels: list[str | None] = []
    depths: list[float] = []
    covered: list[bool] = []
    for lon, lat in zip(out["centroid_lon"], out["centroid_lat"], strict=False):
        try:
            x, y = lonlat_to_3857(float(lon), float(lat))
            col = int((x - x0) / px)
            row = int((y0 - y) / px)
        except (ValueError, TypeError):
            col = row = -1
        if 0 <= row < H and 0 <= col < W and np.isfinite(depth_grid[row, col]):
            d = float(depth_grid[row, col])
            depths.append(d)
            levels.append(bathtub_level(d))
            covered.append(True)
        else:
            depths.append(np.nan)
            levels.append(None)
            covered.append(False)

    out[f"{kind}_susceptibility_covered"] = covered
    out[f"{kind}_susceptibility_level"] = levels
    out[f"{kind}_susceptibility_depth_m"] = depths
    out[f"{kind}_susceptibility_source_ids"] = [source_id if c else "" for c in covered]
    out[f"{kind}_susceptibility_coverage_confidence"] = ["unknown"] * n
    return out

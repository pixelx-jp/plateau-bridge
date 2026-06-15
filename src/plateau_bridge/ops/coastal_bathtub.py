"""Coastline-connected "bathtub" reference for 津波 / 高潮 (terrain susceptibility).

A **deterministic, terrain-derived reference** for the residual coastal "no data"
— low-lying buildings with no official 浸水想定. NOT an official projection and
it does NOT supersede the official 想定 (the API/map only apply it where official
coverage is absent). It answers: "if the sea rose to the prefecture's maximum
modelled 想定 height, which sea-connected low ground would it reach?".

It is an *enhanced* bathtub, not the naive one: only cells **8-connected to the
sea** below the seed height flood, so disconnected inland depressions are NOT
flagged (the classic bathtub over-prediction error, cf. Flow-Tub / Williams &
Lück-Vogel 2020). The seed height is the caller-supplied per-prefecture maximum
想定 (津波/高潮) — a real official figure used as a conservative terrain what-if.

Caveats (must be labelled): static fill — no flow dynamics, timing, or
attenuation, so it OVER-estimates inland reach near the seed height; "low"
susceptibility is never "safe". ``fetch`` is injectable so tests run offline.
"""

from __future__ import annotations

import logging

import numpy as np

from plateau_bridge.ops.hand import Fetch, _default_fetch, fetch_dem_mosaic, lonlat_to_3857

log = logging.getLogger(__name__)

# Inundation depth (m, seed − ground) → relative susceptibility band.
BATHTUB_HIGH_M = 3.0
BATHTUB_MED_M = 0.5


def bathtub_level(depth_m: float) -> str:
    if depth_m >= BATHTUB_HIGH_M:
        return "high"
    if depth_m >= BATHTUB_MED_M:
        return "medium"
    return "low"


def compute_bathtub(elev: np.ndarray, seed_m: float) -> np.ndarray:
    """Connectivity bathtub inundation depth (m) for each cell; NaN where dry.

    Sea = nodata(NaN) or elev ≤ 0 cells connected to the grid border. A land cell
    floods iff elev < ``seed_m`` AND it is 8-connected (through other floodable /
    sea cells) to the sea. Depth = seed_m − elev. Disconnected inland low ground
    stays dry (NaN).
    """
    from scipy import ndimage

    if elev.size == 0 or seed_m <= 0:
        return np.full(elev.shape, np.nan, dtype=np.float32)

    nodata = ~np.isfinite(elev)
    sea_like = nodata | (elev <= 0.0)
    # keep only sea-like components touching the border (true ocean / tidal rivers),
    # not interior nodata holes.
    lab, n = ndimage.label(sea_like)
    if n == 0:
        return np.full(elev.shape, np.nan, dtype=np.float32)
    border = set(lab[0, :]) | set(lab[-1, :]) | set(lab[:, 0]) | set(lab[:, -1])
    border.discard(0)
    if not border:
        return np.full(elev.shape, np.nan, dtype=np.float32)  # no coast in view → no flood
    sea = np.isin(lab, list(border))

    # floodable region = sea ∪ land below seed; keep components that contain sea.
    region = sea | ((~nodata) & (elev < seed_m))
    rlab, rn = ndimage.label(region, structure=np.ones((3, 3)))  # 8-connectivity
    if rn == 0:
        return np.full(elev.shape, np.nan, dtype=np.float32)
    sea_labels = set(np.unique(rlab[sea]))
    sea_labels.discard(0)
    flooded = np.isin(rlab, list(sea_labels)) & (~sea) & (~nodata)

    depth = np.full(elev.shape, np.nan, dtype=np.float32)
    depth[flooded] = (seed_m - elev[flooded]).astype(np.float32)
    depth[depth <= 0] = np.nan
    return depth


def apply_coastal_bathtub(
    buildings,
    kind: str,
    *,
    seed_m: float,
    enabled: bool = False,
    zoom: int = 14,
    fetch: Fetch = _default_fetch,
    source_id: str = "gsi-dem-bathtub",
):
    """Add ``<kind>_susceptibility_*`` columns from a connectivity bathtub.

    ``kind`` ∈ {tsunami, storm_surge}; ``seed_m`` = the prefecture's max 想定
    height (caller-supplied). Opt-in. Honesty: covered=false ⇒ level/depth null;
    only sea-connected low ground floods; a future caller should apply this ONLY
    where the official ``<kind>_covered`` is absent (it never supersedes 想定).
    """
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

    minlon = float(out["centroid_lon"].min())
    maxlon = float(out["centroid_lon"].max())
    minlat = float(out["centroid_lat"].min())
    maxlat = float(out["centroid_lat"].max())
    elev, x0, y0, px = fetch_dem_mosaic((minlon, minlat, maxlon, maxlat), zoom, fetch=fetch)
    depth_grid = compute_bathtub(elev, seed_m)
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

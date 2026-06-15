"""DEM-derived landslide susceptibility via terrain slope.

A **deterministic, terrain-derived reference** layer — NOT an official
土砂災害警戒区域 designation. It answers "how steep is the ground at this
building", the dominant physical precondition for sediment disasters
(急傾斜地の崩壊 / 土石流 / 地すべり). It must be presented as a clearly-labelled
*susceptibility estimate*, never as an official hazard-zone designation, and
"low susceptibility" never means safe.

Pipeline (per city bbox):
  GSI dem_png tiles (Web-Mercator, decoded to metres) → mosaic raster
  → per-cell maximum slope angle (gradient on the metric grid)
  → sample at building centroids → bin to low/medium/high.

Slope is a *local* relief signal (unlike HAND, which is a basin-scale flow
computation), so this samples a finer DEM zoom by default and needs no flow
routing — just a numpy gradient. The level thresholds (≥15° high, ≥5° medium)
are the SAME as the runtime slope estimate in plateau-bosai's API
(gsi/slope.py). Note the *inputs* to those thresholds differ: this bake takes a
~9.5 m DEM gradient while the runtime uses a 50 m finite-difference baseline, so
on steep micro-relief the two terrain-derived estimates may land on different
levels. Both are clearly-labelled "参考推算（非公式）", so a divergence between two
reference estimates is acceptable — neither is presented as authoritative.

The mosaic stays in EPSG:3857 (the tile grid); the ~cos(lat) mercator scale
distortion is corrected to ground metres before the gradient. ``fetch`` is
injectable so tests run offline.
"""

from __future__ import annotations

import logging
import math

import numpy as np

# Reuse the GSI DEM mosaic + projection helpers from the HAND module so both
# terrain references decode the exact same elevation source identically.
from plateau_bridge.ops.hand import Fetch, _default_fetch, fetch_dem_mosaic, lonlat_to_3857

log = logging.getLogger(__name__)

# Maximum slope angle (degrees) → relative landslide susceptibility. The level
# thresholds are identical to plateau-bosai services/api/src/gsi/slope.py; the
# slope *value* fed in differs by DEM baseline (see module docstring), so the
# map and report can disagree on level. Screening-grade, not an engineering judgement.
SLOPE_HIGH_DEG = 15.0
SLOPE_MED_DEG = 5.0


def slope_level(slope_deg: float) -> str:
    if slope_deg >= SLOPE_HIGH_DEG:
        return "high"
    if slope_deg >= SLOPE_MED_DEG:
        return "medium"
    return "low"


def compute_slope_deg(elev: np.ndarray, px_real: float) -> np.ndarray:
    """Maximum slope angle (degrees) per cell from a DEM grid (metres).

    ``px_real`` = real ground metres per pixel (mercator px corrected by cos
    lat). NaN (nodata) propagates to its neighbours via the gradient, so cells
    adjacent to missing DEM stay undefined rather than fabricating a slope.
    """
    if elev.size == 0 or px_real <= 0:
        return np.full(elev.shape, np.nan, dtype=np.float32)
    # np.gradient returns d(elev)/d(row), d(elev)/d(col) in metres-per-metre.
    gy, gx = np.gradient(elev.astype(np.float64), px_real)
    grad = np.hypot(gx, gy)  # rise/run magnitude of steepest ascent
    return np.degrees(np.arctan(grad)).astype(np.float32)


LANDSLIDE_SUSC_SOURCE_ID = "gsi-dem-slope"
LANDSLIDE_SUSC_ATTRIBUTION = (
    "出典：国土地理院 数値標高モデル（DEM）をもとに地形傾斜を算出（地形からの参考推算）"
)


def apply_landslide_susceptibility(
    buildings,
    *,
    enabled: bool = False,
    zoom: int = 14,
    fetch: Fetch = _default_fetch,
    source_id: str = LANDSLIDE_SUSC_SOURCE_ID,
):
    """Add the ``landslide_susceptibility_*`` columns by sampling per-building slope.

    Opt-in: with ``enabled=False`` the columns exist but stay uncovered/null
    (offline/unit builds need no DEM). Honesty: a building whose slope is
    undefined (no DEM tile / nodata cell) stays covered=false, null.

    ``zoom`` defaults to 14 (~9.5 m/px, near GSI DEM10B native resolution) so
    local relief is preserved; HAND uses a coarser zoom because flow routing is
    a basin-scale computation.
    """
    import geopandas as gpd  # local import keeps module import cheap

    out: gpd.GeoDataFrame = buildings.copy()
    n = len(out)
    out["landslide_susceptibility_covered"] = False
    out["landslide_susceptibility_level"] = None
    out["landslide_susceptibility_slope_deg"] = np.nan
    out["landslide_susceptibility_source_ids"] = ""
    out["landslide_susceptibility_coverage_confidence"] = "unknown"
    if not enabled or n == 0:
        return out

    minlon = float(out["centroid_lon"].min())
    maxlon = float(out["centroid_lon"].max())
    minlat = float(out["centroid_lat"].min())
    maxlat = float(out["centroid_lat"].max())
    elev, x0, y0, px = fetch_dem_mosaic((minlon, minlat, maxlon, maxlat), zoom, fetch=fetch)
    # mercator px → real ground metres (×cos lat) so slope angles are physical.
    px_real = px * math.cos(math.radians((minlat + maxlat) / 2))
    slope = compute_slope_deg(elev, px_real)
    H, W = slope.shape

    levels: list[str | None] = []
    degs: list[float] = []
    covered: list[bool] = []
    for lon, lat in zip(out["centroid_lon"], out["centroid_lat"], strict=False):
        try:
            x, y = lonlat_to_3857(float(lon), float(lat))
            col = int((x - x0) / px)
            row = int((y0 - y) / px)
        except (ValueError, TypeError):
            col = row = -1
        if 0 <= row < H and 0 <= col < W and np.isfinite(slope[row, col]):
            d = float(slope[row, col])
            degs.append(d)
            levels.append(slope_level(d))
            covered.append(True)
        else:
            degs.append(np.nan)
            levels.append(None)
            covered.append(False)

    out["landslide_susceptibility_covered"] = covered
    out["landslide_susceptibility_level"] = levels
    out["landslide_susceptibility_slope_deg"] = degs
    out["landslide_susceptibility_source_ids"] = [source_id if c else "" for c in covered]
    out["landslide_susceptibility_coverage_confidence"] = ["unknown"] * n  # derived estimate
    return out

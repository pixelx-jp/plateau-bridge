"""DEM-derived **steep-slope (急傾斜地) susceptibility** via terrain slope.

A **deterministic, terrain-derived reference** layer — NOT an official
土砂災害警戒区域 designation. It proxies the dominant precondition of the
**急傾斜地の崩壊** (steep-slope collapse) mechanism only. It deliberately does
NOT capture the other two legal sediment mechanisms — **土石流** (debris flow,
which occurs on near-flat valley floors / fan apexes, 河床勾配 ≥ ~2°, and reads
"low" by slope) and **地すべり** (landslide, on gentle 5–20° slopes, driven by
geology/groundwater). For those, the official A33 / PLATEAU 土砂災害警戒区域 layer
(our "confirmed" layer) is authoritative. Present this as a clearly-labelled
*steep-slope susceptibility estimate*; "low" never means safe.

Pipeline (per city bbox):
  GSI dem_png tiles (Web-Mercator → metres) → mosaic → per-cell max slope angle
  (Horn-style central-difference gradient on the metric grid) + local relief
  (max−min over a ~50 m window) → sample at building centroids → low/med/high.

Calibration to the legal 急傾斜地 criterion (傾斜度 **30°以上 かつ 高さ5m以上**): a
building reads medium/high only when BOTH the slope ≥ threshold (≥25° medium,
≥30° high) AND the local relief ≥ 5 m (a real scarp, not a long gentle grade or
a sub-metre bump). This is computed at ~10 m DEM resolution (GSI DEM10B native),
unifying the map and the API report on one window/threshold.

The mosaic stays in EPSG:3857; the ~cos(lat) mercator scale is corrected to
ground metres before the gradient. ``fetch`` is injectable so tests run offline.
"""

from __future__ import annotations

import logging
import math

import numpy as np

# Reuse the GSI DEM mosaic + projection helpers from the HAND module so both
# terrain references decode the exact same elevation source identically.
from plateau_bridge.ops.hand import Fetch, _default_fetch, fetch_dem_mosaic, lonlat_to_3857

log = logging.getLogger(__name__)

# Slope thresholds aligned to the legal 急傾斜地 line (30°); medium = approaching it.
SLOPE_HIGH_DEG = 30.0
SLOPE_MED_DEG = 25.0
# Minimum local relief (scarp height, m) for a medium/high call — the legal
# criterion is slope AND height ≥ 5 m, so a flat-but-noisy or long-gentle cell
# below this stays "low".
RELIEF_MIN_M = 5.0
# Relief window radius in cells (~50 m at ~10 m px → 5-cell box) — the scale of an
# actual 急傾斜地 face.
_RELIEF_RADIUS = 2


def slope_level(slope_deg: float, relief_m: float = float("inf")) -> str:
    """Level from slope angle, gated by local relief (≥5 m scarp) per the legal
    急傾斜地 criterion (slope AND height). Relief defaults to ∞ (ungated) for
    callers that only have the angle."""
    if relief_m < RELIEF_MIN_M:
        return "low"
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


def compute_relief_m(elev: np.ndarray, radius: int = _RELIEF_RADIUS) -> np.ndarray:
    """Local relief (max−min elevation, m) over a (2·radius+1)² window — the scarp
    height proxy for the 急傾斜地 ≥5 m gate. NaN where the window has no data."""
    if elev.size == 0:
        return np.full(elev.shape, np.nan, dtype=np.float32)
    from scipy import ndimage
    size = 2 * radius + 1
    hi = ndimage.maximum_filter(elev, size=size, mode="nearest")
    lo = ndimage.minimum_filter(elev, size=size, mode="nearest")
    return (hi - lo).astype(np.float32)


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
    relief = compute_relief_m(elev)  # scarp-height proxy for the ≥5 m gate
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
            r = float(relief[row, col]) if np.isfinite(relief[row, col]) else 0.0
            degs.append(d)
            levels.append(slope_level(d, r))  # gated by ≥5 m relief
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

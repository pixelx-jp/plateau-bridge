"""DEM-derived inland (pluvial) flood susceptibility via depression fill-and-spill.

A **deterministic, terrain-derived reference** for 内水氾濫 (rainfall-driven
ponding) — NOT an official 内水/雨水出水浸水想定. Distinct from the fluvial
``flood_susceptibility`` (HAND) layer: pluvial ponding is governed by local
topographic depressions that fill before they spill, so — unlike HAND, which
fills sinks to extract a river network — this layer KEEPS the depressions and
measures how deep a sink each building sits in (the filling-and-spilling signal,
cf. Safer_RAIN HFSA; the depression-based Topographic Control Index family,
Huang et al. 2019 (Water 11(10):2115), which outperforms the cell-based TWI for
pluvial mapping).

Pipeline (per city bbox):
  GSI dem_png mosaic (Web-Mercator metres) → pysheds fill_depressions →
  depression depth = filled − original → label nested depressions →
  per-depression: potential ponding depth + contributing area + TCI index →
  sample at building centroids → low/medium/high by potential ponding depth.

Level is driven by the building's **potential ponding depth** (rainfall-free,
cross-city comparable, the directly-interpretable fill-spill measure); the
relative TCI = ln(A·S / V) is computed and stored alongside for transparency.
Honesty: this ignores the storm-sewer drainage network and all flow dynamics,
so it can UNDERESTIMATE peak depth; "low" is never "safe". ``fetch`` is
injectable so tests run offline.
"""

from __future__ import annotations

import logging
import math

import numpy as np

from plateau_bridge.ops.hand import Fetch, _default_fetch, fetch_dem_mosaic, lonlat_to_3857

log = logging.getLogger(__name__)

# Potential ponding depth (metres a depression holds before it spills) → level.
# Rainfall-independent: it is the geometric capacity above each building, the
# fill-spill signal. Bands are screening-grade, not an engineering depth.
POND_HIGH_M = 1.0
POND_MED_M = 0.3
# Cells whose fill amount is below this are treated as well-drained (not ponding).
_DEPRESSION_EPS_M = 0.05


def pluvial_level(pond_m: float) -> str:
    if pond_m >= POND_HIGH_M:
        return "high"
    if pond_m >= POND_MED_M:
        return "medium"
    return "low"


def compute_pluvial(elev: np.ndarray, px: float, px_real: float):
    """Per-cell (pond_depth_m, tci) from a DEM grid via depression fill-and-spill.

    ``px`` = mercator pixel size (grid units, for pysheds affine); ``px_real`` =
    real ground metres per pixel (for areas/volumes). Returns two float32 grids
    the shape of ``elev``: potential ponding depth (m, 0 where well-drained) and
    the depression Topographic Control Index ln(A·S/V) (NaN where not ponding /
    undefined). NaN nodata in the input stays NaN in both outputs.
    """
    from scipy import ndimage

    if elev.size == 0:
        z = np.full(elev.shape, np.nan, dtype=np.float32)
        return np.zeros(elev.shape, dtype=np.float32), z

    # pysheds (0.x) still calls np.in1d, removed in NumPy 2.0 → drop-in.
    if not hasattr(np, "in1d"):
        np.in1d = np.isin  # type: ignore[attr-defined]
    from affine import Affine
    from pysheds.grid import Grid
    from pysheds.view import Raster, ViewFinder

    nodata_mask = ~np.isfinite(elev)
    dem = elev.astype(np.float32).copy()
    if nodata_mask.any():
        finite = elev[~nodata_mask]
        dem[nodata_mask] = float(finite.max()) + 1000.0 if finite.size else 0.0

    affine = Affine(px, 0, 0, 0, -px, 0)
    vf = ViewFinder(affine=affine, shape=dem.shape, nodata=np.float32(np.nan))
    grid = Grid(viewfinder=vf)
    dem_r = Raster(dem, viewfinder=vf)

    pit = grid.fill_pits(dem_r)
    filled = grid.fill_depressions(pit)
    # depression depth: total amount the sink was raised vs the ORIGINAL surface to
    # make it drain = potential ponding before it spills (fill_pits + fill_depressions).
    depth = np.asarray(filled, dtype=np.float32) - dem
    depth[~np.isfinite(depth)] = 0.0
    depth[depth < 0] = 0.0

    # flow accumulation on the hydrologically-corrected surface → contributing cells.
    inflated = grid.resolve_flats(filled)
    fdir = grid.flowdir(inflated)
    acc = np.asarray(grid.accumulation(fdir), dtype=np.float64)

    # local slope (dimensionless rise/run) on the filled surface, floored.
    gy, gx = np.gradient(np.asarray(filled, dtype=np.float64), px_real)
    slope = np.hypot(gx, gy)

    # potential ponding depth per building = the fill depth at its own cell.
    pond = depth.copy()
    pond[nodata_mask] = np.nan

    # TCI per depression (relative index), assigned to all cells of the depression.
    tci = np.full(elev.shape, np.nan, dtype=np.float32)
    ponding = depth > _DEPRESSION_EPS_M
    if ponding.any():
        # 8-connectivity so a thin diagonal basin is ONE component (not split into
        # single-cell labels the area<2 gate would wrongly zero).
        labels, n = ndimage.label(ponding, structure=np.ones((3, 3)))
        cell_area = max(px_real, 1.0) ** 2
        idx = np.arange(1, n + 1)
        V = ndimage.sum(depth, labels, idx) * cell_area + 1.0  # +1 m³ guards ln/zero
        A = ndimage.maximum(acc, labels, idx) * cell_area
        S = np.maximum(ndimage.mean(slope, labels, idx), 1e-3)
        # TCI = ln(A·S/V) with LINEAR slope (Huang et al. 2019, Water 11(10):2115).
        tci_per = np.log(np.maximum(A, 1.0) * S / V).astype(np.float32)
        lut = np.concatenate([[np.nan], tci_per]).astype(np.float32)
        tci = lut[labels]
        tci[nodata_mask] = np.nan
        # Conservative single-cell-pit gate (review C6): a 1-cell depression is DEM
        # noise, not a real basin — drop its ponding so it can't read high/medium.
        # Only the literal artifact; never demotes a real multi-cell basin (no false
        # negatives). area = cell count per depression.
        area = ndimage.sum(np.ones_like(depth), labels, idx)
        tiny = idx[area < 2]
        if tiny.size:
            pond[np.isin(labels, tiny)] = 0.0
    return pond, tci


PLUVIAL_SOURCE_ID = "gsi-dem-pluvial"
PLUVIAL_ATTRIBUTION = (
    "出典：国土地理院 数値標高モデル（DEM）の地形窪地から内水（雨水出水）の起こりやすさを算出（参考推算）"
)


def apply_inland_flood_susceptibility(
    buildings,
    *,
    enabled: bool = False,
    zoom: int = 14,
    fetch: Fetch = _default_fetch,
    source_id: str = PLUVIAL_SOURCE_ID,
):
    """Add ``inland_flood_susceptibility_*`` columns by sampling per-building pluvial ponding.

    Opt-in: with ``enabled=False`` the columns exist but stay uncovered/null
    (offline/unit builds need no DEM). Honesty: a building whose cell has no DEM
    stays covered=false, null; a well-drained building is covered=true, level=low,
    pond=0 (we DID assess it — it is low, not "no data").
    """
    import geopandas as gpd

    out: gpd.GeoDataFrame = buildings.copy()
    n = len(out)
    out["inland_flood_susceptibility_covered"] = False
    out["inland_flood_susceptibility_level"] = None
    out["inland_flood_susceptibility_pond_m"] = np.nan
    out["inland_flood_susceptibility_tci"] = np.nan
    out["inland_flood_susceptibility_source_ids"] = ""
    out["inland_flood_susceptibility_coverage_confidence"] = "unknown"
    if not enabled or n == 0:
        return out

    minlon = float(out["centroid_lon"].min())
    maxlon = float(out["centroid_lon"].max())
    minlat = float(out["centroid_lat"].min())
    maxlat = float(out["centroid_lat"].max())
    elev, x0, y0, px = fetch_dem_mosaic((minlon, minlat, maxlon, maxlat), zoom, fetch=fetch)
    px_real = px * math.cos(math.radians((minlat + maxlat) / 2))
    pond, tci = compute_pluvial(elev, px, px_real)
    H, W = pond.shape

    levels: list[str | None] = []
    ponds: list[float] = []
    tcis: list[float] = []
    covered: list[bool] = []
    for lon, lat in zip(out["centroid_lon"], out["centroid_lat"], strict=False):
        try:
            x, y = lonlat_to_3857(float(lon), float(lat))
            col = int((x - x0) / px)
            row = int((y0 - y) / px)
        except (ValueError, TypeError):
            col = row = -1
        if 0 <= row < H and 0 <= col < W and np.isfinite(pond[row, col]):
            d = float(pond[row, col])
            ponds.append(d)
            t = float(tci[row, col]) if np.isfinite(tci[row, col]) else np.nan
            tcis.append(t)
            levels.append(pluvial_level(d))
            covered.append(True)
        else:
            ponds.append(np.nan)
            tcis.append(np.nan)
            levels.append(None)
            covered.append(False)

    out["inland_flood_susceptibility_covered"] = covered
    out["inland_flood_susceptibility_level"] = levels
    out["inland_flood_susceptibility_pond_m"] = ponds
    out["inland_flood_susceptibility_tci"] = tcis
    out["inland_flood_susceptibility_source_ids"] = [source_id if c else "" for c in covered]
    out["inland_flood_susceptibility_coverage_confidence"] = ["unknown"] * n
    return out

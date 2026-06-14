"""DEM-derived flood susceptibility via HAND (Height Above Nearest Drainage).

A **deterministic, terrain-derived reference** layer — NOT an official hazard
map. It answers "how low is this building above the nearest drainage it would
flow to", a peer-reviewed floodplain-screening signal (used operationally by
NOAA). It must be presented as a clearly-labelled *susceptibility estimate*,
never as an official 浸水想定 depth, and "low susceptibility" never means safe.

Pipeline (per city bbox):
  GSI dem_png tiles (Web-Mercator, decoded to metres)  → mosaic raster
  → pysheds flowdir → accumulation → drainage mask (acc ≥ threshold)
  → compute_hand → sample at building centroids → bin to low/medium/high.

The mosaic stays in EPSG:3857 (the tile grid); flow routing on it is a relative
computation, and the ~cos(lat) mercator scale distortion is immaterial for a
reference susceptibility. ``fetch`` is injectable so tests run offline.
"""

from __future__ import annotations

import io
import logging
import math
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import numpy as np

log = logging.getLogger(__name__)

_R = 6378137.0
_ORIGIN = math.pi * _R  # 20037508.342789244
DEM_TILE_URL = "https://cyberjapandata.gsi.go.jp/xyz/dem_png/{z}/{x}/{y}.png"

# HAND (metres above nearest drainage) → relative flood susceptibility.
# Low ground near drainage = high susceptibility. Bands chosen empirically so
# the signal discriminates real terrain (flat lowland skews high, hills/plateau
# skew low) rather than collapsing to one class — see docs/PERFORMANCE-style
# validation in tests. Screening-grade, not a depth.
HAND_HIGH_M = 3.0
HAND_MED_M = 10.0

# Drainage network = cells whose upstream catchment ≥ this area. A *physical*
# threshold (km²) keeps the stream density consistent across zoom levels and
# cities; too small → micro-channels everywhere → HAND≈0 everywhere (useless).
DEFAULT_DRAINAGE_KM2 = 2.0

Fetch = Callable[[str], "bytes | None"]


def _default_fetch(url: str) -> bytes | None:
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "plateau-bridge"})
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
            return r.read()
    except Exception:  # noqa: BLE001 — missing/oob tile → treat as no DEM there
        return None


def lonlat_to_3857(lon: float, lat: float) -> tuple[float, float]:
    x = _R * math.radians(lon)
    y = _R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    return x, y


def _tile_xy(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    xt = int((lon + 180.0) / 360.0 * n)
    latr = math.radians(lat)
    yt = int((1 - math.log(math.tan(latr) + 1 / math.cos(latr)) / math.pi) / 2 * n)
    return xt, yt


def _decode_dem(png: bytes) -> np.ndarray:
    """GSI dem_png → 256×256 float32 metres (invalid → NaN)."""
    from PIL import Image
    a = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"), dtype=np.float64)
    x = a[:, :, 0] * 65536 + a[:, :, 1] * 256 + a[:, :, 2]
    invalid = x == 8388608  # (128,0,0) = no data
    elev = np.where(x < 8388608, x, x - 16777216) * 0.01
    elev[invalid] = np.nan
    return elev.astype(np.float32)


def fetch_dem_mosaic(
    bbox: tuple[float, float, float, float],
    z: int,
    *,
    fetch: Fetch = _default_fetch,
    workers: int = 8,
) -> tuple[np.ndarray, float, float, float]:
    """Mosaic GSI DEM tiles covering ``bbox`` (minlon,minlat,maxlon,maxlat).

    Returns ``(elev, x0, y0, px)``: the elevation grid (metres, NaN=nodata) and
    its EPSG:3857 georef — top-left corner (x0,y0) and pixel size (px, metres).
    Missing tiles become NaN (routed around / treated as no-data).
    """
    minlon, minlat, maxlon, maxlat = bbox
    x0t, y0t = _tile_xy(minlon, maxlat, z)  # top-left tile
    x1t, y1t = _tile_xy(maxlon, minlat, z)  # bottom-right tile
    nx, ny = x1t - x0t + 1, y1t - y0t + 1
    elev = np.full((ny * 256, nx * 256), np.nan, dtype=np.float32)

    def _one(ix: int, iy: int):
        png = fetch(DEM_TILE_URL.format(z=z, x=x0t + ix, y=y0t + iy))
        return ix, iy, (_decode_dem(png) if png else None)

    coords = [(ix, iy) for iy in range(ny) for ix in range(nx)]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for ix, iy, tile in ex.map(lambda c: _one(*c), coords):
            if tile is not None:
                elev[iy * 256:(iy + 1) * 256, ix * 256:(ix + 1) * 256] = tile

    px = (2 * _ORIGIN) / (256 * 2 ** z)  # mercator metres per pixel
    x0 = x0t / (2 ** z) * (2 * _ORIGIN) - _ORIGIN
    y0 = _ORIGIN - y0t / (2 ** z) * (2 * _ORIGIN)
    return elev, x0, y0, px


def compute_hand(elev: np.ndarray, px: float, *, acc_cells: int) -> np.ndarray:
    """HAND (metres) for each cell via pysheds. NaN where undefined.

    ``acc_cells`` = flow-accumulation threshold (cells) defining the drainage
    network. Larger → sparser streams → higher HAND values.
    """
    # pysheds (0.x) still calls np.in1d, removed in NumPy 2.0 → provide the
    # documented drop-in (np.isin) so HAND works on modern NumPy.
    if not hasattr(np, "in1d"):
        np.in1d = np.isin  # type: ignore[attr-defined]
    from affine import Affine
    from pysheds.grid import Grid
    from pysheds.view import Raster, ViewFinder

    # pysheds needs a filled, finite DEM; fill nodata with a high value so it
    # doesn't create spurious sinks, then mask HAND back out where input was NaN.
    nodata_mask = ~np.isfinite(elev)
    dem = elev.copy()
    if nodata_mask.any():
        finite = elev[~nodata_mask]
        dem[nodata_mask] = float(finite.max()) + 1000.0 if finite.size else 0.0

    affine = Affine(px, 0, 0, 0, -px, 0)  # local grid; absolute origin irrelevant for HAND
    # crs left at pysheds' default (metadata only — HAND is a relative computation
    # on the metric tile grid; passing crs=None breaks pyproj).
    vf = ViewFinder(affine=affine, shape=dem.shape, nodata=np.float32(np.nan))
    grid = Grid(viewfinder=vf)
    dem_r = Raster(dem.astype(np.float32), viewfinder=vf)

    pit_filled = grid.fill_pits(dem_r)
    flooded = grid.fill_depressions(pit_filled)
    inflated = grid.resolve_flats(flooded)
    fdir = grid.flowdir(inflated)
    acc = grid.accumulation(fdir)
    drainage = acc >= acc_cells
    if not bool(np.any(drainage)):
        return np.full(elev.shape, np.nan, dtype=np.float32)
    hand = grid.compute_hand(fdir, inflated, drainage)
    hand = np.asarray(hand, dtype=np.float32)
    hand[nodata_mask] = np.nan
    return hand


def hand_level(hand_m: float) -> str:
    if hand_m <= HAND_HIGH_M:
        return "high"
    if hand_m <= HAND_MED_M:
        return "medium"
    return "low"


FLOOD_SUSC_SOURCE_ID = "gsi-dem-hand"
FLOOD_SUSC_ATTRIBUTION = "出典：国土地理院 数値標高モデル（DEM）をもとに HAND を算出（地形からの参考推算）"


def apply_flood_susceptibility(
    buildings,
    *,
    enabled: bool = False,
    zoom: int = 13,
    drainage_area_km2: float = DEFAULT_DRAINAGE_KM2,
    acc_cells: int | None = None,
    fetch: Fetch = _default_fetch,
    source_id: str = FLOOD_SUSC_SOURCE_ID,
):
    """Add the ``flood_susceptibility_*`` columns by sampling per-building HAND.

    Opt-in: with ``enabled=False`` the columns exist but stay uncovered/null
    (offline/unit builds need no DEM). Honesty: a building whose HAND is
    undefined (no DEM / outside drainage solution) stays covered=false, null.

    The drainage network is derived from a physical catchment threshold
    (``drainage_area_km2``); pass ``acc_cells`` to override it directly (tests).
    """
    import geopandas as gpd  # local import keeps module import cheap

    out: gpd.GeoDataFrame = buildings.copy()
    n = len(out)
    out["flood_susceptibility_covered"] = False
    out["flood_susceptibility_level"] = None
    out["flood_susceptibility_hand_m"] = np.nan
    out["flood_susceptibility_source_ids"] = ""
    out["flood_susceptibility_coverage_confidence"] = "unknown"
    if not enabled or n == 0:
        return out

    minlon = float(out["centroid_lon"].min())
    maxlon = float(out["centroid_lon"].max())
    minlat = float(out["centroid_lat"].min())
    maxlat = float(out["centroid_lat"].max())
    elev, x0, y0, px = fetch_dem_mosaic((minlon, minlat, maxlon, maxlat), zoom, fetch=fetch)
    if acc_cells is None:
        # mercator px → real ground metres (×cos lat); catchment km² → cell count.
        px_real = px * math.cos(math.radians((minlat + maxlat) / 2))
        acc_cells = max(1, int(drainage_area_km2 * 1e6 / (px_real ** 2)))
    hand = compute_hand(elev, px, acc_cells=acc_cells)
    H, W = hand.shape

    levels: list[str | None] = []
    hands: list[float] = []
    covered: list[bool] = []
    for lon, lat in zip(out["centroid_lon"], out["centroid_lat"], strict=False):
        try:
            x, y = lonlat_to_3857(float(lon), float(lat))
            col = int((x - x0) / px)
            row = int((y0 - y) / px)
        except (ValueError, TypeError):
            col = row = -1
        if 0 <= row < H and 0 <= col < W and np.isfinite(hand[row, col]):
            h = float(hand[row, col])
            hands.append(h)
            levels.append(hand_level(h))
            covered.append(True)
        else:
            hands.append(np.nan)
            levels.append(None)
            covered.append(False)

    out["flood_susceptibility_covered"] = covered
    out["flood_susceptibility_level"] = levels
    out["flood_susceptibility_hand_m"] = hands
    out["flood_susceptibility_source_ids"] = [source_id if c else "" for c in covered]
    out["flood_susceptibility_coverage_confidence"] = ["unknown"] * n  # derived estimate, not official
    return out

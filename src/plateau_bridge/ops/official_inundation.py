"""Ingest OFFICIAL inundation assumptions (浸水想定) from GSI raster tiles.

GSI's 重ねるハザードマップ serves the nationwide 国土交通省 浸水想定 as XYZ PNG
raster tiles. This is **official data** (an official model estimate — 浸水想定 —
not a per-building survey), so where a tile shows colour at a building it is a
designated inundation area → official coverage. Transparent / missing tiles =
outside the designated area (or 未整備) → not covered here (falls through to the
terrain-derived reference layers or the out-of-scope classification).

It fills the EXISTING per-hazard columns (``<kind>_covered``, ``<kind>_depth_max``,
``<kind>_coverage_confidence``) only where they are not ALREADY covered by the
PLATEAU hazard intersection — i.e. it adds official coverage in cities/hazards
PLATEAU lacked (notably 津波/高潮), turning "no data" into a known official risk,
without clobbering PLATEAU's own data.

Depth is read from the standard 国交省 浸水深 "newlegend" palette (nearest-colour
match); it is the legend BAND midpoint, not a precise value — honest because the
source itself is a banded raster of an official model estimate.

``fetch`` is injectable so tests run offline.
"""

from __future__ import annotations

import io
import logging
import math
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import numpy as np

log = logging.getLogger(__name__)

Fetch = Callable[[str], "bytes | None"]

# GSI 重ねるハザードマップ raster endpoints (verified live). Depth hazards only;
# landslide is a zone product handled elsewhere.
GSI_HAZARD_TILES: dict[str, str] = {
    "river_flood": "https://disaportaldata.gsi.go.jp/raster/01_flood_l2_shinsuishin_data/{z}/{x}/{y}.png",
    "tsunami": "https://disaportaldata.gsi.go.jp/raster/04_tsunami_newlegend_data/{z}/{x}/{y}.png",
    "storm_surge": "https://disaportaldata.gsi.go.jp/raster/03_hightide_l2_shinsuishin_data/{z}/{x}/{y}.png",
    "inland_flood": "https://disaportaldata.gsi.go.jp/raster/02_naisui_data/{z}/{x}/{y}.png",
}

OFFICIAL_INUNDATION_SOURCE = "gsi-shinsuishin"

# Standard 国土交通省 浸水深 "newlegend" palette → band midpoint depth (m). Nearest
# colour match (flat polygon fills, lossless PNG → reliable). Any opaque pixel not
# near a band still counts as covered (depth defaults to the shallowest band).
_DEPTH_PALETTE: tuple[tuple[tuple[int, int, int], float], ...] = (
    ((247, 245, 169), 0.25),   # < 0.5 m
    ((255, 255, 179), 0.25),   # < 0.5 m (variant)
    ((248, 225, 166), 1.75),   # 0.5 – 3 m
    ((255, 216, 192), 4.0),    # 3 – 5 m
    ((255, 183, 183), 7.5),    # 5 – 10 m
    ((255, 145, 145), 15.0),   # 10 – 20 m
    ((242, 133, 201), 20.0),   # ≥ 20 m
)
_PALETTE_RGB = np.array([c for c, _ in _DEPTH_PALETTE], dtype=np.int16)
_PALETTE_DEPTH = np.array([d for _, d in _DEPTH_PALETTE], dtype=np.float32)


def _default_fetch(url: str) -> bytes | None:
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "plateau-bridge"})
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
            return r.read()
    except Exception:  # noqa: BLE001 — 404 / missing tile = no designated area here
        return None


def _tile_xy_f(lon: float, lat: float, z: int) -> tuple[float, float]:
    n = 2 ** z
    xf = (lon + 180.0) / 360.0 * n
    latr = math.radians(lat)
    yf = (1 - math.log(math.tan(latr) + 1 / math.cos(latr)) / math.pi) / 2 * n
    return xf, yf


def fetch_rgba_mosaic(
    bbox: tuple[float, float, float, float], z: int, tmpl: str,
    *, fetch: Fetch = _default_fetch, workers: int = 8,
) -> tuple[np.ndarray, int, int]:
    """Mosaic GSI hazard RGBA tiles covering ``bbox``. Missing tiles stay fully
    transparent. Returns ``(rgba, x0_tile, y0_tile)`` where pixel (row,col) maps
    to a lon/lat via the XYZ tile grid at zoom ``z``."""
    minlon, minlat, maxlon, maxlat = bbox
    x0f, y0f = _tile_xy_f(minlon, maxlat, z)
    x1f, y1f = _tile_xy_f(maxlon, minlat, z)
    x0, y0, x1, y1 = int(x0f), int(y0f), int(x1f), int(y1f)
    nx, ny = x1 - x0 + 1, y1 - y0 + 1
    mosaic = np.zeros((ny * 256, nx * 256, 4), dtype=np.uint8)

    def _one(ix: int, iy: int):
        png = fetch(tmpl.format(z=z, x=x0 + ix, y=y0 + iy))
        if not png:
            return ix, iy, None
        from PIL import Image
        return ix, iy, np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))

    coords = [(ix, iy) for iy in range(ny) for ix in range(nx)]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for ix, iy, tile in ex.map(lambda c: _one(*c), coords):
            if tile is not None:
                mosaic[iy * 256:(iy + 1) * 256, ix * 256:(ix + 1) * 256] = tile
    return mosaic, x0, y0


def _depth_for_color(rgb: np.ndarray) -> float:
    """Nearest 浸水深 legend band midpoint (m) for an opaque pixel colour."""
    d = np.sum((_PALETTE_RGB - rgb.astype(np.int16)) ** 2, axis=1)
    return float(_PALETTE_DEPTH[int(np.argmin(d))])


def _is_covered(v: object) -> bool:
    """PLATEAU's <kind>_covered is a real bool/None; treat only True as covered."""
    return v is True or v == 1 or v == "true" or v == "1"


def apply_official_inundation(
    buildings,
    kind: str,
    *,
    enabled: bool = False,
    zoom: int = 15,
    fetch: Fetch = _default_fetch,
):
    """Overlay GSI official 浸水想定 onto the existing ``<kind>_*`` columns.

    Only fills buildings whose ``<kind>_covered`` is not already True (keeps
    PLATEAU's own data). A building on a coloured pixel → covered=True,
    depth_max=band midpoint, coverage_confidence='inundation_bounded'. Opt-in;
    with ``enabled=False`` the dataframe is returned unchanged.
    """
    import geopandas as gpd

    out: gpd.GeoDataFrame = buildings.copy()
    n = len(out)
    if not enabled or n == 0 or kind not in GSI_HAZARD_TILES:
        return out

    covered_col, depth_col = f"{kind}_covered", f"{kind}_depth_max"
    conf_col = f"{kind}_coverage_confidence"
    if covered_col not in out.columns:
        out[covered_col] = False
    if depth_col not in out.columns:
        out[depth_col] = np.nan
    if conf_col not in out.columns:
        out[conf_col] = None

    minlon = float(out["centroid_lon"].min())
    maxlon = float(out["centroid_lon"].max())
    minlat = float(out["centroid_lat"].min())
    maxlat = float(out["centroid_lat"].max())
    mosaic, x0t, y0t = fetch_rgba_mosaic((minlon, minlat, maxlon, maxlat), zoom,
                                         GSI_HAZARD_TILES[kind], fetch=fetch)
    H, W = mosaic.shape[:2]

    cov = out[covered_col].tolist()
    dep = out[depth_col].tolist()
    conf = out[conf_col].tolist()
    filled = 0
    for i, (lon, lat) in enumerate(zip(out["centroid_lon"], out["centroid_lat"], strict=False)):
        if _is_covered(cov[i]):
            continue  # keep PLATEAU's own coverage; don't clobber
        try:
            xf, yf = _tile_xy_f(float(lon), float(lat), zoom)
            col = int((xf - x0t) * 256)
            row = int((yf - y0t) * 256)
        except (ValueError, TypeError):
            continue
        if not (0 <= row < H and 0 <= col < W):
            continue
        px = mosaic[row, col]
        if px[3] == 0:
            continue  # transparent → outside designated area / 未整備
        cov[i] = True
        dep[i] = _depth_for_color(px[:3])
        conf[i] = "inundation_bounded"
        filled += 1

    out[covered_col] = cov
    out[depth_col] = dep
    out[conf_col] = conf
    log.info("official_inundation %s: +%d buildings from GSI 浸水想定", kind, filled)
    return out

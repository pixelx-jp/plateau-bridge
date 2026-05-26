"""都市規劃 GML (i-UR urf) → zoning_use, far_max.

We convert the urf CityGML to GeoJSON and then attribute-join to buildings by
``intersects`` (a building's zoning is whichever 用途地域 polygon contains its
footprint centroid).
"""

from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd

from plateau_bridge.sources.citygml import convert_buildings

log = logging.getLogger(__name__)

# PLATEAU urf attribute name → our normalised column.
USE_COL_CANDIDATES = ("urf:function", "function", "useDistrict", "用途地域")
FAR_COL_CANDIDATES = ("urf:floorAreaRatio", "floorAreaRatio", "容積率")


def _pick_column(gdf: gpd.GeoDataFrame, candidates: tuple[str, ...]) -> str | None:
    for c in candidates:
        if c in gdf.columns:
            return c
    return None


def load_zoning(
    urf_dir: Path,
    work_dir: Path,
    *,
    converter_bin: str = "plateau-gis-converter",
) -> gpd.GeoDataFrame:
    """Return a GeoDataFrame with columns ``zoning_use`` and ``far_max`` in EPSG:4326."""
    work_dir.mkdir(parents=True, exist_ok=True)
    # urf bundles many feature classes (UseDistrict, FirePreventionDistrict,
    # HeightControlDistrict, ...). For zoning_use we want 用途地域 = UseDistrict.
    res = convert_buildings(
        urf_dir, work_dir, converter_bin=converter_bin, emit_3dtiles=False,
        feature_name="UseDistrict",
    )
    gdf = gpd.read_file(res.geojson_path)
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)

    use_col = _pick_column(gdf, USE_COL_CANDIDATES)
    far_col = _pick_column(gdf, FAR_COL_CANDIDATES)
    out = gdf[["geometry"]].copy()
    out["zoning_use"] = gdf[use_col] if use_col else None
    out["far_max"] = gdf[far_col].astype(float) if far_col else None
    return out

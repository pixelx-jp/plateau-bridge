"""Download and parse the 5 PLATEAU hazard themes."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd

from plateau_parquet.catalog import DatasetEntry
from plateau_parquet.schema import HazardKind
from plateau_parquet.sources.citygml import convert_buildings

log = logging.getLogger(__name__)

# Heuristic depth-rank → meters mapping used when a polygon is tagged by
# numeric rank (legacy schemas).
RANK_TO_METERS: dict[int, float] = {1: 0.5, 2: 3.0, 3: 5.0, 4: 10.0, 5: 15.0}

# PLATEAU河川浸水 rank labels (Japanese, as emitted by nusamai). The keys are
# what we see in floodingRiskAttribute[].rank; values are the *upper* meter
# bound of each rank (we deliberately pick the upper end so risk is not
# underestimated downstream).
RANK_LABEL_TO_METERS: dict[str, float] = {
    "0.5m未満": 0.5,
    "0.5m以上3m未満": 3.0,
    "3m以上5m未満": 5.0,
    "5m以上10m未満": 10.0,
    "10m以上20m未満": 20.0,
    "20m以上": 20.0,
}

# Which nusamai feature class file to read for each hazard kind. Verified
# against PLATEAU Shibuya 2023:
#   fld → WaterBody
#   lsld → SedimentDisasterProneArea
#   ifld/tnm/htd → likewise carried under WaterBody-derived classes (TBD per release)
HAZARD_FEATURE_NAME: dict[HazardKind, str] = {
    HazardKind.RIVER_FLOOD: "WaterBody",
    HazardKind.INLAND_FLOOD: "WaterBody",
    HazardKind.TSUNAMI: "WaterBody",
    HazardKind.STORM_SURGE: "WaterBody",
    HazardKind.LANDSLIDE: "SedimentDisasterProneArea",
}


@dataclass(frozen=True)
class HazardLayer:
    kind: HazardKind
    inundation_gdf: gpd.GeoDataFrame
    """Polygons with `depth_m` (float) or `in_zone` (bool) columns."""
    source_id: str


def _extract_rank_from_attr(value: object) -> float | None:
    """Pull the depth rank from a ``floodingRiskAttribute`` JSON-encoded list.

    nusamai emits ``floodingRiskAttribute`` as a JSON string containing a list
    of dicts; each dict has a ``rank`` key (Japanese label like ``"0.5m未満"``).
    We return the maximum meters across the list, or None.
    """
    import json
    if value is None:
        return None
    if isinstance(value, str):
        if not value or value[0] not in "[{":
            return RANK_LABEL_TO_METERS.get(value)
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
    else:
        parsed = value
    entries = parsed if isinstance(parsed, list) else [parsed]
    best: float | None = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rank = entry.get("rank") or entry.get("depthRank")
        meters: float | None = None
        if isinstance(rank, str):
            meters = RANK_LABEL_TO_METERS.get(rank)
        elif isinstance(rank, (int, float)):
            meters = RANK_TO_METERS.get(int(rank))
        if meters is not None:
            best = meters if best is None else max(best, meters)
    return best


def normalise_depth(gdf: gpd.GeoDataFrame, kind: HazardKind) -> gpd.GeoDataFrame:
    """Add a `depth_m` column (or `in_zone` for landslide).

    Three real-data shapes handled:

    - Landslide: every polygon is a warning zone; set ``in_zone = True``.
    - Modern PLATEAU floods (verified against Shibuya 2023): depth lives in
      ``floodingRiskAttribute[].rank`` as a Japanese label string.
    - Legacy: top-level ``depth`` / ``depth_rank`` / ``rank`` columns.
    """
    if kind == HazardKind.LANDSLIDE:
        gdf = gdf.copy()
        gdf["in_zone"] = True
        return gdf

    gdf = gdf.copy()
    if "floodingRiskAttribute" in gdf.columns:
        gdf["depth_m"] = gdf["floodingRiskAttribute"].apply(_extract_rank_from_attr).astype(float)
    elif "depth" in gdf.columns:
        gdf["depth_m"] = gdf["depth"].astype(float)
    elif "depth_rank" in gdf.columns:
        gdf["depth_m"] = gdf["depth_rank"].map(RANK_TO_METERS).astype(float)
    elif "rank" in gdf.columns:
        gdf["depth_m"] = gdf["rank"].map(RANK_TO_METERS).astype(float)
    else:
        log.warning("no depth column for %s; setting NaN", kind)
        gdf["depth_m"] = float("nan")
    return gdf


def load_hazard(
    entry: DatasetEntry,
    citygml_dir: Path,
    work_dir: Path,
    *,
    converter_bin: str = "plateau-gis-converter",
    bbox: tuple[float, float, float, float] | None = None,
) -> HazardLayer:
    """Convert the hazard CityGML to GeoJSON and normalise the depth column.

    Args:
        bbox: optional ``(minx, miny, maxx, maxy)`` in EPSG:4326. When given,
            pyogrio filters features at read time via GDAL — a huge speedup
            for prefecture-wide flood layers that span far beyond the city's
            admin extent (Osaka 大阪市 river_flood is 3.4 GB unfiltered, but
            only ~10% lies within the 27100 admin polygon).
    """
    assert entry.theme == "hazard" and entry.hazard_kind is not None
    work_dir.mkdir(parents=True, exist_ok=True)
    res = convert_buildings(
        citygml_dir, work_dir, converter_bin=converter_bin, emit_3dtiles=False,
        feature_name=HAZARD_FEATURE_NAME[entry.hazard_kind],
    )
    if bbox is not None:
        gdf = gpd.read_file(res.geojson_path, bbox=bbox)
    else:
        gdf = gpd.read_file(res.geojson_path)
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)
    gdf = normalise_depth(gdf, entry.hazard_kind)
    return HazardLayer(
        kind=entry.hazard_kind,
        inundation_gdf=gdf,
        source_id=entry.dataset_id,
    )

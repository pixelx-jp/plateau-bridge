"""Gate C: PMTiles + per-ward FGB + zoning backfill + CORS verification.

Depends on Gate A's parquet (and ideally Gate B's tile columns, but it does
not require them).
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import httpx

from plateau_parquet.admin import load_wards
from plateau_parquet.catalog import CityCatalog
from plateau_parquet.config import load_settings
from plateau_parquet.ops.flatgeobuf import write_per_ward_fgb
from plateau_parquet.ops.pmtiles import write_pmtiles
from plateau_parquet.sources.citygml import convert_pmtiles
from plateau_parquet.sources.download import fetch_and_unzip
from plateau_parquet.sources.zoning import load_zoning

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GateCResult:
    pmtiles_path: Path
    fgb_paths: list[Path]
    buildings_parquet: Path


def _backfill_zoning(
    gdf: gpd.GeoDataFrame, zoning_gdf: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    """Spatial join centroid → zoning polygon; write zoning_use, far_max."""
    centroids = gdf.copy()
    centroids["geometry"] = centroids.geometry.representative_point()
    joined = gpd.sjoin(
        centroids[["geometry"]].reset_index(),
        zoning_gdf[["geometry", "zoning_use", "far_max"]],
        how="left",
        predicate="within",
    )
    # Multiple zoning polygons may match a centroid; keep the first.
    joined = joined.drop_duplicates(subset="index", keep="first").set_index("index")
    out = gdf.copy()
    out["zoning_use"] = joined["zoning_use"].reindex(gdf.index)
    out["far_max"] = joined["far_max"].reindex(gdf.index)
    return out


def run_gate_c(
    gdf: gpd.GeoDataFrame,
    catalog: CityCatalog,
    out_dir: Path,
    *,
    verify_cors_urls: list[str] | None = None,
) -> GateCResult:
    settings = load_settings()

    # 1. zoning_use / far_max backfill.
    zoning_entry = catalog.zoning()
    if zoning_entry is not None:
        urf_root = fetch_and_unzip(zoning_entry.url, settings.cache_dir / "datasets")
        urf_src = urf_root / zoning_entry.udx_subdir if zoning_entry.udx_subdir else urf_root
        zoning_gdf = load_zoning(
            urf_src, out_dir / "_work" / "urf", converter_bin=settings.converter_bin
        )
        gdf = _backfill_zoning(gdf, zoning_gdf)
    else:
        log.info("no zoning entry in catalog; skipping backfill")

    # 2. Re-persist parquet with zoning columns.
    parquet_path = out_dir / "buildings.parquet"
    gdf.to_parquet(parquet_path, index=False)

    # 3. PMTiles.
    #
    # Preferred path: tippecanoe over the enriched GeoDataFrame (carries every
    # hazard 4-tuple as scalar properties — what downstream 2D risk maps need).
    #
    # Fallback path: nusamai's native ``--sink pmtiles`` directly from CityGML.
    # Loses the hazard joins but always works; we ship it when tippecanoe is
    # unavailable so the bundle is never missing a 2D layer.
    pmtiles_path = out_dir / "buildings.pmtiles"
    bldg_entry = catalog.building()
    try:
        geojson_tmp = out_dir / "_work" / "for_pmtiles.geojson"
        geojson_tmp.parent.mkdir(parents=True, exist_ok=True)
        gdf.to_file(geojson_tmp, driver="GeoJSON")
        write_pmtiles(geojson_tmp, pmtiles_path, tippecanoe_bin=settings.tippecanoe_bin)
    except RuntimeError as e:
        log.warning("tippecanoe path failed (%s); falling back to nusamai pmtiles sink", e)
        bldg_root = fetch_and_unzip(bldg_entry.url, settings.cache_dir / "datasets")
        bldg_src = bldg_root / bldg_entry.udx_subdir if bldg_entry.udx_subdir else bldg_root
        convert_pmtiles(bldg_src, pmtiles_path, converter_bin=settings.converter_bin)

    # 4. Per-ward FGB. For 政令指定都市 we have sub-ward polygons bundled;
    # for Tokyo special wards / small cities the city already IS one ward.
    fgb_dir = out_dir / "buildings"
    wards = load_wards(catalog.city_code)
    fgb_paths = write_per_ward_fgb(gdf, fgb_dir, city_code=catalog.city_code, wards=wards)

    # 5. CORS + Range verification — useful in CI when an HTTP base is provided.
    if verify_cors_urls:
        _verify_cors_and_range(verify_cors_urls)

    return GateCResult(
        pmtiles_path=pmtiles_path,
        fgb_paths=fgb_paths,
        buildings_parquet=parquet_path,
    )


def _verify_cors_and_range(urls: list[str]) -> None:
    """Hit each URL with a Range request and check CORS headers; raise on failure."""
    for url in urls:
        r = httpx.head(url, headers={"Range": "bytes=0-1023", "Origin": "https://example.test"}, timeout=10)
        r.raise_for_status()
        if r.headers.get("Access-Control-Allow-Origin") != "*":
            raise RuntimeError(f"CORS not configured for {url}")
        if r.headers.get("Accept-Ranges") != "bytes":
            raise RuntimeError(f"Range requests not supported for {url}")
        log.info("verified CORS + Range for %s", url)
    # silence "imported but unused" when stdlib subprocess isn't used.
    _ = subprocess
    _ = json

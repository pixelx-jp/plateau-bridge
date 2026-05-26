"""Re-run hazard intersection on an existing buildings.parquet.

For huge cities (e.g. 大阪市's 615k × 64k flood polygons take >1 hour), Gate A
is often run with ``--no-hazards`` for a fast first artifact. This module
lets users add hazards later without re-running Gate A's expensive CityGML →
GeoJSON → admin-clip pipeline.

Reads:  ``out_dir/buildings.parquet`` + cached hazard GeoJSON from ``_work/``
Writes: in-place overwrite of ``buildings.parquet`` + updated ``manifest.json``
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd

from plateau_parquet.admin import ADMIN_PROVENANCE, load_admin
from plateau_parquet.catalog import CityCatalog
from plateau_parquet.config import load_settings
from plateau_parquet.manifest import build_manifest, write_manifest
from plateau_parquet.ops.attributes import field_coverage
from plateau_parquet.ops.intersect import apply_coverage, apply_hazards
from plateau_parquet.pipeline.gate_a import ATTRIBUTE_FIELDS_FOR_COVERAGE
from plateau_parquet.sources.coverage import resolve_coverage
from plateau_parquet.sources.download import fetch_and_unzip
from plateau_parquet.sources.hazard import load_hazard

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HazardOnlyResult:
    buildings_parquet: Path
    manifest_path: Path
    n_buildings: int


def run_hazard_only(
    catalog: CityCatalog,
    out_dir: Path,
    *,
    admin_boundary: gpd.GeoDataFrame | None = None,
) -> HazardOnlyResult:
    """Add hazard fields to an existing Gate-A parquet.

    Re-uses the cached hazard GeoJSON in ``out_dir/_work/hzd_*`` when present,
    so re-running is cheap if the converter has already touched the data once.
    """
    settings = load_settings()
    parquet_path = out_dir / "buildings.parquet"
    if not parquet_path.exists():
        raise FileNotFoundError(
            f"{parquet_path} not found — run `plateau build --gates A` first"
        )

    gdf = gpd.read_parquet(parquet_path)
    log.info("loaded existing %d buildings", len(gdf))

    if admin_boundary is None:
        admin_boundary = load_admin(catalog.city_code)
    notes: list[str] = []
    if admin_boundary is not None:
        notes.append(f"admin_boundary source: bundled ({ADMIN_PROVENANCE})")
    hazard_bbox = (
        tuple(admin_boundary.total_bounds.tolist()) if admin_boundary is not None else None
    )

    work = out_dir / "_work"
    extents = []
    layers = []
    for kind, entry in catalog.hazards().items():
        root = fetch_and_unzip(entry.url, settings.cache_dir / "datasets")
        src = root / entry.udx_subdir if entry.udx_subdir else root
        if not src.exists():
            log.warning("hazard %s subdir %s missing — unknown", kind, entry.udx_subdir)
            ext = resolve_coverage(entry, admin_boundary, dataset_root=root)
            if ext is not None:
                extents.append(ext)
            continue
        try:
            layer = load_hazard(
                entry, src, work / f"hzd_{kind.value}",
                converter_bin=settings.converter_bin,
                bbox=hazard_bbox,
            )
            layers.append(layer)
        except Exception as e:  # noqa: BLE001
            log.warning("hazard %s failed (%s); unknown", kind, e)
            continue
        ext = resolve_coverage(
            entry, admin_boundary,
            dataset_root=root,
            hazard_polygons=layer.inundation_gdf,
        )
        if ext is not None:
            extents.append(ext)
        else:
            log.warning("no coverage extent for %s — uncovered", kind)

    gdf = apply_coverage(gdf, extents)
    gdf = apply_hazards(gdf, layers)
    gdf.to_parquet(parquet_path, index=False)

    fc = field_coverage(gdf, ATTRIBUTE_FIELDS_FOR_COVERAGE)
    manifest = build_manifest(gdf=gdf, catalog=catalog, field_coverage=fc, notes=notes)
    manifest_path = out_dir / "manifest.json"
    write_manifest(manifest, manifest_path)

    return HazardOnlyResult(
        buildings_parquet=parquet_path,
        manifest_path=manifest_path,
        n_buildings=len(gdf),
    )

"""Gate A: buildings.parquet + manifest + hazard intersection + coverage extent.

Inputs (read via the sources layer):
- Building CityGML
- 5 hazard CityGML themes
- Coverage extent polygons (per-hazard; explicit or declared_full_admin)
- City admin boundary (optional; needed only for declared_full_admin fallback)

Output:
- ``buildings.parquet`` — GeoParquet, see ``schema.BUILDINGS_ARROW_SCHEMA``.
- ``manifest.json``
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd

from plateau_bridge.admin import ADMIN_PROVENANCE, load_admin
from plateau_bridge.catalog import CityCatalog
from plateau_bridge.config import load_settings
from plateau_bridge.manifest import build_manifest, write_manifest
from plateau_bridge.ops.attributes import field_coverage, normalise
from plateau_bridge.ops.intersect import apply_coverage, apply_hazards
from plateau_bridge.ops.uid import batch_uids
from plateau_bridge.schema import ATTRIBUTION
from plateau_bridge.sources.citygml import convert_buildings, load_geojson
from plateau_bridge.sources.coverage import resolve_coverage
from plateau_bridge.sources.download import fetch_and_unzip
from plateau_bridge.sources.hazard import load_hazard

log = logging.getLogger(__name__)

ATTRIBUTE_FIELDS_FOR_COVERAGE = [
    "year_built", "structure", "usage", "height",
    "floors_above", "floors_below", "fire_resistance",
]


@dataclass(frozen=True)
class GateAResult:
    buildings_parquet: Path
    manifest_path: Path
    tiles3d_dir: Path | None
    gdf: gpd.GeoDataFrame  # in-memory copy for downstream gates


def run_gate_a(
    catalog: CityCatalog,
    out_dir: Path,
    *,
    admin_boundary: gpd.GeoDataFrame | None = None,
    emit_3dtiles: bool = True,
    clip_to_admin: bool = True,
    skip_hazards: bool = False,
) -> GateAResult:
    settings = load_settings()
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "_work"
    work.mkdir(exist_ok=True)
    notes: list[str] = []

    # Resolve admin boundary: explicit arg > bundled lookup > None.
    admin_source = "argument"
    if admin_boundary is None:
        admin_boundary = load_admin(catalog.city_code)
        admin_source = "bundled" if admin_boundary is not None else "none"
    if admin_boundary is not None:
        notes.append(f"admin_boundary source: {admin_source} ({ADMIN_PROVENANCE})")
        log.info("admin boundary resolved (%s) for city %s", admin_source, catalog.city_code)

    # 1. Building CityGML → GeoDataFrame.
    bldg_entry = catalog.building()
    bldg_root = fetch_and_unzip(bldg_entry.url, settings.cache_dir / "datasets")
    bldg_src = bldg_root / bldg_entry.udx_subdir if bldg_entry.udx_subdir else bldg_root
    bldg_out = work / "bldg"
    conv = convert_buildings(
        bldg_src, bldg_out,
        converter_bin=settings.converter_bin,
        emit_3dtiles=emit_3dtiles,
    )
    gdf = load_geojson(conv.geojson_path)
    log.info("loaded %d buildings", len(gdf))

    # 1b. Clip to admin boundary if available. PLATEAU ships prefecture-wide
    # bundles (``_pref_``) so e.g. the 渋谷区 zip contains ~90k buildings
    # spanning multiple wards. Clipping keeps the parquet honest to its
    # city_code label and drops ~50% of irrelevant rows.
    if admin_boundary is not None and clip_to_admin:
        before = len(gdf)
        admin_union = admin_boundary.geometry.union_all()
        gdf = gdf[gdf.geometry.intersects(admin_union)].copy().reset_index(drop=True)
        notes.append(
            f"clipped to admin boundary: {before:,} → {len(gdf):,} buildings"
        )
        log.info("clipped to admin: %d → %d", before, len(gdf))

    # 2. Normalise attributes.
    gdf = normalise(gdf)

    # 3. Building UIDs + centroid.
    centroids = gdf.geometry.representative_point()
    gdf["centroid_lon"] = centroids.x
    gdf["centroid_lat"] = centroids.y
    gdf["city_code"] = catalog.city_code
    gdf["dataset_year"] = catalog.dataset_year
    # ``load_geojson`` restores gml_id from the GeoJSON Feature.id; defend
    # against converter-version drift by falling back to a stable synthetic id.
    if "gml_id" not in gdf.columns or gdf["gml_id"].isna().all():
        gdf["gml_id"] = [f"synth_{i}" for i in range(len(gdf))]
        log.warning("no Feature.id from converter; synthesised gml_id (uid stability degraded)")
    gdf["source_file_id"] = bldg_entry.dataset_id
    gdf["building_uid"] = batch_uids(
        city_code=catalog.city_code,
        dataset_year=catalog.dataset_year,
        source_file_ids=gdf["source_file_id"],
        gml_ids=gdf["gml_id"].astype(str),
    )
    gdf["source_url"] = bldg_entry.url
    gdf["source_dataset_id"] = bldg_entry.dataset_id
    gdf["attribution"] = ATTRIBUTION

    # 4. Hazard coverage + intersection.
    hazards = {} if skip_hazards else catalog.hazards()
    if skip_hazards:
        log.info("skip_hazards=True; all hazard columns will be NULL/unknown")
        notes.append("hazards skipped (--no-hazards); all hazard fields are 'unknown'")
    extents = []
    layers = []
    # Optional bbox for hazard-layer load. Pre-filtering the (potentially
    # multi-GB) flood polygons via GDAL bbox at read time saves minutes on
    # Osaka.
    hazard_bbox = (
        tuple(admin_boundary.total_bounds.tolist()) if admin_boundary is not None else None
    )
    for kind, entry in hazards.items():
        root = fetch_and_unzip(entry.url, settings.cache_dir / "datasets")
        src = root / entry.udx_subdir if entry.udx_subdir else root
        if not src.exists():
            log.warning(
                "hazard %s subdir %s missing from bundle — leaving as unknown",
                kind, entry.udx_subdir,
            )
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
            log.warning("hazard %s conversion failed (%s); leaving as unknown", kind, e)
            continue
        ext = resolve_coverage(
            entry, admin_boundary,
            dataset_root=root,
            hazard_polygons=layer.inundation_gdf,
        )
        if ext is not None:
            extents.append(ext)
        else:
            log.warning("no coverage extent for %s — buildings will be marked uncovered", kind)

    gdf = apply_coverage(gdf, extents)
    gdf = apply_hazards(gdf, layers)

    # Pre-populate Gate B / Gate C columns as nulls so the parquet schema is
    # stable from Gate A onward. Downstream gates overwrite these in place.
    for col in ("zoning_use", "far_max", "tile_content_uri", "tile_feature_id"):
        if col not in gdf.columns:
            gdf[col] = None

    # 5. Write parquet (GeoParquet).
    parquet_path = out_dir / "buildings.parquet"
    gdf.to_parquet(parquet_path, index=False)

    # 6. Manifest.
    fc = field_coverage(gdf, ATTRIBUTE_FIELDS_FOR_COVERAGE)
    manifest = build_manifest(gdf=gdf, catalog=catalog, field_coverage=fc, notes=notes)
    manifest_path = out_dir / "manifest.json"
    write_manifest(manifest, manifest_path)

    return GateAResult(
        buildings_parquet=parquet_path,
        manifest_path=manifest_path,
        tiles3d_dir=conv.tiles3d_dir,
        gdf=gdf,
    )

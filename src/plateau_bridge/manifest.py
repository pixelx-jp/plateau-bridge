"""manifest.json writer.

Every output bundle ships a manifest documenting datasets used, source years,
coverage stats per hazard, attribute field coverage, and the tool version. This
is what lets downstream apps (poster renderers, 2D risk-map viewers, etc.) say honest things about
their data — e.g. "63% of buildings in Shibuya have a year_built attribute".
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import geopandas as gpd

from plateau_bridge import __version__
from plateau_bridge.catalog import CityCatalog
from plateau_bridge.schema import (
    DEPTH_HAZARDS,
    CoverageConfidence,
    CoverageStats,
    HazardKind,
    Manifest,
    SourceRef,
)


def compute_coverage_stats(gdf: gpd.GeoDataFrame) -> list[CoverageStats]:
    out: list[CoverageStats] = []
    for kind in HazardKind:
        cov_col = f"{kind.value}_covered"
        conf_col = f"{kind.value}_coverage_confidence"
        hit_col = (
            f"{kind.value}_depth_max" if kind in DEPTH_HAZARDS else f"{kind.value}_in_zone"
        )
        if cov_col not in gdf.columns:
            continue
        covered = int(gdf[cov_col].fillna(False).sum())
        if kind in DEPTH_HAZARDS:
            hit = int((gdf[hit_col].fillna(0) > 0).sum())
        else:
            hit = int(gdf[hit_col].fillna(False).sum())
        breakdown_raw = Counter(gdf[conf_col].fillna("unknown").tolist())
        breakdown: dict[CoverageConfidence, int] = {}
        for k_str, n in breakdown_raw.items():
            try:
                breakdown[CoverageConfidence(k_str)] = int(n)
            except ValueError:
                continue
        out.append(
            CoverageStats(
                kind=kind,
                covered_count=covered,
                hit_count=hit,
                coverage_confidence_breakdown=breakdown,
            )
        )
    return out


def build_manifest(
    *,
    gdf: gpd.GeoDataFrame,
    catalog: CityCatalog,
    field_coverage: dict[str, float],
    notes: list[str] | None = None,
) -> Manifest:
    sources: dict[str, SourceRef] = {}
    for e in catalog.entries:
        sources[e.dataset_id] = SourceRef(
            source_id=e.dataset_id,
            dataset_id=e.dataset_id,
            year=e.year,
            url=e.url,
            coverage_extent_url=e.coverage_extent_url,
        )
    return Manifest(
        tool_version=__version__,
        generated_at=datetime.now(tz=UTC),
        city_code=catalog.city_code,
        city_name=catalog.city_name,
        dataset_year=catalog.dataset_year,
        n_buildings=len(gdf),
        datasets=[e.dataset_id for e in catalog.entries],
        sources=sources,
        coverage_stats=compute_coverage_stats(gdf),
        field_coverage=field_coverage,
        notes=notes or [],
    )


def write_manifest(manifest: Manifest, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

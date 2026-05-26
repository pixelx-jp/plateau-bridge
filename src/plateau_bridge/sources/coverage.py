"""Resolve hazard coverage extent.

This module enforces the **never-reverse-engineer-from-inundation-polygons**
rule. The plan calls this out as the single most important data-integrity
invariant of the pipeline; this is where it lives.

Order of preference (per plan §coverage):

1. **explicit_polygon** — download the source's published "想定区域 / 調査範囲"
   polygon (linked from ``DatasetEntry.coverage_extent_url``).
2. **declared_full_admin** — only when the source metadata declares full-admin
   coverage. Intersect the city admin boundary.
3. **unknown** — return ``None``. Downstream must set ``covered = False`` and
   ``coverage_confidence = "unknown"``.

A None return is **not** a fallback to inundation-union; it is the absence of
trustworthy coverage data and must surface to the UI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd

from plateau_bridge.catalog import DatasetEntry
from plateau_bridge.schema import CoverageConfidence, HazardKind

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CoverageExtent:
    kind: HazardKind
    source_id: str
    geometry: gpd.GeoDataFrame  # single-row GeoDataFrame in EPSG:4326
    confidence: CoverageConfidence


def _load_polygon(path_or_url: str) -> gpd.GeoDataFrame:
    """Read a polygon from a file path or URL (pyogrio/fiona handle both)."""
    gdf = gpd.read_file(path_or_url)
    if gdf.crs and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)
    return gdf


def resolve_coverage(
    entry: DatasetEntry,
    admin_boundary: gpd.GeoDataFrame | None,
    *,
    extent_cache_path: Path | None = None,
    dataset_root: Path | None = None,
    ksj_cache_dir: Path | None = None,
    hazard_polygons: gpd.GeoDataFrame | None = None,
    hazard_coverage_threshold: float = 0.95,
) -> CoverageExtent | None:
    """Resolve the coverage extent for a single hazard dataset.

    Resolve order (most → least trustworthy):

    1. ``entry.coverage_extent_url`` — explicit polygon URL on the catalog
       entry itself. ``explicit_polygon`` confidence.
    2. ``coverage_sources.json`` mapping — match PLATEAU bundle's
       ``metadata/*_op.xml`` source-documents against MLIT KSJ URLs in
       the bundled mapping table. ``explicit_polygon`` confidence, but
       gated by a sanity check vs ``hazard_polygons`` if provided —
       KSJ candidates that don't contain ≥ 95% of PLATEAU's hazard
       polygon area are rejected (they'd mask depth data we have).
    3. ``hazard_polygons`` themselves — when PLATEAU bundles per-building
       depth data, use the union of those polygons AS-IS as the extent.
       ``inundation_bounded`` confidence. The literal truth of the data:
       inside = modelled with depth, outside = not modelled.
    4. ``entry.declared_full_admin`` — source claims full-admin
       coverage; intersect with admin polygon. ``declared_full_admin``
       confidence. Weaker than (3) because it overclaims a
       "modelled-and-safe" category that doesn't actually exist in
       most cases.
    5. None — caller must surface ``coverage_confidence: unknown`` and
       ``covered = false``. **Never** synthesise an extent by
       buffering / dilating inundation polygons — that's the
       reverse-engineering HONESTY.md forbids.

    Args:
        entry: hazard ``DatasetEntry`` from the catalog.
        admin_boundary: the city's admin polygon in EPSG:4326. Required when
            falling back to ``declared_full_admin``; otherwise may be ``None``.
        extent_cache_path: where to read a cached step-1 extent polygon. Optional.
        dataset_root: path to the unzipped PLATEAU bundle for this hazard
            theme. Required to enable step 2 (KSJ auto-resolve); pipeline
            already has it from the preceding ``fetch_and_unzip`` call.
        ksj_cache_dir: directory for the KSJ download cache. Defaults to
            ``$PLATEAU_CACHE_DIR/ksj`` when omitted.
        hazard_polygons: the PLATEAU inundation/risk polygons for this hazard
            (typically ``HazardLayer.inundation_gdf``). Used as a *sanity
            check* on KSJ-derived extents — if the KSJ polygon covers
            < ``hazard_coverage_threshold`` of the PLATEAU polygons' area,
            we conclude KSJ is incomplete (e.g. PLATEAU ingested municipal
            urban-flood maps not in KSJ A31) and demote to
            ``declared_full_admin`` rather than hide depth data we have.
            See docs/COVERAGE_ROADMAP.md for the data-integrity rationale.
        hazard_coverage_threshold: fraction of PLATEAU hazard-polygon area
            that the KSJ-derived extent must contain. Default 0.95.

    Returns:
        A ``CoverageExtent`` or ``None`` if no trustworthy extent is available.
    """
    assert entry.theme == "hazard" and entry.hazard_kind is not None

    # 1. Explicit polygon from a catalog-pinned URL.
    if entry.coverage_extent_url:
        src = extent_cache_path if extent_cache_path and extent_cache_path.exists() else entry.coverage_extent_url
        try:
            gdf = _load_polygon(str(src))
            merged = gdf.dissolve().reset_index(drop=True)
            return CoverageExtent(
                kind=entry.hazard_kind,
                source_id=entry.dataset_id,
                geometry=merged,
                confidence=CoverageConfidence.EXPLICIT_POLYGON,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("failed to load coverage extent %s: %s", src, e)

    # 1b. KSJ auto-resolve via metadata XML + bundled mapping table.
    if dataset_root is not None:
        from plateau_bridge.config import load_settings
        from plateau_bridge.sources.coverage_ksj import (
            resolve_explicit_polygon_from_metadata,
        )
        cache = ksj_cache_dir or (load_settings().cache_dir / "ksj")
        cache.mkdir(parents=True, exist_ok=True)
        result = resolve_explicit_polygon_from_metadata(entry, dataset_root, cache)
        if result is not None:
            # Sanity check: KSJ extent must contain the PLATEAU hazard polygons.
            # If KSJ is materially smaller than what PLATEAU actually models —
            # the Tokyo-Metro 流域浸水予想区域図 case described in
            # docs/COVERAGE_ROADMAP.md — we'd be hiding depth data we have.
            # Better to fall through to declared_full_admin.
            if hazard_polygons is not None and not hazard_polygons.empty:
                ksj_poly = result.polygon.geometry.union_all()
                hazard_union = hazard_polygons.geometry.union_all()
                if hazard_union.is_empty:
                    log.info(
                        "KSJ sanity check skipped: hazard polygons union is empty for %s",
                        entry.dataset_id,
                    )
                else:
                    contained_area = ksj_poly.intersection(hazard_union).area
                    total_area = hazard_union.area
                    coverage = contained_area / total_area if total_area else 0.0
                    if coverage < hazard_coverage_threshold:
                        log.warning(
                            "KSJ extent for %s covers only %.1f%% of PLATEAU hazard "
                            "polygons (< %.0f%% threshold); falling back to "
                            "declared_full_admin to avoid masking depth data. "
                            "See docs/COVERAGE_ROADMAP.md.",
                            entry.dataset_id,
                            coverage * 100,
                            hazard_coverage_threshold * 100,
                        )
                        # do NOT return — fall through to declared_full_admin
                        result = None
            if result is not None:
                return result.as_coverage_extent(entry)

    # 1c. INUNDATION_BOUNDED — when PLATEAU's bundled hazard polygons are
    # available, use them AS-IS as the extent. The literal truth of the data:
    # inside polygon = modelled with depth, outside = not modelled.
    #
    # This is NOT "reverse-engineering" in the HONESTY.md sense — that rule
    # forbids BUFFERING / DILATING flood polygons to fabricate a survey
    # extent (e.g. "within 100m of a flood is surveyed-safe"). Using the
    # raw polygon at its native boundary makes no synthetic claim — the
    # boundary IS where MLIT's model stops.
    #
    # Strictly more honest than declared_full_admin, which fabricates a
    # "modelled-and-safe" category for buildings that simply sit outside
    # the modelling area (uphill, far from rivers). Those should be
    # "unknown", not "covered=true, depth=0".
    if hazard_polygons is not None and not hazard_polygons.empty:
        try:
            merged = hazard_polygons.dissolve().reset_index(drop=True)
            if not merged.empty and not merged.geometry.iloc[0].is_empty:
                return CoverageExtent(
                    kind=entry.hazard_kind,
                    source_id=f"{entry.dataset_id}+inundation_bounded",
                    geometry=merged,
                    confidence=CoverageConfidence.INUNDATION_BOUNDED,
                )
        except Exception as e:  # noqa: BLE001
            log.warning("inundation-bounded extent failed for %s: %s", entry.dataset_id, e)

    # 2. Declared full-admin.
    if entry.declared_full_admin and admin_boundary is not None and not admin_boundary.empty:
        merged = admin_boundary.dissolve().reset_index(drop=True)
        return CoverageExtent(
            kind=entry.hazard_kind,
            source_id=entry.dataset_id,
            geometry=merged,
            confidence=CoverageConfidence.DECLARED_FULL_ADMIN,
        )

    # 3. Unknown — explicit None, never inundation-union.
    log.info("no trustworthy coverage extent for %s; leaving as unknown", entry.dataset_id)
    return None

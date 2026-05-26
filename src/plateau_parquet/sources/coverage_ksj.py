"""Resolve `explicit_polygon` coverage by joining PLATEAU bundle metadata
against MLIT KSJ (国土数値情報) published 想定区域 polygons.

The full rationale lives in ``docs/COVERAGE_ROADMAP.md``. Short version:

PLATEAU bundles list — in free-text Japanese under
``<descriptiveKeywords type=005>`` — the source documents underpinning
each hazard layer. Example::

    利根川水系利根川洪水浸水想定区域図（平成29年7月20日）国土交通省関東地方整備局…

MLIT publishes the precise polygons separately under the KSJ catalog
(A31 for river flood, A40 for tsunami, A48 for landslide, A41 for
storm surge). This module:

1. Reads the PLATEAU metadata XML to extract source-document names.
2. Looks each up in ``data/coverage_sources.json``.
3. Downloads + unions the matching KSJ polygons (content-addressed cache).
4. Returns a ``CoverageExtent`` of confidence ``EXPLICIT_POLYGON``.

When the mapping table is empty, this module is effectively a no-op —
the resolver chain falls through to ``declared_full_admin``. Adding
a single row in the JSON upgrades any matching city without any code
change. That's the design point.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely

from plateau_parquet.catalog import DatasetEntry
from plateau_parquet.schema import CoverageConfidence, HazardKind
from plateau_parquet.sources.download import fetch_and_unzip
from plateau_parquet.sources.metadata_xml import (
    canonicalise_source_document,
    find_metadata_files,
    parse_metadata_xml,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class KsjMapping:
    """One row of ``coverage_sources.json``.

    ``ksj_urls`` is a list because PLATEAU's source-document names often
    span multiple KSJ files — e.g. "荒川水系神田川、善福寺川、妙正寺川
    洪水浸水想定区域図" includes rivers managed by both the prefecture
    (A31 file 13) and the regional bureau (A31 file 83). The resolver
    fetches each URL once (content-addressed cache) and unions the
    polygons. Listing the same URL across many entries is therefore
    free — overhead is per-unique-URL, not per-entry.
    """
    hazard: HazardKind
    ksj_urls: tuple[str, ...]
    published: str
    notes: str = ""


@lru_cache(maxsize=1)
def load_coverage_sources() -> dict[str, KsjMapping]:
    """Load the bundled mapping JSON, returning a dict keyed by canonical
    source-document name. Empty dict if the file is malformed or has no
    entries — callers fall through to ``declared_full_admin`` cleanly.
    """
    try:
        raw = files("plateau_parquet").joinpath("data", "coverage_sources.json").read_text(
            encoding="utf-8"
        )
    except (FileNotFoundError, ModuleNotFoundError):
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("coverage_sources.json malformed: %s; treating as empty", e)
        return {}
    out: dict[str, KsjMapping] = {}
    for key, val in data.get("entries", {}).items():
        try:
            kind = HazardKind(val["hazard"])
        except (KeyError, ValueError) as e:
            log.warning(
                "skipping coverage_sources.json entry %r: invalid hazard (%s)", key, e
            )
            continue
        # Accept either `ksj_url` (str) or `ksj_urls` (list of str) — keeps
        # one-river-one-URL entries terse, multi-bureau entries explicit.
        urls = val.get("ksj_urls") or ([val["ksj_url"]] if val.get("ksj_url") else [])
        if not urls:
            log.warning("skipping coverage_sources.json entry %r: no ksj_url(s)", key)
            continue
        out[key] = KsjMapping(
            hazard=kind,
            ksj_urls=tuple(urls),
            published=val.get("published", ""),
            notes=val.get("notes", ""),
        )
    return out


def _fetch_ksj_polygon(url: str, cache_dir: Path) -> gpd.GeoDataFrame | None:
    """Download + extract a KSJ zip, then read the first polygon-bearing
    file inside. Content-addressed cache identical to PLATEAU downloads.

    KSJ ships Shapefile-in-zip mostly (A31 / A40 / A48 / A41). geopandas
    via pyogrio reads .shp directly. Returns ``None`` on any failure so
    callers fall through cleanly rather than crashing the build.
    """
    try:
        root = fetch_and_unzip(url, cache_dir)
    except Exception as e:  # noqa: BLE001
        log.warning("KSJ download failed for %s: %s", url, e)
        return None
    # KSJ zip layout: one or more .shp files at the root or one level down.
    shp_candidates = list(root.rglob("*.shp"))
    if not shp_candidates:
        # Fallback: try GML for the few datasets that ship that way.
        shp_candidates = list(root.rglob("*.gml"))
    if not shp_candidates:
        log.warning("no .shp/.gml found inside KSJ extract at %s", root)
        return None
    # Concat all polygon-bearing layers — a single KSJ dataset can ship
    # multiple subdivisions (e.g. per-river segments). The union happens
    # downstream in `resolve_explicit_polygon_from_metadata`.
    gdfs: list[gpd.GeoDataFrame] = []
    for shp in shp_candidates:
        try:
            g = gpd.read_file(shp)
        except Exception as e:  # noqa: BLE001
            log.warning("failed to read KSJ layer %s: %s", shp, e)
            continue
        if g.empty:
            continue
        if g.crs and g.crs.to_epsg() != 4326:
            g = g.to_crs(4326)
        gdfs.append(g[["geometry"]])
    if not gdfs:
        return None
    merged = gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True), crs=4326)
    return merged


def resolve_explicit_polygon_from_metadata(
    entry: DatasetEntry,
    dataset_root: Path,
    ksj_cache_dir: Path,
):
    """Try to assemble an explicit-polygon coverage extent by joining
    PLATEAU bundle metadata against the bundled KSJ mapping table.

    Returns ``None`` on any of:
    - hazard kind missing on the entry,
    - no metadata XML for this hazard in the bundle,
    - no source documents match any row in the mapping JSON,
    - no KSJ polygon successfully downloaded + parsed.

    A ``None`` here is *correct fall-through* — the caller in
    ``resolve_coverage`` then drops to ``declared_full_admin``. We do NOT
    treat a None as failure; it's "this city + hazard isn't yet covered
    by the mapping table."

    On success: returns a single-row ``GeoDataFrame`` in EPSG:4326 plus
    the list of source-document strings matched (for attribution).
    """
    if entry.hazard_kind is None:
        return None
    mapping = load_coverage_sources()
    if not mapping:
        return None  # community-curated table is empty; fall through

    # 1. Walk the bundle's metadata XML files. We don't pre-filter by
    # theme because the file naming convention isn't 100% stable across
    # PLATEAU versions; cheap to read all and filter by content.
    matched_polygons: list[gpd.GeoDataFrame] = []
    matched_sources: list[str] = []
    for meta_path in find_metadata_files(dataset_root):
        extract = parse_metadata_xml(meta_path)
        if extract is None:
            continue
        for raw_source in extract.source_documents:
            canonical = canonicalise_source_document(raw_source)
            mapped = mapping.get(canonical)
            if mapped is None:
                continue
            if mapped.hazard != entry.hazard_kind:
                continue
            for url in mapped.ksj_urls:
                poly = _fetch_ksj_polygon(url, ksj_cache_dir)
                if poly is None or poly.empty:
                    continue
                matched_polygons.append(poly)
            matched_sources.append(canonical)

    if not matched_polygons:
        return None

    # 2. Union every matched polygon into a single-row GeoDataFrame.
    # Multiple watersheds per city is the common case in Tokyo.
    union = shapely.unary_union([g.geometry.union_all() for g in matched_polygons])
    out = gpd.GeoDataFrame({"geometry": [union]}, crs=4326)
    return _ExplicitPolygonResult(
        polygon=out,
        source_documents=tuple(matched_sources),
    )


@dataclass(frozen=True)
class _ExplicitPolygonResult:
    polygon: gpd.GeoDataFrame
    source_documents: tuple[str, ...]

    def as_coverage_extent(self, entry: DatasetEntry):
        """Wrap as a ``CoverageExtent`` matching ``sources.coverage``'s shape."""
        from plateau_parquet.sources.coverage import CoverageExtent  # avoid cycle
        return CoverageExtent(
            kind=entry.hazard_kind,
            source_id=f"{entry.dataset_id}+ksj:{','.join(self.source_documents)[:80]}",
            geometry=self.polygon,
            confidence=CoverageConfidence.EXPLICIT_POLYGON,
        )


_ = HazardKind  # keep import used

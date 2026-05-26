"""City administrative boundary loader.

PLATEAU hazard datasets often carry ``declared_full_admin: true`` — the source
says "covers the entire administrative area" — without shipping the admin
polygon. ``plateau-bridge`` ships its own polygons for the bundled cities so
the ``declared_full_admin`` fallback in ``sources.coverage`` has data to work
with out of the box. Users can override with ``plateau build --admin <path>``.

Bundled sources:
- Tokyo 23 wards + Tama-area municipalities — ``dataofjapan/land`` (MIT).
- Osaka-shi (single dissolved polygon) — MLIT 国土数値情報 N03 (`27` 2024).

Provenance is recorded in ``manifest.notes`` per build.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

import geopandas as gpd
import shapely

log = logging.getLogger(__name__)


def _coerce_to_polygonal(geom):
    """Make ``geom`` polygon-typed and topologically valid.

    Several PLATEAU / dataofjapan admin polygons in the bundled file have
    minor topology defects (free holes, self-touches at vertex precision).
    Most pass quietly through ``union_all``; Edogawa-ku does not — it
    raises ``TopologyException: unable to assign free hole to a shell``
    and aborts Gate A.

    ``shapely.make_valid`` fixes the topology losslessly (area change
    ≤ 0.7 % across our entire bundled file, 0 % for clean inputs). The
    catch is it can return a ``GeometryCollection`` that bundles tiny
    line/point artefacts alongside the repaired polygon — those break
    later spatial joins. We keep only the polygonal parts.

    Idempotent. No-ops on already-valid input.
    """
    if geom is None or geom.is_empty:
        return geom
    if geom.is_valid:
        return geom
    fixed = shapely.make_valid(geom)
    if fixed.geom_type in {"Polygon", "MultiPolygon"}:
        return fixed
    if fixed.geom_type == "GeometryCollection":
        polys = [g for g in fixed.geoms if g.geom_type in {"Polygon", "MultiPolygon"}]
        if not polys:
            return geom  # nothing recoverable — leave the original
        if len(polys) == 1:
            return polys[0]
        return shapely.union_all(polys)
    return geom

ADMIN_PROVENANCE = (
    "Tokyo: © dataofjapan/land (MIT); "
    "Osaka & others: MLIT 国土数値情報 N03 行政区域 (2024)"
)


@lru_cache(maxsize=1)
def _bundled_admin() -> gpd.GeoDataFrame:
    """Load and cache the bundled admin-polygon GeoDataFrame."""
    with (files("plateau_bridge") / "data" / "japan_admin.geojson").open(
        encoding="utf-8"
    ) as f:
        gdf = gpd.read_file(f)
    if gdf.crs is None:
        gdf = gdf.set_crs(4326)
    n_invalid = (~gdf.is_valid).sum()
    if n_invalid:
        log.info("repairing %d invalid admin polygons via make_valid", n_invalid)
        gdf.geometry = gdf.geometry.apply(_coerce_to_polygonal)
    return gdf


def load_admin(city_code: str) -> gpd.GeoDataFrame | None:
    """Return the admin polygon for a city, or None if unavailable.

    Looks first in the bundled Tokyo data. Returns a single-row GeoDataFrame
    in EPSG:4326. None means "we don't have it" — pipeline must fall back to
    ``coverage_confidence: unknown``.
    """
    bundled = _bundled_admin()
    if "city_code" not in bundled.columns:
        return None
    match = bundled[bundled["city_code"].astype(str) == str(city_code)]
    # Exclude ward-level rows (those have parent_city_code populated)
    if "parent_city_code" in match.columns:
        match = match[match["parent_city_code"].fillna("") == ""]
    if match.empty:
        log.info("no bundled admin polygon for city_code=%s", city_code)
        return None
    return match.reset_index(drop=True)


def load_wards(parent_city_code: str) -> gpd.GeoDataFrame | None:
    """Return per-ward sub-polygons for 政令指定都市, or None for single-ward cities.

    Used by Gate C to emit ``buildings/{city}_{ward}.fgb`` shards. For
    Tokyo special wards (each "city" already is one ward) this returns
    None and Gate C falls back to single-file output.
    """
    bundled = _bundled_admin()
    if "parent_city_code" not in bundled.columns:
        return None
    match = bundled[bundled["parent_city_code"].astype(str) == str(parent_city_code)]
    if match.empty:
        return None
    return match.reset_index(drop=True)


def load_admin_from_path(path: Path) -> gpd.GeoDataFrame:
    """User-supplied admin polygon. Reprojects to EPSG:4326."""
    gdf = gpd.read_file(path)
    if gdf.crs is None:
        log.warning("admin polygon %s has no CRS; assuming EPSG:4326", path)
        gdf = gdf.set_crs(4326)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)
    if not gdf.is_valid.all():
        gdf.geometry = gdf.geometry.apply(_coerce_to_polygonal)
    return gdf

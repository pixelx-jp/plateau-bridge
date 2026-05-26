"""Building × hazard spatial intersection.

Two passes per hazard kind:

1. **Coverage join** — for every building, did at least one coverage extent
   polygon include it? This produces ``*_covered`` + ``*_coverage_source_ids``.
2. **Hit join** — for every covered building, intersect against the inundation
   polygons. Multi-source max for depth; OR for landslide ``in_zone``.

Doing it in two passes is what makes the "covered but not hit" case (i.e. the
survey looked and found no risk) distinguishable from "no survey" — the single
most important honesty invariant of this pipeline.

**Centroid mode**: PLATEAU's prefecture-wide tsunami / storm_surge layers ship
as single mega-polygons with hundreds of thousands of vertices. Polygon-vs-
polygon ``intersects`` against them is genuinely O(N · M) and pathologically
slow (Osaka 615 k × ~500 k-vertex tsunami polygon ≈ 7+ hours and counting).

The right tradeoff is to use **building centroids vs hazard polygon
`within`**: O(N · log M) via STRtree, sub-minute on Osaka. The precision loss
is bounded by half a building footprint width (~5–10 m) against hazard
polygons that span kilometres — well below PLATEAU's positional accuracy.
``centroid_mode=True`` is the default for this reason; pass False for legacy
edge-precise behaviour.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import geopandas as gpd
import numpy as np

from plateau_bridge.schema import DEPTH_HAZARDS, HazardKind
from plateau_bridge.sources.coverage import CoverageExtent
from plateau_bridge.sources.hazard import HazardLayer

log = logging.getLogger(__name__)


def _ids_join(values: Iterable[str]) -> str:
    """Compact comma-separated source-id list, sorted, deduped."""
    return ",".join(sorted({v for v in values if v}))


def _building_points(buildings: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Return a (building_index → representative_point) GeoDataFrame.

    ``representative_point`` is guaranteed inside the polygon and deterministic
    — unlike ``centroid`` which can fall outside concave footprints.
    """
    pts = buildings[["geometry"]].copy()
    pts["geometry"] = buildings.geometry.representative_point()
    return pts


def apply_coverage(
    buildings: gpd.GeoDataFrame,
    extents: Iterable[CoverageExtent],
    *,
    centroid_mode: bool = True,
) -> gpd.GeoDataFrame:
    """Add ``{kind}_covered``, ``{kind}_coverage_source_ids``, ``{kind}_coverage_confidence``."""
    out = buildings.copy()
    # Initialise all kinds defensively.
    for kind in HazardKind:
        out[f"{kind.value}_covered"] = False
        out[f"{kind.value}_coverage_source_ids"] = ""
        out[f"{kind.value}_coverage_confidence"] = "unknown"

    # Build the left frame + reset_index ONCE — geopandas's reset_index
    # constructs a fresh DataFrame each call, so doing it inside the per-
    # extent loop wastes ~ms × N_extents for big cities.
    left = (_building_points(out) if centroid_mode else out[["geometry"]]).reset_index()
    predicate = "within" if centroid_mode else "intersects"

    for ext in extents:
        hits = gpd.sjoin(
            left,
            ext.geometry[["geometry"]],
            how="inner",
            predicate=predicate,
        )["index"].unique()
        col_cov = f"{ext.kind.value}_covered"
        col_src = f"{ext.kind.value}_coverage_source_ids"
        col_conf = f"{ext.kind.value}_coverage_confidence"
        out.loc[hits, col_cov] = True
        prev = out.loc[hits, col_src]
        out.loc[hits, col_src] = [
            _ids_join([p, ext.source_id]) for p in prev
        ]
        order = {
            "unknown": 0,
            "declared_full_admin": 1,
            "inundation_bounded": 2,
            "explicit_polygon": 3,
        }
        prev_conf = out.loc[hits, col_conf]
        new_conf = ext.confidence.value if hasattr(ext.confidence, "value") else str(ext.confidence)
        out.loc[hits, col_conf] = [
            new_conf if order[new_conf] >= order[c] else c for c in prev_conf
        ]
    return out


def apply_hazards(
    buildings: gpd.GeoDataFrame,
    layers: Iterable[HazardLayer],
    *,
    centroid_mode: bool = True,
) -> gpd.GeoDataFrame:
    """Add ``{kind}_depth_max`` / ``{kind}_in_zone`` + ``{kind}_hit_source_ids``.

    A building only receives a hit value when ``{kind}_covered`` is True;
    intersections outside coverage are dropped to keep the honesty invariant.
    """
    out = buildings.copy()
    for kind in DEPTH_HAZARDS:
        out[f"{kind.value}_depth_max"] = np.nan
        out[f"{kind.value}_hit_source_ids"] = ""
    out[f"{HazardKind.LANDSLIDE.value}_in_zone"] = False
    out[f"{HazardKind.LANDSLIDE.value}_hit_source_ids"] = ""

    # Per-kind left frame is rebuilt because eligible-rows depend on
    # *_covered (different per hazard kind). Still cheaper than the previous
    # double-reset, and the predicate / centroid choice is loop-invariant.
    predicate = "within" if centroid_mode else "intersects"
    for layer in layers:
        kind = layer.kind
        cov_col = f"{kind.value}_covered"
        eligible = out[out[cov_col]].copy()
        if eligible.empty:
            log.info("no covered buildings for %s; skipping hits", kind)
            continue
        left = (_building_points(eligible) if centroid_mode else eligible[["geometry"]]).reset_index()
        joined = gpd.sjoin(
            left,
            layer.inundation_gdf,
            how="inner",
            predicate=predicate,
        )
        if joined.empty:
            continue

        if kind == HazardKind.LANDSLIDE:
            hit_idx = joined["index"].unique()
            out.loc[hit_idx, f"{kind.value}_in_zone"] = True
            out.loc[hit_idx, f"{kind.value}_hit_source_ids"] = layer.source_id
        else:
            per_bldg = joined.groupby("index")["depth_m"].max()
            depth_col = f"{kind.value}_depth_max"
            hit_col = f"{kind.value}_hit_source_ids"
            existing = out.loc[per_bldg.index, depth_col]
            new = np.fmax(existing.to_numpy(), per_bldg.to_numpy())
            out.loc[per_bldg.index, depth_col] = new
            prev_src = out.loc[per_bldg.index, hit_col]
            out.loc[per_bldg.index, hit_col] = [
                _ids_join([p, layer.source_id]) for p in prev_src
            ]

    return out

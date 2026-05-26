"""Honesty invariant: covered=false ≠ depth=0.

These tests exercise the rule that lives in `sources/coverage.py` and
`ops/intersect.py`: a building outside any coverage extent must end up with
``covered = False`` and ``coverage_confidence = "unknown"``, regardless of
whether an inundation polygon happens to overlap it.
"""

from __future__ import annotations

import geopandas as gpd
from shapely.geometry import Polygon

from plateau_parquet.ops.intersect import apply_coverage, apply_hazards
from plateau_parquet.schema import CoverageConfidence, HazardKind
from plateau_parquet.sources.coverage import CoverageExtent
from plateau_parquet.sources.hazard import HazardLayer


def _square(x: float, y: float, size: float = 1.0) -> Polygon:
    return Polygon([(x, y), (x + size, y), (x + size, y + size), (x, y + size)])


def test_uncovered_building_is_unknown_even_when_intersects_inundation() -> None:
    # Two buildings: A inside coverage extent, B outside.
    bldgs = gpd.GeoDataFrame(
        {"id": ["A", "B"]},
        geometry=[_square(0, 0, 0.1), _square(10, 10, 0.1)],
        crs="EPSG:4326",
    )
    extent = gpd.GeoDataFrame(geometry=[_square(-1, -1, 5)], crs="EPSG:4326")
    cov = CoverageExtent(
        kind=HazardKind.RIVER_FLOOD,
        source_id="src1",
        geometry=extent,
        confidence=CoverageConfidence.EXPLICIT_POLYGON,
    )
    # An inundation polygon happens to overlap B (outside coverage extent).
    inund = gpd.GeoDataFrame(
        {"depth_m": [2.5, 2.5]},
        geometry=[_square(0, 0, 0.5), _square(10, 10, 0.5)],
        crs="EPSG:4326",
    )
    layer = HazardLayer(kind=HazardKind.RIVER_FLOOD, inundation_gdf=inund, source_id="src1")

    out = apply_coverage(bldgs, [cov])
    out = apply_hazards(out, [layer])

    a = out[out["id"] == "A"].iloc[0]
    b = out[out["id"] == "B"].iloc[0]

    # A: covered, hit.
    assert a["river_flood_covered"] is True or a["river_flood_covered"] == True  # noqa: E712
    assert a["river_flood_depth_max"] == 2.5
    assert a["river_flood_coverage_confidence"] == "explicit_polygon"

    # B: not covered, depth must stay NaN, NOT 0.0.
    assert not bool(b["river_flood_covered"])
    assert b["river_flood_coverage_confidence"] == "unknown"
    import math
    assert math.isnan(b["river_flood_depth_max"])


def test_multi_source_depth_takes_max() -> None:
    bldgs = gpd.GeoDataFrame({"id": ["X"]}, geometry=[_square(0, 0, 0.1)], crs="EPSG:4326")
    extent = gpd.GeoDataFrame(geometry=[_square(-1, -1, 5)], crs="EPSG:4326")
    covs = [
        CoverageExtent(HazardKind.RIVER_FLOOD, "src1", extent, CoverageConfidence.EXPLICIT_POLYGON),
        CoverageExtent(HazardKind.RIVER_FLOOD, "src2", extent, CoverageConfidence.DECLARED_FULL_ADMIN),
    ]
    layers = [
        HazardLayer(HazardKind.RIVER_FLOOD, gpd.GeoDataFrame({"depth_m": [1.5]}, geometry=[_square(0, 0, 0.5)], crs="EPSG:4326"), "src1"),
        HazardLayer(HazardKind.RIVER_FLOOD, gpd.GeoDataFrame({"depth_m": [3.0]}, geometry=[_square(0, 0, 0.5)], crs="EPSG:4326"), "src2"),
    ]
    out = apply_coverage(bldgs, covs)
    out = apply_hazards(out, layers)

    row = out.iloc[0]
    assert row["river_flood_depth_max"] == 3.0
    # Stronger confidence wins.
    assert row["river_flood_coverage_confidence"] == "explicit_polygon"
    # Both source ids retained, sorted.
    assert row["river_flood_coverage_source_ids"] == "src1,src2"

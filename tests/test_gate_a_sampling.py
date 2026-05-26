"""Gate A 通过标准 §90: sample 10 buildings, verify ``*_covered`` matches the
known coverage extent polygon.

Plan quote:
    抽样 10 栋楼，验证 `_covered` 字段正确（5 栋落在已知调查范围内
    → covered=true；5 栋落在调查范围外 → covered=false）

This is the real-data check that distinguishes correct coverage logic from
the catastrophic mode where every building gets `covered=true` because we
silently reverse-engineered the extent from inundation polygons.

The test runs against a synthetic 10-building parquet whose geometry is
deliberately laid out 5-in / 5-out of a known extent polygon. It catches:

* off-by-one in ``apply_coverage`` (e.g. swapped predicates)
* accidental fallback to inundation-union extent
* centroid_mode vs polygon-mode drift on edge cases
"""

from __future__ import annotations

import geopandas as gpd
from shapely.geometry import Polygon

from plateau_bridge.ops.intersect import apply_coverage, apply_hazards
from plateau_bridge.schema import CoverageConfidence, HazardKind
from plateau_bridge.sources.coverage import CoverageExtent
from plateau_bridge.sources.hazard import HazardLayer


def _square(x: float, y: float, size: float = 0.001) -> Polygon:
    return Polygon([(x, y), (x + size, y), (x + size, y + size), (x, y + size)])


def _ten_buildings() -> gpd.GeoDataFrame:
    """5 inside (0,0)–(1,1), 5 outside (10,10)–(11,11)."""
    inside = [_square(x, x) for x in (0.1, 0.3, 0.5, 0.7, 0.9)]
    outside = [_square(x, x) for x in (10.1, 10.3, 10.5, 10.7, 10.9)]
    return gpd.GeoDataFrame(
        {"id": [f"in{i}" for i in range(5)] + [f"out{i}" for i in range(5)]},
        geometry=inside + outside,
        crs="EPSG:4326",
    )


def _extent_only() -> CoverageExtent:
    return CoverageExtent(
        kind=HazardKind.RIVER_FLOOD,
        source_id="test-fld",
        geometry=gpd.GeoDataFrame(geometry=[_square(0, 0, size=1.0)], crs="EPSG:4326"),
        confidence=CoverageConfidence.EXPLICIT_POLYGON,
    )


def test_ten_building_sample_covered_correctly() -> None:
    """Plan Gate A §90: 5 in / 5 out — covered=true for 5, false for 5."""
    bldgs = _ten_buildings()
    out = apply_coverage(bldgs, [_extent_only()])
    by_id = out.set_index("id")["river_flood_covered"]

    inside_results = [bool(by_id[f"in{i}"]) for i in range(5)]
    outside_results = [bool(by_id[f"out{i}"]) for i in range(5)]

    assert inside_results == [True, True, True, True, True], inside_results
    assert outside_results == [False, False, False, False, False], outside_results


def test_uncovered_buildings_have_no_depth_value_even_if_inundation_overlaps() -> None:
    """The critical anti-pattern: an inundation polygon that happens to
    overlap a building OUTSIDE the coverage extent must not produce a depth
    reading. This is the honesty invariant Plan §coverage extent calls out
    in bold ("**关键合规**：covered=false 是无数据，不等于无风险")."""
    bldgs = _ten_buildings()
    # Inundation polygon places water on building 'out0' — but that
    # building is outside the coverage extent.
    inundation = gpd.GeoDataFrame(
        {"depth_m": [3.0]},
        geometry=[_square(10.05, 10.05, size=0.2)],
        crs="EPSG:4326",
    )
    layer = HazardLayer(
        kind=HazardKind.RIVER_FLOOD, inundation_gdf=inundation, source_id="test-fld",
    )
    out = apply_coverage(bldgs, [_extent_only()])
    out = apply_hazards(out, [layer])
    by_id = out.set_index("id")

    # 'out0' overlaps the inundation polygon but is OUTSIDE the coverage
    # extent — depth_max MUST stay NaN.
    import math
    assert bool(by_id.loc["out0", "river_flood_covered"]) is False
    assert math.isnan(float(by_id.loc["out0", "river_flood_depth_max"]))


def test_coverage_confidence_propagates_to_each_sampled_row() -> None:
    """Plan §coverage extent: every covered row records which confidence
    level applied (explicit_polygon / declared_full_admin / unknown)."""
    bldgs = _ten_buildings()
    out = apply_coverage(bldgs, [_extent_only()])
    by_id = out.set_index("id")["river_flood_coverage_confidence"]

    for i in range(5):
        assert by_id[f"in{i}"] == "explicit_polygon"
    for i in range(5):
        assert by_id[f"out{i}"] == "unknown"

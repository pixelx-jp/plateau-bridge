"""``apply_coverage`` / ``apply_hazards`` centroid-mode parity tests.

Centroid mode is the default fast path: it swaps each building's footprint
for its representative point and uses ``predicate="within"`` instead of
``intersects``. The honesty invariant must hold in both modes.
"""

from __future__ import annotations

import geopandas as gpd
from shapely.geometry import Polygon

from plateau_parquet.ops.intersect import apply_coverage, apply_hazards
from plateau_parquet.schema import CoverageConfidence, HazardKind
from plateau_parquet.sources.coverage import CoverageExtent
from plateau_parquet.sources.hazard import HazardLayer


def _square(x: float, y: float, size: float = 0.001) -> Polygon:
    return Polygon([(x, y), (x + size, y), (x + size, y + size), (x, y + size)])


def _setup() -> tuple[gpd.GeoDataFrame, CoverageExtent, HazardLayer]:
    # 4 buildings: 2 inside the coverage+inundation, 2 outside.
    bldgs = gpd.GeoDataFrame(
        {"id": ["a", "b", "c", "d"]},
        geometry=[_square(0, 0), _square(0.5, 0.5), _square(10, 10), _square(20, 20)],
        crs="EPSG:4326",
    )
    extent = gpd.GeoDataFrame(geometry=[_square(-0.1, -0.1, 2)], crs="EPSG:4326")
    cov = CoverageExtent(
        kind=HazardKind.RIVER_FLOOD,
        source_id="fld1",
        geometry=extent,
        confidence=CoverageConfidence.EXPLICIT_POLYGON,
    )
    # Flood polygon overlapping building 'a' and centroid of 'b'.
    flood = gpd.GeoDataFrame(
        {"depth_m": [3.0]},
        geometry=[_square(-0.1, -0.1, 1)],
        crs="EPSG:4326",
    )
    layer = HazardLayer(kind=HazardKind.RIVER_FLOOD, inundation_gdf=flood, source_id="fld1")
    return bldgs, cov, layer


def test_centroid_mode_matches_honesty_invariant() -> None:
    bldgs, cov, layer = _setup()
    out = apply_coverage(bldgs, [cov], centroid_mode=True)
    out = apply_hazards(out, [layer], centroid_mode=True)

    # a, b inside extent (covered=True); c, d outside (covered=False).
    cov_col = out.set_index("id")["river_flood_covered"]
    assert bool(cov_col["a"])
    assert bool(cov_col["b"])
    assert not bool(cov_col["c"])
    assert not bool(cov_col["d"])

    # Honesty: uncovered buildings MUST NOT have a depth value.
    import math
    depth = out.set_index("id")["river_flood_depth_max"]
    assert math.isnan(float(depth["c"]))
    assert math.isnan(float(depth["d"]))

    # Confidence escalates to explicit_polygon for covered rows.
    conf = out.set_index("id")["river_flood_coverage_confidence"]
    assert conf["a"] == "explicit_polygon"
    assert conf["c"] == "unknown"


def test_legacy_polygon_mode_still_works() -> None:
    """centroid_mode=False is the edge-precise fallback for users who need
    polygon-vs-polygon precision against small hazard polygons."""
    bldgs, cov, layer = _setup()
    out = apply_coverage(bldgs, [cov], centroid_mode=False)
    out = apply_hazards(out, [layer], centroid_mode=False)

    cov_col = out.set_index("id")["river_flood_covered"]
    assert bool(cov_col["a"])
    assert not bool(cov_col["c"])


def test_centroid_mode_default_is_true() -> None:
    """Regression: the public API must default to centroid_mode for the
    120× speedup on real PLATEAU data."""
    import inspect
    sig = inspect.signature(apply_coverage)
    assert sig.parameters["centroid_mode"].default is True
    sig = inspect.signature(apply_hazards)
    assert sig.parameters["centroid_mode"].default is True

"""Landslide uses ``in_zone`` (bool) instead of ``depth_max``.

This is the path that's easiest to break when refactoring — landslide is the
only hazard with a different value column, so it tends to fall out of
intersect logic. Guard it with its own test.
"""

from __future__ import annotations

import geopandas as gpd
from shapely.geometry import Polygon

from plateau_parquet.ops.intersect import apply_coverage, apply_hazards
from plateau_parquet.schema import CoverageConfidence, HazardKind
from plateau_parquet.sources.coverage import CoverageExtent
from plateau_parquet.sources.hazard import HazardLayer


def _sq(x: float, y: float, size: float = 0.1) -> Polygon:
    return Polygon([(x, y), (x + size, y), (x + size, y + size), (x, y + size)])


def test_landslide_in_zone_bool_not_depth() -> None:
    bldgs = gpd.GeoDataFrame(
        {"id": ["A", "B"]},
        geometry=[_sq(0, 0), _sq(10, 10)],
        crs="EPSG:4326",
    )
    extent = gpd.GeoDataFrame(geometry=[_sq(-1, -1, 5)], crs="EPSG:4326")
    cov = CoverageExtent(
        kind=HazardKind.LANDSLIDE,
        source_id="lsld1",
        geometry=extent,
        confidence=CoverageConfidence.EXPLICIT_POLYGON,
    )
    zones = gpd.GeoDataFrame({"in_zone": [True]}, geometry=[_sq(0, 0, 0.5)], crs="EPSG:4326")
    layer = HazardLayer(kind=HazardKind.LANDSLIDE, inundation_gdf=zones, source_id="lsld1")

    out = apply_coverage(bldgs, [cov])
    out = apply_hazards(out, [layer])

    a = out[out["id"] == "A"].iloc[0]
    b = out[out["id"] == "B"].iloc[0]

    assert bool(a["landslide_covered"]) is True
    assert bool(a["landslide_in_zone"]) is True
    # Landslide never gets a depth column populated.
    assert "landslide_depth_max" not in out.columns

    assert bool(b["landslide_covered"]) is False
    assert bool(b["landslide_in_zone"]) is False

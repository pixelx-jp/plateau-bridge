"""`plateau bench` smoke test against a tiny synthetic parquet."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pyarrow as pa  # noqa: F401  (ensure pyarrow available)
from shapely.geometry import Polygon

from plateau_parquet.bench import run_suite


def _make_minimal_parquet(path: Path, n: int = 100) -> None:
    rows = []
    for i in range(n):
        rows.append({
            "building_uid": f"u{i}",
            "year_built": (1980 + (i % 40)) if i % 3 else None,
            "structure": "wood" if i % 2 else "rc",
            "centroid_lon": 139.70 + (i % 10) * 0.001,
            "centroid_lat": 35.66 + (i % 10) * 0.001,
            "river_flood_covered": bool(i % 2),
            "river_flood_depth_max": (i % 5) / 2.0 if i % 2 else None,
        })
    gdf = gpd.GeoDataFrame(
        rows,
        geometry=[Polygon([(0, 0), (1, 0), (1, 1)])] * n,
        crs="EPSG:4326",
    )
    gdf.to_parquet(path, index=False)


def test_bench_runs_all_queries(tmp_path: Path) -> None:
    p = tmp_path / "buildings.parquet"
    _make_minimal_parquet(p)
    results = run_suite(p, iterations=3, warmup=1)
    names = {r.name for r in results}
    expected = {"filter_by_attrs", "decade_histogram", "river_flood_at_risk",
                "centroid_table", "bbox_count", "honesty_pivot"}
    assert names == expected
    for r in results:
        assert r.median_ms >= 0
        assert r.p99_ms >= r.median_ms
        assert r.runs == 3

"""``plateau diff`` smoke + correctness."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely.geometry import Polygon

from plateau_bridge.diff import diff


def _p(x: float, y: float, size: float = 0.001) -> Polygon:
    return Polygon([(x, y), (x + size, y), (x + size, y + size), (x, y + size)])


def _make(path: Path, ids: list[str], year: int, depth_map: dict[str, float | None]) -> None:
    n = len(ids)
    rows = []
    for uid in ids:
        d = depth_map.get(uid)
        rows.append({
            "building_uid": uid,
            "gml_id": uid,
            "dataset_year": year,
            "river_flood_covered": True,
            "river_flood_depth_max": d if d is not None else np.nan,
        })
    gpd.GeoDataFrame(
        rows, geometry=[_p(i * 0.01, 0) for i in range(n)], crs="EPSG:4326"
    ).to_parquet(path, index=False)


def test_diff_counts_matches_and_disjoint(tmp_path: Path) -> None:
    a = tmp_path / "a.parquet"
    b = tmp_path / "b.parquet"
    _make(a, ["x", "y", "z"], 2023, {"x": 1.0, "y": 0.5, "z": None})
    _make(b, ["y", "z", "w"], 2023, {"y": 1.5, "z": 0.5, "w": 3.0})

    r = diff(a, b)
    assert r.match_key == "building_uid"
    assert r.n_a == 3 and r.n_b == 3
    assert r.n_matched == 2          # y, z
    assert r.n_only_in_a == 1        # x gone
    assert r.n_only_in_b == 1        # w new


def test_diff_hazard_deltas(tmp_path: Path) -> None:
    a = tmp_path / "a.parquet"
    b = tmp_path / "b.parquet"
    _make(a, ["x", "y", "z"], 2023, {"x": 1.0, "y": 0.5, "z": None})
    _make(b, ["x", "y", "z"], 2023, {"x": 1.0, "y": 3.0, "z": 0.5})

    r = diff(a, b)
    fld = r.hazard_deltas["river_flood"]
    # z newly hit (was NaN → now 0.5).
    assert fld["newly_hit"] == 1
    # y depth grew (0.5 → 3.0).
    assert fld["depth_grew"] == 1
    # nothing shrank, nothing newly-covered.
    assert fld["depth_shrank"] == 0
    assert fld["newly_covered"] == 0

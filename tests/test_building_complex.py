"""Building complex grouping — touching footprints fuse, alleys don't."""

from __future__ import annotations

import geopandas as gpd
from shapely.geometry import Polygon

from plateau_parquet.ops.building_complex import compute_complexes


def _bldg(x: float, y: float, w: float, h: float, *, uid: str, height: float) -> dict:
    return {
        "building_uid": uid,
        "height": height,
        "geometry": Polygon([(x, y), (x + w, y), (x + w, y + h), (x, y + h)]),
    }


def _gdf(rows: list[dict]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(rows, crs="EPSG:3857")


def test_touching_footprints_fuse_with_max_height() -> None:
    # Two abutting rectangles → one complex; max height applies to both.
    g = _gdf([
        _bldg(0,   0, 10, 10, uid="a", height=30.0),
        _bldg(10,  0, 10, 10, uid="b", height=80.0),   # shares x=10 edge with a
    ])
    out = compute_complexes(g)
    assert out["complex_uid"].nunique() == 1
    assert (out["complex_max_height"] == 80.0).all()


def test_alley_split_keeps_buildings_separate() -> None:
    # Two rectangles with a 1.5 m alley between → two complexes.
    g = _gdf([
        _bldg(0,    0, 10, 10, uid="a", height=30.0),
        _bldg(11.5, 0, 10, 10, uid="b", height=80.0),
    ])
    out = compute_complexes(g)
    assert out["complex_uid"].nunique() == 2
    # Each remains its own cluster representative.
    assert out.loc[out["building_uid"] == "a", "complex_max_height"].iloc[0] == 30.0
    assert out.loc[out["building_uid"] == "b", "complex_max_height"].iloc[0] == 80.0


def test_tower_on_podium_cluster() -> None:
    # The motivating case: a 30 m podium and a 200 m tower sharing footprint.
    g = _gdf([
        _bldg(0, 0, 30, 30, uid="podium", height=30.0),
        _bldg(5, 5, 20, 20, uid="tower",  height=200.0),  # fully inside podium
    ])
    out = compute_complexes(g)
    assert out["complex_uid"].nunique() == 1
    assert (out["complex_max_height"] == 200.0).all()


def test_singletons_get_self_uid() -> None:
    g = _gdf([_bldg(0, 0, 10, 10, uid="lonely", height=20.0)])
    out = compute_complexes(g)
    assert out["complex_uid"].iloc[0] == "lonely"
    assert out["complex_max_height"].iloc[0] == 20.0


def test_chain_of_three_buildings_one_complex() -> None:
    # row of three attached townhouses → one complex with max=50.
    g = _gdf([
        _bldg(0,  0, 10, 10, uid="r1", height=20.0),
        _bldg(10, 0, 10, 10, uid="r2", height=50.0),
        _bldg(20, 0, 10, 10, uid="r3", height=15.0),
    ])
    out = compute_complexes(g)
    assert out["complex_uid"].nunique() == 1
    assert (out["complex_max_height"] == 50.0).all()

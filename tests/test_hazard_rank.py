"""PLATEAU河川浸水 depth rank parsing (Japanese labels from nusamai).

Real Shibuya 2023 ``floodingRiskAttribute`` is a JSON-encoded list of dicts
where the depth lives under a ``rank`` key as a Japanese string like
``"0.5m未満"``. We verified the schema against the actual output.
"""

from __future__ import annotations

import json

import geopandas as gpd
from shapely.geometry import Polygon

from plateau_bridge.schema import HazardKind
from plateau_bridge.sources.hazard import (
    RANK_LABEL_TO_METERS,
    _extract_rank_from_attr,
    normalise_depth,
)


def test_rank_labels_cover_known_classes() -> None:
    for label in ("0.5m未満", "0.5m以上3m未満", "3m以上5m未満", "5m以上10m未満", "20m以上"):
        assert label in RANK_LABEL_TO_METERS


def test_extract_rank_handles_json_string() -> None:
    raw = json.dumps([
        {"adminType": "都道府県", "rank": "0.5m以上3m未満", "scale": "L2"},
    ])
    assert _extract_rank_from_attr(raw) == 3.0


def test_extract_rank_picks_max_across_multiple_entries() -> None:
    raw = json.dumps([
        {"rank": "0.5m未満"},
        {"rank": "5m以上10m未満"},
        {"rank": "0.5m以上3m未満"},
    ])
    assert _extract_rank_from_attr(raw) == 10.0


def test_extract_rank_returns_none_for_unparseable() -> None:
    assert _extract_rank_from_attr(None) is None
    assert _extract_rank_from_attr("not json") is None
    assert _extract_rank_from_attr("[]") is None


def test_normalise_depth_uses_floodingRiskAttribute() -> None:
    g = Polygon([(0, 0), (1, 0), (1, 1)])
    gdf = gpd.GeoDataFrame(
        {
            "floodingRiskAttribute": [
                json.dumps([{"rank": "0.5m未満"}]),
                json.dumps([{"rank": "3m以上5m未満"}]),
                None,
            ]
        },
        geometry=[g, g, g],
        crs="EPSG:4326",
    )
    out = normalise_depth(gdf, HazardKind.RIVER_FLOOD)
    assert list(out["depth_m"])[:2] == [0.5, 5.0]
    import math
    assert math.isnan(out["depth_m"].iloc[2])

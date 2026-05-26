"""Gate B → Gate C handoff: tile keys must survive Gate C's parquet re-write.

Regression test for a real bug found running ABC against Shibuya 2023:
``run_gate_b`` set ``tile_content_uri`` / ``tile_feature_id`` columns on its
local gdf and wrote them to parquet, then ``run_gate_c`` (which received
Gate A's pre-tile-key gdf) re-wrote the parquet — silently zeroing the tile
key coverage to 0%.

The fix is that ``GateBResult`` now carries the enriched gdf and the CLI
threads it into Gate C. This test pins that behaviour.
"""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
from shapely.geometry import Polygon

from plateau_bridge.pipeline.gate_b import GateBResult


def test_gate_b_result_carries_enriched_gdf() -> None:
    """The dataclass field exists and round-trips."""
    g = Polygon([(0, 0), (1, 0), (1, 1)])
    gdf = gpd.GeoDataFrame(
        {
            "building_uid": ["u1", "u2"],
            "tile_content_uri": ["t/a.glb", "t/b.glb"],
            "tile_feature_id": pd.array([0, 0], dtype="Int32"),
        },
        geometry=[g, g],
        crs="EPSG:4326",
    )
    res = GateBResult(
        style_dir=None,  # type: ignore[arg-type]
        tile_index_path=None,  # type: ignore[arg-type]
        buildings_parquet=None,  # type: ignore[arg-type]
        verified=True,
        gdf=gdf,
    )
    assert res.gdf is gdf
    assert res.gdf["tile_content_uri"].notna().all()
    assert res.gdf["tile_feature_id"].notna().all()

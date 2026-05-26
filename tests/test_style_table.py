from __future__ import annotations

import pyarrow as pa

from plateau_bridge.ops.style_table import encode_tile_uri, write_style_tables


def test_encode_tile_uri_is_url_safe() -> None:
    assert encode_tile_uri("tiles/13/abc.glb") == "tiles%2F13%2Fabc.glb"


def test_write_style_tables_partitions_by_tile(tmp_path) -> None:
    table = pa.table(
        {
            "tile_content_uri": ["tiles/a.glb", "tiles/a.glb", "tiles/b.glb"],
            "tile_feature_id": [0, 1, 0],
            "building_uid": ["u1", "u2", "u3"],
            "year_built": [1980, 1995, 2010],
        }
    )
    out = tmp_path / "style"
    index = write_style_tables(table, out)
    assert len(index) == 2
    assert (out / "tiles%2Fa.glb.arrow").exists()
    assert (out / "tiles%2Fb.glb.arrow").exists()

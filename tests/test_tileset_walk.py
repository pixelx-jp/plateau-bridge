"""Walking a tileset.json including external-tileset references."""

from __future__ import annotations

import json
from pathlib import Path

from plateau_parquet.ops.tiles3d import list_contents, walk_tileset


def test_walk_nested_external_tileset(tmp_path: Path) -> None:
    # root tileset references child.json; child references a.glb and b.glb.
    child = tmp_path / "child.json"
    child.write_text(
        json.dumps(
            {
                "root": {
                    "children": [
                        {"content": {"uri": "a.glb"}},
                        {"content": {"uri": "b.glb"}},
                    ]
                }
            }
        )
    )
    root = tmp_path / "tileset.json"
    root.write_text(
        json.dumps(
            {
                "root": {
                    "children": [{"content": {"uri": "child.json"}}]
                }
            }
        )
    )

    uris = list_contents(tmp_path)
    assert sorted(uris) == ["a.glb", "b.glb"]
    pairs = list(walk_tileset(root))
    assert len(pairs) == 2

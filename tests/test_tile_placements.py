"""Multi-LOD placement collection.

Regression: a building exists at every LOD level it spans (nusamai writes
copies at LOD15/16/17/18). Earlier code did ``mapping[gml_id] = (uri, fid)``
and overwrote — so only whichever LOD was walked last got entries in
``tile_index.json``. When the renderer dropped to lower LODs (zoom-out) the
buildings rendered grey because no style table existed. Test that
``collect_tile_placements`` preserves every (gml_id, uri, fid) triple.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from plateau_parquet.ops import tiles3d


def test_collect_tile_placements_preserves_all_lods(tmp_path: Path) -> None:
    # Stub the tileset walk to yield three "tile" URIs at LOD15/17/18.
    fake_uris = ["15/a.glb", "17/b.glb", "18/c.glb"]

    def fake_walk(_path):  # noqa: ARG001
        for u in fake_uris:
            yield u, {}

    # Same building "bldg-1" appears at every LOD with a different feature_id.
    # An extra building "bldg-2" only appears at LOD18.
    per_uri_ids = {
        "15/a.glb": ["bldg-1", "neighbour-1"],
        "17/b.glb": ["bldg-1"],
        "18/c.glb": ["bldg-1", "bldg-2"],
    }

    def fake_extract(glb_path):
        # walk_tileset yields relative URIs; collect_tile_placements joins to
        # tileset_dir then asks if the path exists. We bypass existence check
        # by patching _extract_gml_ids_from_glb directly and reading the URI
        # from glb_path.name's parent path.
        rel = glb_path.relative_to(tmp_path).as_posix()
        return per_uri_ids.get(rel)

    # Touch the files so the existence guard in collect_tile_placements
    # passes.
    for u in fake_uris:
        p = tmp_path / u
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")

    with patch.object(tiles3d, "walk_tileset", fake_walk), \
         patch.object(tiles3d, "_extract_gml_ids_from_glb", fake_extract):
        out = tiles3d.collect_tile_placements(tmp_path)

    # bldg-1 must appear in ALL three LODs.
    by_gid: dict[str, list[tuple[str, int]]] = {}
    for gid, uri, fid in out:
        by_gid.setdefault(gid, []).append((uri, fid))
    assert sorted(uri for uri, _ in by_gid["bldg-1"]) == ["15/a.glb", "17/b.glb", "18/c.glb"]
    # neighbour-1 only at LOD15.
    assert by_gid["neighbour-1"] == [("15/a.glb", 1)]
    # bldg-2 only at LOD18.
    assert by_gid["bldg-2"] == [("18/c.glb", 1)]

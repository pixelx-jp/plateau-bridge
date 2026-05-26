"""Per-tile Arrow IPC style tables for 3D Tiles in-browser shading.

The renderer holds only ``(tile_content_uri, feature_id)`` when shading a glTF
batch from a 3D Tile. Asking PMTiles for that lookup is a category error
(PMTiles is a spatial index, not a KV store). Instead we ship one tiny Arrow
file per tile_content_uri, keyed by feature_id.

File naming: ``encodeURIComponent(tile_content_uri)`` becomes the filename, in
one flat directory. We do not nest by tile path — that would create thousands
of single-file directories that hurt HTTP/2 perf.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote

import pyarrow as pa
import pyarrow.feather as feather

log = logging.getLogger(__name__)


# Columns that the r3f colorBy demo needs in-tile. Keep this list short — every
# extra column inflates per-tile asset size. Heavy attrs stay in parquet.
DEFAULT_STYLE_COLUMNS: tuple[str, ...] = (
    "building_uid",
    "tile_feature_id",
    "year_built",
    "structure",
    "usage",
    "height",
    "complex_max_height",  # cluster-aware height; renderer prefers this
    "complex_uid",
    "floors_above",
    "river_flood_covered",
    "river_flood_depth_max",
    "tsunami_covered",
    "tsunami_depth_max",
    "landslide_covered",
    "landslide_in_zone",
)


def encode_tile_uri(tile_content_uri: str) -> str:
    """URL-encode the tile uri for a single-level filename."""
    # safe="" so every / becomes %2F.
    return quote(tile_content_uri, safe="")


def write_style_tables(
    table: pa.Table,
    out_dir: Path,
    *,
    tile_uri_col: str = "tile_content_uri",
    columns: tuple[str, ...] = DEFAULT_STYLE_COLUMNS,
) -> dict[str, str]:
    """Partition ``table`` by ``tile_content_uri`` and emit one Arrow IPC per tile.

    Returns a mapping ``tile_content_uri -> relative_style_path`` for ``tile_index.json``.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    if tile_uri_col not in table.column_names:
        raise KeyError(f"column {tile_uri_col!r} not in input table")

    # Restrict to the columns the browser actually needs.
    keep = [c for c in columns if c in table.column_names]
    if tile_uri_col not in keep:
        # We don't ship the uri inside the per-tile file (redundant), but keep
        # it during the groupby step.
        pass
    slim = table.select(keep + [tile_uri_col])

    # Partition by unique tile_content_uri values. pyarrow has no direct groupby
    # partition writer, so we use combine_chunks + a Python loop. Acceptable for
    # ~thousands of tiles.
    uris = slim.column(tile_uri_col).to_pylist()
    unique = sorted({u for u in uris if u})
    index: dict[str, str] = {}
    for uri in unique:
        mask = pa.compute.equal(slim.column(tile_uri_col), uri)
        subset = slim.filter(mask).drop([tile_uri_col])
        encoded = encode_tile_uri(uri)
        rel = f"style/{encoded}.arrow"
        path = out_dir.parent / rel if out_dir.name == "style" else out_dir / f"{encoded}.arrow"
        # Use the simpler form: out_dir already points at .../style/.
        path = out_dir / f"{encoded}.arrow"
        # IMPORTANT: apache-arrow JS doesn't implement compressed-record-batch
        # decoding; per-tile files are tiny (~20 KB) so leave uncompressed.
        feather.write_feather(subset, path, compression="uncompressed")
        index[uri] = rel if rel.startswith("style/") else f"style/{encoded}.arrow"
    log.info("wrote %d style tables to %s", len(unique), out_dir)
    return index

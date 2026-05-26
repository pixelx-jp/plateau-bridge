"""Walk a 3D Tiles tileset.json, read per-feature metadata, emit the global mapping.

PLATEAU 3D Tiles 1.1 carries per-feature attributes via the modern
``EXT_mesh_features`` (which says "feature_id = this vertex attribute") plus
``EXT_structural_metadata`` (which says "look up properties for that
feature_id in this property table"). Legacy assets still use ``_BATCHID``
inside the b3dm batch table; we handle both.

A feature_id is only unique **within a single content (.glb)**; the global
key is ``(tile_content_uri, tile_feature_id)``. This module attaches both
columns to the buildings GeoDataFrame and provides a verifier.
"""

from __future__ import annotations

import json
import logging
import struct
from collections.abc import Iterator
from pathlib import Path

log = logging.getLogger(__name__)

# CityGML gml:id property names we expect in PLATEAU property tables.
GML_ID_PROPERTY_NAMES: tuple[str, ...] = ("gml_id", "gmlId", "id", "建物ID")


def walk_tileset(tileset_json_path: Path) -> Iterator[tuple[str, dict]]:
    """Yield ``(content_uri_relative, tile_node)`` for every leaf content.

    Honours external tilesets referenced via ``content.uri`` ending in
    ``.json``: we recurse into them so the entire hierarchy is yielded.
    """
    root_dir = tileset_json_path.parent
    root = json.loads(tileset_json_path.read_text(encoding="utf-8"))

    def _walk(node: dict, base: Path) -> Iterator[tuple[str, dict]]:
        content = node.get("content")
        if content and "uri" in content:
            uri = content["uri"]
            if uri.endswith(".json"):
                # external tileset; recurse
                ext_path = base / uri
                if ext_path.exists():
                    ext = json.loads(ext_path.read_text(encoding="utf-8"))
                    yield from _walk(ext["root"], ext_path.parent)
            else:
                # path is relative to the tileset.json that introduced this content
                rel = str((base / uri).relative_to(root_dir))
                yield rel, node
        for child in node.get("children", []):
            yield from _walk(child, base)

    yield from _walk(root["root"], root_dir)


def list_contents(tileset_dir: Path) -> list[str]:
    """Return all relative content URIs under ``<dir>/tileset.json``."""
    return [uri for uri, _ in walk_tileset(tileset_dir / "tileset.json")]


def _read_property_strings(
    gltf, table: dict, property_name: str, buffers: list[bytes]
) -> list[str] | None:
    """Read a STRING property column from an EXT_structural_metadata table.

    The binary layout: ``values`` is a UTF-8 buffer of concatenated strings;
    ``stringOffsets`` is a buffer of (count+1) offsets into ``values``. Both
    are referenced by bufferView index.
    """
    props = table.get("properties", {})
    p = props.get(property_name)
    if p is None:
        return None

    def _bv_bytes(bv_index: int) -> bytes:
        bv = gltf.bufferViews[bv_index]
        buf = buffers[bv.buffer]
        start = bv.byteOffset or 0
        return buf[start : start + bv.byteLength]

    values = _bv_bytes(p["values"])
    offsets_raw = _bv_bytes(p["stringOffsets"])
    # Default offset type is uint32; spec allows uint8/16/32/64.
    offset_type = p.get("stringOffsetType", "UINT32")
    fmt = {"UINT8": "B", "UINT16": "H", "UINT32": "I", "UINT64": "Q"}[offset_type]
    size = struct.calcsize(fmt)
    n = len(offsets_raw) // size
    offsets = list(struct.unpack(f"<{n}{fmt}", offsets_raw))
    out: list[str] = []
    for i in range(len(offsets) - 1):
        out.append(values[offsets[i] : offsets[i + 1]].decode("utf-8", errors="replace"))
    return out


def _extract_gml_ids_from_glb(glb_path: Path) -> list[str] | None:
    """Return list of gml_ids indexed by feature_id, or None if metadata absent."""
    try:
        from pygltflib import GLTF2  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        gltf = GLTF2().load(str(glb_path))
    except Exception as e:  # noqa: BLE001
        log.warning("failed to load %s: %s", glb_path, e)
        return None
    ext = (gltf.extensions or {}).get("EXT_structural_metadata")
    if not ext or "propertyTables" not in ext:
        return None

    # Resolve buffer data. pygltflib stashes binary chunks under .binary_blob().
    blob = gltf.binary_blob()
    buffers: list[bytes] = []
    for b in gltf.buffers:
        if b.uri is None:
            buffers.append(blob or b"")
        else:
            # External buffer; resolve relative to glb path.
            ext_path = glb_path.parent / b.uri
            buffers.append(ext_path.read_bytes() if ext_path.exists() else b"")

    for table in ext["propertyTables"]:
        for name in GML_ID_PROPERTY_NAMES:
            ids = _read_property_strings(gltf, table, name, buffers)
            if ids:
                return ids
    return None


def collect_tile_placements(tileset_dir: Path) -> list[tuple[str, str, int]]:
    """Walk the tileset and return every (gml_id, tile_content_uri, tile_feature_id).

    nusamai emits the same building at every LOD it spans (15/16/17/18) — each
    copy has its own EXT_structural_metadata with a tile-local feature_id. We
    need the full cross product so style tables can be written for ALL LODs,
    not just whichever was walked last.
    """
    out: list[tuple[str, str, int]] = []
    for uri, _ in walk_tileset(tileset_dir / "tileset.json"):
        glb_path = tileset_dir / uri
        if not glb_path.exists():
            continue
        ids = _extract_gml_ids_from_glb(glb_path)
        if not ids:
            continue
        for feature_id, gid in enumerate(ids):
            out.append((gid, uri, feature_id))
    return out


def attach_tile_keys(
    buildings_df,
    tileset_dir: Path,
    *,
    gml_id_col: str = "gml_id",
):
    """Attach a canonical ``tile_content_uri`` and ``tile_feature_id``.

    The canonical placement is the deepest LOD a building appears in — that's
    the one downstream parquet consumers care about for high-res inspection.
    For the full multi-LOD cross product (needed by style-table emission), see
    :func:`collect_tile_placements`.

    Returns the dataframe with the two columns back-filled; missing buildings
    get NULL.
    """
    df = buildings_df.copy()
    df["tile_content_uri"] = None
    df["tile_feature_id"] = None

    placements = collect_tile_placements(tileset_dir)
    if not placements:
        log.warning(
            "no per-feature gml_id metadata found in tileset; "
            "tile_content_uri/tile_feature_id will be NULL. "
            "Install pygltflib and confirm the 3D Tiles export carried "
            "EXT_structural_metadata."
        )
        return df

    # Canonical = deepest LOD. Tile URIs are "<lod>/x/y_bldg_Building.glb";
    # parse the leading integer.
    def _lod(uri: str) -> int:
        head = uri.split("/", 1)[0]
        try:
            return int(head)
        except ValueError:
            return -1

    canonical: dict[str, tuple[str, int]] = {}
    for gid, uri, fid in placements:
        prev = canonical.get(gid)
        if prev is None or _lod(uri) > _lod(prev[0]):
            canonical[gid] = (uri, fid)

    gid_series = df[gml_id_col].astype(str)
    df["tile_content_uri"] = gid_series.map(lambda g: canonical.get(g, (None, None))[0])
    df["tile_feature_id"] = gid_series.map(lambda g: canonical.get(g, (None, None))[1])
    matched = df["tile_content_uri"].notna().sum()
    log.info(
        "attached canonical tile keys to %d / %d buildings (%d total placements across all LODs)",
        matched, len(df), len(placements),
    )
    return df


def verify_feature_id_mapping(
    buildings_df,
    sample_n: int = 10,
) -> bool:
    """Sample N buildings and assert (tile_content_uri, tile_feature_id) is unique."""
    eligible = buildings_df[buildings_df["tile_content_uri"].notna()]
    if eligible.empty:
        return False
    sample = eligible.sample(min(sample_n, len(eligible)))
    pairs = list(zip(sample["tile_content_uri"], sample["tile_feature_id"], strict=True))
    if len(set(pairs)) != len(pairs):
        log.error("duplicate (tile_content_uri, feature_id) detected in sample of %d", sample_n)
        return False
    # Additional sanity: feature_id=0 should appear in more than one tile (else
    # the "joint key" claim is vacuous on this sample).
    by_uri: dict[str, set[int]] = {}
    for u, f in pairs:
        by_uri.setdefault(u, set()).add(int(f))
    log.info("verified %d tile-scoped feature ids across %d tiles", len(pairs), len(by_uri))
    return True

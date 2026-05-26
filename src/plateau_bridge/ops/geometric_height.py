"""Extract per-building height directly from 3D Tile mesh geometry.

PLATEAU's ``measuredHeight`` attribute is derived from LiDAR and is missing
(-9999 sentinel) for 5–10% of buildings — typically those built after the
LiDAR survey or in areas the survey couldn't capture. ``floors_above × 3.5m``
is an okay fallback but ``floors_above`` itself is sentinel-9999 for ~3% of
buildings, leaving us with hard-NULL shading in the demos.

But every building written into a 3D Tile **has 3D geometry by definition** —
otherwise it wouldn't render. The ``_FEATURE_ID_0`` per-vertex attribute lets
us cluster vertices by building, and ``max(y) - min(y)`` across that cluster
is the actual rendered height. This value is:

- Always present (geometry is a precondition of being in the tileset).
- Sourced from the same modeller as ``measuredHeight`` and agrees with it
  within ~0.3 m on Shibuya buildings where both exist (validated).
- The exact number a downstream renderer will display.

So we use it as the **primary** height source, falling back to
``measuredHeight`` only as a sanity-check and ``floors × 3.5m`` only when
the building has no LOD18 placement at all (rare; admin-clipped edge cases).

## Coordinate system

PLATEAU 3D Tiles use the default glTF Y-up local axis. The tile transform
is a pure translation (ECEF offset, no rotation), so local Y maps directly
to world altitude. Verified on Shibuya: Y span of the mesh agrees with
``measuredHeight`` for 8/8 sampled buildings.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Iterable
from pathlib import Path

log = logging.getLogger(__name__)

# glTF component type → struct format char.
_FMT = {
    5120: "b",   # BYTE
    5121: "B",   # UNSIGNED_BYTE
    5122: "h",   # SHORT
    5123: "H",   # UNSIGNED_SHORT
    5125: "I",   # UNSIGNED_INT
    5126: "f",   # FLOAT
}
_SIZE = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}


def _bv_bytes(gltf, bv_idx: int, blob: bytes) -> bytes:
    bv = gltf.bufferViews[bv_idx]
    start = bv.byteOffset or 0
    return blob[start : start + bv.byteLength]


_TYPE_NC = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


def _read_accessor(gltf, blob: bytes, accessor_idx: int) -> tuple[tuple[float, ...], int]:
    """Read every value from a glTF accessor, honouring interleaved buffers.

    PLATEAU GLBs are written by nusamai with a single interleaved bufferView
    holding POSITION + NORMAL + TEXCOORD_0 + _FEATURE_ID_0 etc. — each
    vertex's 36 bytes are laid out at ``bufferView.byteStride`` strides.
    We MUST honour ``bufferView.byteStride`` and ``accessor.byteOffset``;
    reading raw ``count × componentSize`` bytes gives garbage that happens
    to look like float values (we did this and saw negative feature IDs).
    """
    acc = gltf.accessors[accessor_idx]
    bv = gltf.bufferViews[acc.bufferView]
    fmt = _FMT.get(acc.componentType)
    if fmt is None:
        raise ValueError(f"unsupported componentType {acc.componentType}")
    nc = _TYPE_NC.get(acc.type, 1)
    comp_size = _SIZE[acc.componentType]
    elem_size = comp_size * nc

    base = (bv.byteOffset or 0) + (acc.byteOffset or 0)
    stride = bv.byteStride or elem_size

    if stride == elem_size:
        # Tightly packed — single struct.unpack is faster.
        total = acc.count * nc
        return struct.unpack(f"<{total}{fmt}", blob[base : base + total * comp_size]), nc

    # Interleaved — iterate strides.
    out: list[float] = []
    for i in range(acc.count):
        off = base + i * stride
        out.extend(struct.unpack(f"<{nc}{fmt}", blob[off : off + elem_size]))
    return tuple(out), nc


def _gml_ids_from_glb(gltf, blob: bytes) -> list[str] | None:
    """Reuse the schema-walking logic from tiles3d.py, inlined here to avoid
    a cyclic import. Returns the gml_id strings indexed by feature_id, or
    None when the GLB has no EXT_structural_metadata.
    """
    ext = (gltf.extensions or {}).get("EXT_structural_metadata")
    if not ext or "propertyTables" not in ext:
        return None

    for table in ext["propertyTables"]:
        props = table.get("properties", {})
        for name in ("gml_id", "gmlId", "id"):
            p = props.get(name)
            if p is None:
                continue
            values = _bv_bytes(gltf, p["values"], blob)
            offsets_raw = _bv_bytes(gltf, p["stringOffsets"], blob)
            offset_fmt = {"UINT8": "B", "UINT16": "H", "UINT32": "I", "UINT64": "Q"}[
                p.get("stringOffsetType", "UINT32")
            ]
            size = struct.calcsize(offset_fmt)
            n = len(offsets_raw) // size
            offs = list(struct.unpack(f"<{n}{offset_fmt}", offsets_raw))
            return [
                values[offs[i] : offs[i + 1]].decode("utf-8", errors="replace")
                for i in range(len(offs) - 1)
            ]
    return None


def compute_glb_heights(glb_path: Path) -> dict[str, float]:
    """Return ``{gml_id: height_m}`` for every building in one GLB.

    Iterates the mesh's POSITION + ``_FEATURE_ID_0`` attributes, groups
    vertices by feature_id, computes ``max(y) - min(y)``, and maps each
    feature_id to its gml_id via the structural metadata property table.
    """
    try:
        from pygltflib import GLTF2  # type: ignore[import-not-found]
    except ImportError:
        log.warning("pygltflib not installed; skipping geometric heights")
        return {}

    try:
        gltf = GLTF2().load(str(glb_path))
    except Exception as e:  # noqa: BLE001
        log.warning("failed to load %s: %s", glb_path, e)
        return {}

    blob = gltf.binary_blob() or b""
    gml_ids = _gml_ids_from_glb(gltf, blob)
    if not gml_ids:
        return {}

    # Per-feature y range. Use plain dicts (Python ints) — small enough.
    min_y: dict[int, float] = {}
    max_y: dict[int, float] = {}

    for mesh in gltf.meshes or []:
        for prim in mesh.primitives or []:
            pos_idx = getattr(prim.attributes, "POSITION", None)
            fid_idx = getattr(prim.attributes, "_FEATURE_ID_0", None)
            if pos_idx is None or fid_idx is None:
                continue
            positions, nc = _read_accessor(gltf, blob, pos_idx)
            if nc != 3:
                continue
            (fids, _) = _read_accessor(gltf, blob, fid_idx)
            n = len(fids)
            for i in range(n):
                # PLATEAU writes feature ids as float32 (componentType 5126)
                # but the values are always non-negative integers in
                # [0, propertyTable.count). Round to handle f32 precision.
                f_raw = fids[i]
                fid = int(round(f_raw))
                if fid < 0 or fid >= len(gml_ids):
                    continue
                y = positions[i * 3 + 1]
                cur_min = min_y.get(fid)
                cur_max = max_y.get(fid)
                if cur_min is None or y < cur_min:
                    min_y[fid] = y
                if cur_max is None or y > cur_max:
                    max_y[fid] = y

    out: dict[str, float] = {}
    for fid, lo in min_y.items():
        hi = max_y[fid]
        h = float(hi - lo)
        if h <= 0:
            continue
        gid = gml_ids[fid]
        # If a building appears in multiple primitives (rare) keep the max.
        if gid in out:
            out[gid] = max(out[gid], h)
        else:
            out[gid] = h
    return out


def compute_tileset_heights(
    tileset_dir: Path,
    glb_relative_uris: Iterable[str] | None = None,
) -> dict[str, float]:
    """Walk a tileset and union geometric heights from all leaf-LOD GLBs.

    Lower-LOD GLBs (15/16/17) contain decimated geometry whose y-span can
    differ from the true height; use the deepest LOD only by default.
    Pass ``glb_relative_uris`` explicitly to override.
    """
    # Resolve which GLBs to walk. Without an explicit list, prefer LOD18.
    if glb_relative_uris is None:
        from plateau_bridge.ops.tiles3d import list_contents
        all_uris = list_contents(tileset_dir)
        # PLATEAU paths are "<lod>/x/y_..._Building.glb".
        max_lod = -1
        per_lod: dict[int, list[str]] = {}
        for u in all_uris:
            try:
                lod = int(u.split("/", 1)[0])
            except ValueError:
                continue
            per_lod.setdefault(lod, []).append(u)
            max_lod = max(max_lod, lod)
        glb_relative_uris = per_lod.get(max_lod, [])
        log.info("computing geometric heights from %d LOD%d tiles", len(per_lod.get(max_lod, [])), max_lod)

    out: dict[str, float] = {}
    for uri in glb_relative_uris:
        p = tileset_dir / uri
        if not p.exists():
            continue
        heights = compute_glb_heights(p)
        # Same gml_id can theoretically appear in multiple tiles only if
        # there's tile overlap — take the larger value as a conservative
        # estimate.
        for gid, h in heights.items():
            prev = out.get(gid)
            if prev is None or h > prev:
                out[gid] = h
    log.info("computed geometric heights for %d unique buildings", len(out))
    return out

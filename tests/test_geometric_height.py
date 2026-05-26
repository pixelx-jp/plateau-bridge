"""Geometric height extraction from GLB mesh.

PLATEAU's ``measuredHeight`` attribute is sentinel-NULL for ~5–10 % of
buildings; we fall back to the per-feature mesh Y bbox. The test builds a
synthetic interleaved-attribute GLB (matching nusamai's layout) with two
"buildings" of known height (10 m and 20 m), then asserts the extractor
returns those exact values.

The interleaving guard is critical — earlier code read attribute bytes
ignoring ``bufferView.byteStride`` and got garbage values (negative
"feature IDs", random heights). The test fails immediately if that
regression returns.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

pygltflib = pytest.importorskip("pygltflib")


def _build_synthetic_glb(path: Path) -> None:
    """Two boxy "buildings" sharing one interleaved bufferView.

    Layout per vertex (36 bytes, matches nusamai):
        POSITION  vec3 float  12 bytes  offset 0
        NORMAL    vec3 float  12 bytes  offset 12
        TEXCOORD0 vec2 float   8 bytes  offset 20
        _FEATURE_ID_0 float    4 bytes  offset 28
        (pad 4 bytes to 36)
    """
    # Building 0: 8 vertices, y in [0, 10]
    # Building 1: 8 vertices, y in [0, 20]
    verts: list[tuple[float, float, float, int]] = []
    for fid, h in [(0, 10.0), (1, 20.0)]:
        for x, y, z in [
            (0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1),
            (0, h, 0), (1, h, 0), (1, h, 1), (0, h, 1),
        ]:
            verts.append((x, y, z, fid))

    n = len(verts)
    stride = 36
    buf = bytearray(n * stride)
    for i, (x, y, z, fid) in enumerate(verts):
        off = i * stride
        struct.pack_into("<3f", buf, off, x, y, z)              # POSITION
        struct.pack_into("<3f", buf, off + 12, 0, 1, 0)         # NORMAL
        struct.pack_into("<2f", buf, off + 24, 0, 0)            # TEXCOORD_0
        struct.pack_into("<f", buf, off + 28, float(fid))       # _FEATURE_ID_0
        # last 4 bytes padding

    # Build the structural metadata property table: gml_ids as variable-length strings.
    gml_strs = ["bldg-A", "bldg-B"]
    values = "".join(gml_strs).encode()
    offsets = []
    cur = 0
    for s in gml_strs:
        offsets.append(cur)
        cur += len(s.encode())
    offsets.append(cur)
    offsets_buf = struct.pack(f"<{len(offsets)}I", *offsets)

    # Combine: vertex buffer + values_buf + offsets_buf as one binary chunk.
    combined = bytes(buf) + values + offsets_buf
    # Pad to multiple of 4.
    while len(combined) % 4:
        combined += b"\0"

    from pygltflib import (  # type: ignore[import-not-found]
        GLTF2,
        Accessor,
        Attributes,
        Buffer,
        BufferView,
        Mesh,
        Node,
        Primitive,
        Scene,
    )
    gltf = GLTF2()
    gltf.scenes = [Scene(nodes=[0])]
    gltf.scene = 0
    gltf.nodes = [Node(mesh=0)]

    vertex_bv_len = n * stride
    bv_vertex = BufferView(buffer=0, byteOffset=0, byteLength=vertex_bv_len, byteStride=stride)
    bv_values = BufferView(buffer=0, byteOffset=vertex_bv_len, byteLength=len(values))
    bv_offsets = BufferView(buffer=0, byteOffset=vertex_bv_len + len(values), byteLength=len(offsets_buf))
    gltf.bufferViews = [bv_vertex, bv_values, bv_offsets]

    gltf.accessors = [
        Accessor(bufferView=0, byteOffset=0,  componentType=5126, count=n, type="VEC3"),  # POSITION
        Accessor(bufferView=0, byteOffset=12, componentType=5126, count=n, type="VEC3"),  # NORMAL
        Accessor(bufferView=0, byteOffset=24, componentType=5126, count=n, type="VEC2"),  # TEXCOORD_0
        Accessor(bufferView=0, byteOffset=28, componentType=5126, count=n, type="SCALAR"),  # _FEATURE_ID_0
    ]
    prim = Primitive(attributes=Attributes(POSITION=0, NORMAL=1, TEXCOORD_0=2), mode=4)
    prim.attributes._FEATURE_ID_0 = 3
    gltf.meshes = [Mesh(primitives=[prim])]
    gltf.buffers = [Buffer(byteLength=len(combined))]

    gltf.extensionsUsed = ["EXT_structural_metadata", "EXT_mesh_features"]
    gltf.extensions = {
        "EXT_structural_metadata": {
            "schema": {
                "classes": {
                    "bldg": {"properties": {"gml_id": {"type": "STRING"}}}
                }
            },
            "propertyTables": [
                {
                    "class": "bldg",
                    "count": 2,
                    "properties": {
                        "gml_id": {
                            "values": 1,
                            "stringOffsets": 2,
                            "stringOffsetType": "UINT32",
                        }
                    },
                }
            ],
        }
    }
    prim.extensions = {  # type: ignore[attr-defined]
        "EXT_mesh_features": {
            "featureIds": [{"featureCount": 2, "attribute": 0, "propertyTable": 0}]
        }
    }

    gltf.set_binary_blob(combined)
    gltf.save_binary(str(path))


def test_per_feature_height_matches_y_bbox(tmp_path: Path) -> None:
    from plateau_bridge.ops.geometric_height import compute_glb_heights

    glb = tmp_path / "synth.glb"
    _build_synthetic_glb(glb)

    heights = compute_glb_heights(glb)

    assert heights == {"bldg-A": 10.0, "bldg-B": 20.0}, heights


def test_interleaved_stride_is_honoured(tmp_path: Path) -> None:
    """Regression: if we read attribute bytes packed (ignoring byteStride),
    we'd see negative / out-of-range feature IDs and return nothing or
    nonsense heights. This synthetic GLB uses ``byteStride = 36`` exactly
    like PLATEAU's; if the reader regresses to packed reads, neither
    expected height appears in the output.
    """
    from plateau_bridge.ops.geometric_height import compute_glb_heights

    glb = tmp_path / "synth.glb"
    _build_synthetic_glb(glb)
    heights = compute_glb_heights(glb)

    # 20.0 m would not appear with a packed read — the b'd vertex bytes
    # for building B's Y would alias to NORMAL/TEXCOORD bytes (zeros).
    assert 20.0 in heights.values()

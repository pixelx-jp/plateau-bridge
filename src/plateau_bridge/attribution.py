"""Helpers to inject ``© Project PLATEAU / MLIT (CC BY 4.0)`` into outputs.

Per plan §Attribution: every artifact carries provenance, so downstream tools
(poster renderers, GLB exporters, MCP servers) inherit it automatically.

For PMTiles / FlatGeobuf / GeoParquet the attribution is a row field on
every record (set in gate_a). For matplotlib posters it's a corner overlay
(set in poster.py). For 3D Tiles output (potentially tens of thousands of
glb files per city) we stamp the **tileset.json root**, which 3D Tiles 1.1
consumers read as authoritative attribution for every tile under it —
saves rewriting every glb. ``attach_to_glb_extras`` is still available for
single-glb edits (e.g. derivative exports).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from plateau_bridge.schema import ATTRIBUTION

log = logging.getLogger(__name__)


def stamp_tileset_json(
    tileset_json_path: Path,
    *,
    manifest: dict | None = None,
) -> None:
    """Set ``asset.extras.attribution`` on the root 3D Tiles tileset.

    Per the 3D Tiles 1.1 spec, ``tileset.asset.extras`` propagates to every
    tile under the tileset — clients (Cesium, 3d-tiles-renderer, deck.gl)
    read this as the canonical attribution. No per-tile glb edit needed.
    """
    if not tileset_json_path.exists():
        log.warning("tileset.json not found at %s; skipping attribution", tileset_json_path)
        return
    tileset = json.loads(tileset_json_path.read_text(encoding="utf-8"))
    asset = tileset.setdefault("asset", {})
    extras = asset.setdefault("extras", {})
    extras["attribution"] = ATTRIBUTION
    if manifest is not None:
        # Embed a minimal pointer rather than the whole manifest — glb consumers
        # don't need our full coverage stats inline.
        extras["plateau_bridge"] = {
            "tool_version": manifest.get("tool_version"),
            "datasets": manifest.get("datasets", []),
            "city_code": manifest.get("city_code"),
            "dataset_year": manifest.get("dataset_year"),
        }
    tileset_json_path.write_text(json.dumps(tileset, indent=2), encoding="utf-8")
    log.info("stamped attribution → %s", tileset_json_path)


def attach_to_glb_extras(glb_path: Path, manifest_path: Path | None = None) -> None:
    """Set ``asset.extras.attribution`` on a single glTF/GLB.

    Slow path — for cities with thousands of tiles, prefer
    ``stamp_tileset_json`` which marks the root once and lets the 3D Tiles
    spec propagate. Use this for derivative single-glb exports
    (e.g. extracting one building's GLB for a downstream tool).
    """
    try:
        from pygltflib import GLTF2  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "pygltflib is required to attach attribution to .glb files"
        ) from e

    gltf = GLTF2().load(str(glb_path))
    extras = gltf.asset.extras or {}
    extras["attribution"] = ATTRIBUTION
    if manifest_path is not None:
        extras["manifest"] = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    gltf.asset.extras = extras
    gltf.save(str(glb_path))


def png_corner_text() -> str:
    """Returns the watermark text downstream PNG/SVG/PDF writers should embed."""
    return ATTRIBUTION

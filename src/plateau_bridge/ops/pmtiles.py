"""PMTiles writer (shells out to tippecanoe).

The 2D footprint layer + scalar attributes is exactly what tippecanoe is
designed to produce. We deliberately do not embed building_uid → attrs as a
KV store here: that's what ``style/<tile>.arrow`` is for. See architecture doc.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)


# Properties to keep in PMTiles.
#
# We include the full set of per-hazard fields — covered, confidence, depth /
# in_zone, AND both source_id lists — because downstream renderers depend on
# them at render time:
#
#   - `*_coverage_confidence` distinguishes trusted coverage from "unknown".
#     Without it, a covered building can't be promoted to a depth band — it
#     has to fall back to a low-confidence grey, which is the opposite of the
#     honesty rule the consumers (e.g. plateau-risk-lens) are designed around.
#
#   - `*_coverage_source_ids` / `*_hit_source_ids` populate the citation
#     section of building property cards. Without them, the card shows
#     "(no source recorded)" for every building, defeating CC BY 4.0
#     compliance and the citable-by-design positioning.
#
# Compressed PMTiles still fits comfortably under common hosting size limits
# even with these added (~13 MB → ~20 MB for Shibuya); the correctness win
# is much larger than the bandwidth cost.
DEFAULT_PMTILES_PROPERTIES: tuple[str, ...] = (
    "building_uid",
    "year_built",
    "structure",
    "usage",
    "floors_above",
    "height",
    # river_flood
    "river_flood_covered",
    "river_flood_coverage_confidence",
    "river_flood_coverage_source_ids",
    "river_flood_depth_max",
    "river_flood_hit_source_ids",
    # inland_flood
    "inland_flood_covered",
    "inland_flood_coverage_confidence",
    "inland_flood_coverage_source_ids",
    "inland_flood_depth_max",
    "inland_flood_hit_source_ids",
    # tsunami
    "tsunami_covered",
    "tsunami_coverage_confidence",
    "tsunami_coverage_source_ids",
    "tsunami_depth_max",
    "tsunami_hit_source_ids",
    # storm_surge
    "storm_surge_covered",
    "storm_surge_coverage_confidence",
    "storm_surge_coverage_source_ids",
    "storm_surge_depth_max",
    "storm_surge_hit_source_ids",
    # landslide
    "landslide_covered",
    "landslide_coverage_confidence",
    "landslide_coverage_source_ids",
    "landslide_in_zone",
    "landslide_hit_source_ids",
)


def write_pmtiles(
    geojson_path: Path,
    pmtiles_path: Path,
    *,
    tippecanoe_bin: str = "tippecanoe",
    layer_name: str = "buildings",
    min_zoom: int = 10,
    max_zoom: int = 16,
    properties: tuple[str, ...] = DEFAULT_PMTILES_PROPERTIES,
) -> Path:
    exe = shutil.which(tippecanoe_bin)
    if not exe:
        raise RuntimeError(
            f"{tippecanoe_bin!r} not found on $PATH — install from "
            "https://github.com/felt/tippecanoe"
        )
    pmtiles_path.parent.mkdir(parents=True, exist_ok=True)
    include_args: list[str] = []
    for p in properties:
        include_args += ["-y", p]
    cmd = [
        exe,
        "-o", str(pmtiles_path),
        "-l", layer_name,
        "-Z", str(min_zoom),
        "-z", str(max_zoom),
        "--force",
        "--no-feature-limit",
        "--no-tile-size-limit",
        *include_args,
        str(geojson_path),
    ]
    log.info("running tippecanoe: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)
    return pmtiles_path

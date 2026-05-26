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


# Properties to keep in PMTiles. MVT field width adds up fast, so we omit
# *_coverage_source_ids and *_hit_source_ids in PMTiles — they live in parquet.
DEFAULT_PMTILES_PROPERTIES: tuple[str, ...] = (
    "building_uid",
    "year_built",
    "structure",
    "usage",
    "floors_above",
    "height",
    "river_flood_covered",
    "river_flood_depth_max",
    "inland_flood_covered",
    "inland_flood_depth_max",
    "tsunami_covered",
    "tsunami_depth_max",
    "storm_surge_covered",
    "storm_surge_depth_max",
    "landslide_covered",
    "landslide_in_zone",
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

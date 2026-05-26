"""Full-precision per-ward FlatGeobuf writer.

PMTiles geometry is tile-clipped and zoom-simplified — usable for rendering,
unusable for export. A typical use case: a downstream viewer lets users
draw a bbox and download the contained buildings; that path reads from
these FGBs, not PMTiles.

Sharding:

* **Single ward** (Tokyo 23 区 + small cities): one ``{city_code}.fgb``
* **政令指定都市** (Osaka 24 区 / Yokohama 18 区 / …): per-ward
  ``{city_code}_{ward_code}.fgb`` using bundled ward polygons. The Risk
  Lens bbox query only loads the wards that intersect the bbox — saves
  pulling the whole city for a 1 km² window.

Plan §Web 衍生品 quote:
    全精度 FlatGeobuf: 未瓦片化、未简化、unique features;
    给 bbox 导出 + 服务端分析用 (PMTiles 几何按瓦片裁剪/简化/重复,
    不能用于精确导出)
"""

from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd

log = logging.getLogger(__name__)


def write_per_ward_fgb(
    gdf: gpd.GeoDataFrame,
    out_dir: Path,
    *,
    city_code: str,
    wards: gpd.GeoDataFrame | None = None,
) -> list[Path]:
    """Partition ``gdf`` by ward and emit ``{city_code}_{ward_code}.fgb`` files.

    If ``wards`` is None, emits a single ``{city_code}.fgb`` covering the
    whole input. Otherwise spatially partitions buildings against the ward
    polygons (centroid-in-polygon for speed; precision tradeoff identical to
    the one in ``ops.intersect``).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    if wards is None or wards.empty:
        path = out_dir / f"{city_code}.fgb"
        gdf.to_file(path, driver="FlatGeobuf")
        log.info("wrote single %s (%d rows)", path.name, len(gdf))
        return [path]

    # Per-ward partition by centroid containment.
    centroids = gdf.copy()
    centroids["geometry"] = centroids.geometry.representative_point()
    joined = gpd.sjoin(
        centroids[["geometry"]].reset_index(),
        wards[["geometry", "city_code"]].rename(columns={"city_code": "ward_code"}),
        how="left",
        predicate="within",
    )
    # joined: original_index → ward_code (might be NaN for buildings just
    # outside any ward polygon)
    joined = joined.drop_duplicates(subset="index", keep="first").set_index("index")
    buildings_with_ward = gdf.copy()
    buildings_with_ward["__ward_code"] = joined["ward_code"].reindex(gdf.index)

    total_assigned = 0
    for ward_code, sub in buildings_with_ward.groupby("__ward_code"):
        if not isinstance(ward_code, str) or not ward_code:
            continue
        path = out_dir / f"{city_code}_{ward_code}.fgb"
        sub.drop(columns="__ward_code").to_file(path, driver="FlatGeobuf")
        written.append(path)
        total_assigned += len(sub)

    # Buildings whose centroid landed outside every ward polygon (rare; on
    # admin-boundary corners). Ship them as a leftover shard rather than
    # silently dropping.
    leftover = buildings_with_ward[buildings_with_ward["__ward_code"].isna()]
    if len(leftover):
        path = out_dir / f"{city_code}_unassigned.fgb"
        leftover.drop(columns="__ward_code").to_file(path, driver="FlatGeobuf")
        written.append(path)
        log.warning("%d buildings had no matching ward — wrote %s", len(leftover), path.name)

    log.info(
        "wrote %d ward FGB shards (%d buildings assigned, %d unassigned)",
        len(written), total_assigned, len(leftover),
    )
    return written

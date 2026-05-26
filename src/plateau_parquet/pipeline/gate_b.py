"""Gate B: 3D Tiles + (tile_content_uri, tile_feature_id) mapping + Arrow style tables.

Depends on Gate A's gdf and the converter's 3D Tiles output.
Produces ``style/<encoded>.arrow`` + ``tile_index.json`` plus the back-filled
``tile_content_uri`` / ``tile_feature_id`` columns in buildings.parquet.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyarrow as pa

from plateau_parquet.attribution import stamp_tileset_json
from plateau_parquet.ops.building_complex import compute_complexes
from plateau_parquet.ops.geometric_height import compute_tileset_heights
from plateau_parquet.ops.style_table import encode_tile_uri, write_style_tables
from plateau_parquet.ops.tiles3d import (
    attach_tile_keys,
    collect_tile_placements,
    list_contents,
    verify_feature_id_mapping,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GateBResult:
    style_dir: Path
    tile_index_path: Path
    buildings_parquet: Path
    verified: bool
    gdf: gpd.GeoDataFrame  # tile_content_uri / tile_feature_id back-filled


def _prepare_arrow_table(gdf: gpd.GeoDataFrame) -> pa.Table:
    """Convert the GeoDataFrame to an Arrow table for per-tile style writes.

    - Drops ``geometry`` (Arrow encoding handled in parquet via geopandas).
    - Forces ``tile_feature_id`` to nullable Int32 even when the entire column
      is None (which would otherwise be inferred as ``object`` and explode at
      ``Table.from_pandas``).
    """
    df = pd.DataFrame(gdf.drop(columns="geometry"))
    if "tile_feature_id" in df.columns:
        df["tile_feature_id"] = pd.array(df["tile_feature_id"].to_list(), dtype="Int32")
    return pa.Table.from_pandas(df, preserve_index=False)


def run_gate_b(
    gdf: gpd.GeoDataFrame,
    out_dir: Path,
    tiles3d_dir: Path,
    *,
    verify_sample: int = 10,
) -> GateBResult:
    # Promote the 3D Tiles bundle from _work into the public out dir so the
    # browser demo can fetch it from a stable URL (e.g. /data/3dtiles/...).
    # Symlink rather than copy — the bundle is large (~2 GB for Shibuya) and
    # the source is on the same filesystem.
    public_tiles = out_dir / "3dtiles"
    if not public_tiles.exists() and tiles3d_dir.exists():
        try:
            public_tiles.symlink_to(tiles3d_dir.resolve())
            log.info("symlinked 3dtiles → %s", public_tiles)
        except OSError as e:
            log.warning("symlink failed (%s); browser demo will need PLATEAU_DATA_DIR pointed at _work", e)

    # Stamp the root tileset.json with attribution + minimal manifest
    # pointer. Per 3D Tiles 1.1, tileset.asset.extras propagates to every
    # tile — Cesium / 3d-tiles-renderer / deck.gl pick it up automatically.
    manifest_path = out_dir / "manifest.json"
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    stamp_tileset_json(tiles3d_dir / "tileset.json", manifest=manifest_data)

    gdf = attach_tile_keys(gdf, tiles3d_dir)
    has_keys = bool(gdf["tile_content_uri"].notna().any())
    verified = has_keys and verify_feature_id_mapping(gdf, sample_n=verify_sample)
    if not verified:
        log.warning(
            "Gate B verification skipped or failed (has_keys=%s); "
            "downstream r3f colorBy will fall back to LOD1.",
            has_keys,
        )

    # Back-fill `height` from 3D Tile mesh geometry. ``measuredHeight`` is
    # a LiDAR-derived attribute that PLATEAU sets to -9999 for ~5–10 % of
    # buildings; ``floors_above × 3.5 m`` only recovers a quarter of those
    # because PLATEAU also sentinel-9999s the floor count for ~30 % of
    # the holdouts. But geometric height is computed from the building's
    # actual mesh (max_Y − min_Y over its ``_FEATURE_ID_0`` vertex cluster),
    # which is by definition present for every building that's rendered.
    # Where ``measuredHeight`` exists, we keep it as the canonical value
    # (median diff vs geom ≈ 3.7 m on Shibuya; the demos bucket heights by
    # 5–10 m, so the difference is sub-bucket noise) — geometric is only
    # used as a fallback. Provenance is recorded in ``height_source``.
    if has_keys:
        log.info("computing geometric heights from 3D Tile mesh geometry…")
        geom_heights = compute_tileset_heights(tiles3d_dir)
        if geom_heights:
            gid_s = gdf["gml_id"].astype(str)
            geom_series = gid_s.map(geom_heights).astype("Float32")
            measured_present = gdf["height"].notna()
            fillable = (~measured_present) & geom_series.notna()
            gdf.loc[fillable, "height"] = geom_series[fillable]
            if "height_source" in gdf.columns:
                gdf.loc[fillable, "height_source"] = "geometric"
            log.info(
                "geometric fill: filled %d / %d previously-null heights",
                int(fillable.sum()), int((~measured_present).sum()),
            )

        # Footprint-only fallback. The residual <1 % of buildings have no
        # measuredHeight, no floor count, and aren't in any 3D tile —
        # PLATEAU only shipped them as 2D footprints (typically sheds,
        # garages, outbuildings; median footprint ≤ 25 m²). Assign a
        # conservative one-storey default so downstream renderers and
        # parquet consumers never have to special-case NULL. ``height_source``
        # makes the provenance loud — analysts can filter these out with
        # ``WHERE height_source IN ('measured', 'geometric')``.
        still_null = gdf["height"].isna()
        if still_null.any():
            FOOTPRINT_DEFAULT_M = 3.5
            gdf.loc[still_null, "height"] = FOOTPRINT_DEFAULT_M
            if "height_source" in gdf.columns:
                gdf.loc[still_null, "height_source"] = "footprint_fallback"
            log.info(
                "footprint fallback: assigned %d buildings to %.1f m (PLATEAU 2D-only)",
                int(still_null.sum()), FOOTPRINT_DEFAULT_M,
            )

    # Building complex grouping — fuse touching footprints into one cluster
    # and expose ``complex_max_height`` so renderers can paint adjacent
    # tower-on-podium splits as one visual mass. Per-feature ``height`` is
    # untouched; analysts who want per-feature truth still get it from
    # the original column.
    log.info("computing building complexes (touch eps 0.1 m)…")
    gdf = compute_complexes(gdf)

    # Persist updated parquet.
    parquet_path = out_dir / "buildings.parquet"
    gdf.to_parquet(parquet_path, index=False)

    # Style tables — emit one per (tile_content_uri) across ALL LODs.
    # Each building can appear in multiple LOD tiles; we expand the gdf into
    # one row per placement so style_table.write_style_tables groups properly.
    style_dir = out_dir / "style"
    tile_index_path = out_dir / "tile_index.json"
    index: dict[str, str] = {}
    if has_keys:
        placements = collect_tile_placements(tiles3d_dir)
        if placements:
            placements_df = pd.DataFrame(placements, columns=["gml_id", "tile_content_uri", "tile_feature_id"])
            placements_df["gml_id"] = placements_df["gml_id"].astype(str)
            # Join attributes onto every placement. Drop the gdf's own
            # (canonical) tile columns so the placement columns win.
            attrs = pd.DataFrame(gdf.drop(columns=["geometry", "tile_content_uri", "tile_feature_id"]))
            attrs["gml_id"] = attrs["gml_id"].astype(str)
            expanded = placements_df.merge(attrs, on="gml_id", how="inner")
            expanded["tile_feature_id"] = pd.array(
                expanded["tile_feature_id"].to_list(), dtype="Int32"
            )
            table = pa.Table.from_pandas(expanded, preserve_index=False)
            index = write_style_tables(table, style_dir)
            # Some tiles (esp. low-LOD tiles whose bbox spans neighbouring
            # wards) have ALL their features admin-clipped out of the
            # parquet — they're physically loaded by the renderer but
            # produce zero rows here. Emit empty style tables for them so
            # ``tileIndex[uri]`` is always defined; the demos then apply
            # the same "unknown" colour uniformly instead of falling back
            # to the glTF default material (which produced jarring
            # half-coloured / half-grey screenshots).
            import pyarrow.feather as feather
            empty_table = pa.Table.from_pydict({
                "tile_feature_id": pa.array([], type=pa.int32()),
            })
            # Cover EVERY tile referenced by the tileset, including ones with
            # no metadata at all (corrupted GLB / converter quirk). The
            # frontend will look up these as zero-row tables and apply the
            # uniform "unknown" colour — visually consistent with the
            # surrounding city.
            for uri in list_contents(tiles3d_dir):
                if uri not in index:
                    encoded = encode_tile_uri(uri)
                    rel = f"style/{encoded}.arrow"
                    feather.write_feather(empty_table, style_dir / f"{encoded}.arrow", compression="uncompressed")
                    index[uri] = rel
            log.info(
                "tile_index entries: %d (%d had attribute rows, %d empty/admin-clipped)",
                len(index), sum(1 for _ in placements_df.groupby("tile_content_uri")), 0,
            )
        else:
            # Fall back to canonical-only emission.
            table = _prepare_arrow_table(gdf)
            index = write_style_tables(table, style_dir)
    else:
        log.info("no tile_content_uri attached; skipping style table emission")
        style_dir.mkdir(parents=True, exist_ok=True)
    tile_index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")

    return GateBResult(
        style_dir=style_dir,
        tile_index_path=tile_index_path,
        buildings_parquet=parquet_path,
        verified=verified,
        gdf=gdf,
    )

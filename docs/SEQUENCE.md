# Pipeline sequence diagram

End-to-end flow for `plateau build CITY --gates ABC`. All stages are idempotent
(cached `_work/` dirs are reused on re-runs).

```mermaid
sequenceDiagram
    participant User
    participant CLI as plateau (Typer)
    participant Catalog
    participant Download as sources.download
    participant Admin as plateau_parquet.admin
    participant Nusamai as nusamai (CLI)
    participant LoadGJ as sources.citygml.load_geojson
    participant Norm as ops.attributes.normalise
    participant Clip
    participant Cov as ops.intersect.apply_coverage
    participant Haz as ops.intersect.apply_hazards
    participant T3D as ops.tiles3d.attach_tile_keys
    participant Style as ops.style_table
    participant Tippy as tippecanoe / nusamai pmtiles
    participant Manifest

    User->>CLI: plateau build 13113 --gates ABC
    CLI->>Catalog: get_catalog("13113")
    Catalog-->>CLI: bldg + urf + fld + lsld entries

    Note over CLI,Nusamai: Gate A — buildings.parquet
    CLI->>Download: fetch_and_unzip(bldg.url)
    Download-->>CLI: cached CityGML/
    CLI->>Admin: load_admin("13113")
    Admin-->>CLI: 渋谷区 polygon
    CLI->>Nusamai: --sink geojson <udx/bldg/*.gml>
    Nusamai-->>CLI: Building.geojson
    CLI->>LoadGJ: read_file, force 2D, restore gml_id
    LoadGJ-->>CLI: 90,299 buildings GeoDataFrame
    CLI->>Clip: gdf[geometry.intersects(admin_union)]
    Clip-->>CLI: 41,858 渋谷区 buildings
    CLI->>Norm: extract yearOfConstruction, measuredHeight, ...
    Norm-->>CLI: enriched gdf
    loop for each hazard kind
        CLI->>Nusamai: --sink geojson <udx/<kind>/*.gml>
        Nusamai-->>CLI: <FeatureClass>.geojson (bbox-prefiltered)
    end
    CLI->>Cov: apply_coverage(gdf, extents, centroid_mode=True)
    Cov-->>CLI: *_covered, *_coverage_confidence
    CLI->>Haz: apply_hazards(gdf, layers, centroid_mode=True)
    Haz-->>CLI: *_depth_max, *_in_zone, *_hit_source_ids
    CLI->>Manifest: build_manifest(notes incl. admin provenance)
    CLI-->>User: → out/buildings.parquet + manifest.json

    Note over CLI,Style: Gate B — 3D Tiles + style tables
    CLI->>Nusamai: --sink 3dtiles
    Nusamai-->>CLI: 3dtiles/<z>/<x>/<y>_bldg_Building.glb
    CLI->>T3D: attach_tile_keys(gdf, tiles3d_dir)
    T3D->>T3D: walk tileset.json, read EXT_structural_metadata
    T3D-->>CLI: tile_content_uri + tile_feature_id per building
    CLI->>Style: write_style_tables(table, out_dir/style/)
    Style-->>CLI: 875 × Arrow IPC files
    CLI->>CLI: symlink out/3dtiles → _work/bldg/3dtiles

    Note over CLI,Tippy: Gate C — PMTiles + FGB + zoning
    CLI->>Nusamai: --sink geojson <udx/urf/*.gml>
    Nusamai-->>CLI: UseDistrict.geojson
    CLI->>CLI: spatial join centroids → zoning_use, far_max
    alt tippecanoe installed
        CLI->>Tippy: tippecanoe -y building_uid -y height -y *_covered ...
        Tippy-->>CLI: enriched buildings.pmtiles
    else fallback
        CLI->>Nusamai: --sink pmtiles
        Nusamai-->>CLI: geometry-only buildings.pmtiles
    end
    CLI->>CLI: write per-ward FlatGeobuf
    CLI-->>User: → 5 artifact types ready
```

## Reading the diagram

- **Idempotence**: every `Nusamai` call short-circuits if the target file
  already exists. Re-running ABC after a Gate-A-only build is fast.
- **Centroid mode**: `apply_coverage` and `apply_hazards` default to
  representative-point sjoin (`within` predicate), avoiding the O(N · M)
  polygon-vs-polygon cost on huge cities. See `docs/PERFORMANCE.md`.
- **Honesty invariant**: `apply_coverage` runs *before* `apply_hazards`.
  Buildings outside coverage extent can't acquire a depth value even if
  they happen to sit inside an inundation polygon — encoded as a hard
  filter in the `apply_hazards` loop.
- **Provenance**: `Manifest` records the admin polygon source and the
  CKAN dataset id; downstream consumers reading the parquet can also
  read the manifest to know exactly which PLATEAU release the build came
  from.

## Branching points

| If… | …then |
|---|---|
| User passes `--no-hazards` | `Cov` + `Haz` blocks skipped; parquet ships with `*_covered = false` everywhere |
| User then runs `plateau hazard CITY` | Existing parquet loaded, `Cov` + `Haz` injected in place |
| nusamai's geojson sink emits unexpected feature classes | gate_a logs warning, falls back to first matching `*.geojson` |
| Admin polygon for city not in bundled `japan_admin.geojson` | `load_admin()` returns None, `declared_full_admin` ↓ unknown |
| Hazard sub-bundle missing (e.g. Osaka lsld) | gate_a tolerates and marks unknown |

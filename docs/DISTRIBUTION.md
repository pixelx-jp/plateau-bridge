# Pre-built bundle distribution

End users can skip the build pipeline entirely:

```bash
plateau cache add 13113     # downloads ~36 MB instead of 636 MB + nusamai run
```

This page explains the format, how maintainers add a city to the mirror,
and the trust model.

## What's in a bundle

One zstd-compressed tarball per city/year:

```
plateau-<city>-<year>-v1.tar.zst
├── buildings.parquet        # canonical GeoParquet
├── manifest.json            # provenance + coverage stats
├── tile_index.json          # 3D Tiles → style table lookup
├── style/<encoded>.arrow ×N # per-tile attribute partitions (uncompressed)
├── buildings.pmtiles        # browser 2D vector tiles
└── buildings/<city>.fgb     # full-precision FlatGeobuf
```

**3D Tiles are NOT bundled.** They're 2–10 GB per city and easy to
regenerate from the parquet (run `plateau build CITY --gates B` with the
catalog URL). Heavy users with nusamai installed rebuild locally.

Compression: Shibuya 251 MB raw → **36 MB zstd** (14 % ratio).

## Cache index

A single JSON file lists every available bundle:

```json
{
  "schema": 1,
  "updated": "2026-05-24T05:05:20Z",
  "cities": [
    {
      "city_code": "13113",
      "city_name": "Shibuya-ku",
      "dataset_year": 2023,
      "bundle_url": "https://github.com/pixelx-jp/plateau-bridge/releases/download/data-v1/plateau-13113-2023-v1.tar.zst",
      "sha256": "dbe43d4c1a6dd5092a671b2cc06c2a8f7f860f3f01285d973db2e06899005bde",
      "bytes": 37709760,
      "n_buildings": 41858,
      "tool_version": "0.1.0"
    }
  ]
}
```

Default index URL is hard-coded to this repo's `main` branch:

```python
DEFAULT_INDEX_URL = "https://raw.githubusercontent.com/pixelx-jp/plateau-bridge/main/distribution/index.json"
```

Override per-invocation: `plateau cache add 13113 --index https://...`

## Trust model

Every download is **sha256-verified** against the index entry before
extraction. A compromised mirror or in-flight corruption fails closed
(the bundle is deleted and an exception raises). The index itself is
served over HTTPS from the repo's `main` branch; tampering with it would
require commit access to the repository.

The bundle does NOT need to be signed beyond this — the index is the
trust root, and the index lives in version control.

## Adding a city as a maintainer

```bash
# 1. Build it.
plateau build 14100 --out ./out_yokohama

# 2. Pack + push to a draft GitHub release.
gh release create data-v1 --title "plateau-bridge bundles v1" --notes "Pre-built city data"   # one-time
plateau cache push ./out_yokohama --backend github-releases --tag data-v1

# 3. Commit the updated index.
git add distribution/index.json && git commit -m "data: add 14100"
git push
```

`plateau cache push` does three things:

1. Pack the `out_<city>/` tree into a zstd tarball.
2. `gh release upload data-v1 <tarball>` (the gh CLI must be authenticated
   for the current repo).
3. Merge the new entry into `distribution/index.json` (replacing any
   older row for the same `city_code` + `dataset_year`).

## Local testing without a remote

Use `--backend local` + `--dry-run`:

```bash
plateau cache push ./out --backend local --dry-run
# → distribution/plateau-13113-2023-v1.tar.zst
# → distribution/index.json with file:// URLs

plateau cache add 13113 --index file://$(pwd)/distribution/index.json --out /tmp/test
```

Both `file://` and `https://` work — the same code path. The roundtrip
test in this doc takes about a second on a laptop.

## What about R2 / S3 / Cloudflare?

The bundle format and index schema are mirror-agnostic. To use R2:

1. Upload bundles via `rclone copy` or `aws s3 cp` instead of `gh release upload`
2. Hand-edit `distribution/index.json` to point `bundle_url` at the R2 public URL
3. Commit + push the index

The `plateau cache add` end of the contract doesn't care — it just
follows `bundle_url`, verifies sha256, extracts. Future PRs welcome to
add native `--backend r2` / `--backend s3` push commands.

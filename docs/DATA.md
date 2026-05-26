# Data distribution

How `plateau-parquet` ships data, why, and what it costs.

## TL;DR

You don't need to build anything to use plateau-parquet. Pre-built city
bundles ship via GitHub Releases. One command:

```bash
plateau cache add shibuya
```

…downloads a 14 %-compressed bundle (~36 MB for Shibuya, ~250 MB for
Yokohama), verifies sha256, extracts to `./out_<slug>/`, and you're
ready to query.

If you want to build from PLATEAU's CityGML source yourself — for a
specific `dataset_year`, for a city not yet in our catalog, or because
you don't want to trust pre-built artifacts — `plateau build <city>`
does the full pipeline. That path needs `nusamai` + `tippecanoe` and
takes ~5–30 minutes per city.

## Two flows, by user

```
                ┌─────────────────────────────────────┐
                │       PLATEAU CKAN / nusamai        │
                │      (source CityGML, ~5 GB/city)   │
                └────────────────┬────────────────────┘
                                 │
              ┌──────────────────┴──────────────────┐
              │ (we run this once, per dataset_year)│
              │       plateau build <city>          │
              │       gate A → B → C                │
              └──────────────────┬──────────────────┘
                                 │ artifacts: parquet, pmtiles,
                                 │ 3D Tiles, style/*.arrow, …
                                 ▼
              ┌─────────────────────────────────────┐
              │       plateau cache push            │
              │  → tar.zst + sha256 + index.json    │
              │  → GitHub Releases (free CDN)       │
              └──────────────────┬──────────────────┘
                                 │ ~36 MB – 250 MB / city
              ┌──────────────────┴──────────────────┐
              │       plateau cache add <city>      │  ← end user runs this
              │  fetches, sha256-verifies, extracts │
              └─────────────────────────────────────┘
```

The contract: `cache push` and `cache add` are decoupled. Anyone can
host a mirror by pointing `--index` at their own `index.json`; the
official mirror is just the default.

## Distribution policy

`plateau-parquet` redistributes data derived from PLATEAU. The license
chain:

- **PLATEAU source CityGML**: CC BY 4.0, MLIT (Ministry of Land,
  Infrastructure, Transport and Tourism)
- **Our derived artifacts**: same CC BY 4.0, attribution
  auto-embedded into every output (PNG corner watermark, GLB
  `asset.extras.attribution`, mp4 tail card, etc.)
- **Code (pipeline + CLI + demos)**: MIT, © PixelX Inc. (Yodo Labs)

If you fuse our parquet with OSM at runtime, ODbL attribution is
added automatically. We do **not** redistribute OSM-fused parquet.

## Hosting choice and why

| Backend            | Storage cost | Egress cost | File size cap | Total cap | Where we land |
| ---                | ---:         | ---:        | ---:          | ---:      | ---:          |
| **GitHub Releases**| **$0**       | **$0**      | 2 GB / file   | 100 GB / release | ✓ our default |
| Cloudflare R2      | $0.015 / GB / mo | $0      | none          | pay-as-go | back-up plan  |
| AWS S3 + CloudFront| $0.023 / GB / mo | $0.09 / GB | none      | pay-as-go | avoided       |
| Self-hosted VPS    | $5–10 / mo   | metered     | none          | metered   | not worth it  |

29 cities at ~25 GB compressed sits comfortably inside one GitHub
Release (100 GB cap) and within file-size limit (largest single bundle
~250 MB). Public OSS repos get unlimited bandwidth via Fastly's CDN,
which GitHub fronts.

Result: **$0 / month** for the project's foreseeable scale. If we
ever exceed GitHub's per-release cap (we'd need ~400 cities at
current sizes), the fallback is Cloudflare R2 at ~$0.40 / month for
the full set — the `index.json` mirror swap is one URL change and
breaks nothing in the client.

## What's in a bundle

Each `plateau-<city>-<year>.tar.zst` decompresses to:

```
out_<slug>/
├── buildings.parquet       # GeoParquet, server-side queries
├── buildings.pmtiles       # MapLibre 2D tiles
├── manifest.json           # provenance, coverage stats, sha256s
├── tile_index.json         # tile_content_uri → style file map
├── style/<encoded>.arrow   # per-tile attribute side table
└── buildings/<ward>.fgb    # full-precision bbox export (per ward)
```

Bundle sizes are **40 MB – 300 MB compressed** per city. The full
catalog (29 cities) totals ~3 GB.

### Why no `3dtiles/` in the cache bundle?

The visual 3D Tiles geometry for a single city is 1.8–18 GB on disk.
Zstd compresses GLB poorly (binary already), so Yokohama's 18 GB
would still exceed GitHub Releases' 2 GB per-file limit even after
compression. Cache bundles ship the **analytics + 2D-map subset**;
to view a city in the 3D browser demos, run the full build:

```bash
plateau build shibuya --prune-cache
```

`plateau build` produces the same parquet + pmtiles as `cache add`
*plus* the 3D Tiles, locally on your machine. ~5–30 min depending on
city size. See "Building from source" below.

### Future work

A separate per-city "tiles" bundle (split into < 2 GB chunks where
needed) would let `cache add --with-tiles` deliver the full 3D
experience. Open an issue if this matters for your use case.

## Versioning

Releases are tagged `data-vN`. Bundles include `dataset_year` in
their filename and inside `manifest.json`. A given `cache add` call
downloads the latest tagged bundle for the requested city — if you
need an older year, pass `--index` pointing at the specific release's
`index.json`.

We re-publish:

- **Yearly** when PLATEAU updates the city's dataset (typically Q1
  for the previous fiscal year's data).
- **Out-of-cycle** when we ship a pipeline bug fix that materially
  changes the output (e.g. the recent uro 3.1 schema sanitiser for
  Toshima/Kita/Itabashi; the `make_valid` admin polygon fix for
  Edogawa). These also bump the bundle's `manifest.json#pipeline_version`.

Old releases stay accessible. We do not delete published bundles.

## Mirroring (for downstream / air-gapped use)

If GitHub is blocked or you want a private mirror:

```bash
# Pull every bundle from the official index into your S3 / R2 / disk:
plateau cache mirror --to s3://your-bucket/plateau-parquet/

# Re-publish your own index pointing at the new location:
plateau cache index --from s3://your-bucket/plateau-parquet/ \
                    --out your-index.json

# Downstream consumers:
plateau cache add shibuya --index https://your-cdn/index.json
```

(`plateau cache mirror` and `plateau cache index` exist as scaffold;
the S3/R2 backends are stubbed pending real demand — open an issue
if you need them prioritised.)

## Building from source instead

You don't have to use pre-built bundles. The full pipeline is one
command:

```bash
plateau build shibuya --prune-cache
```

This downloads PLATEAU's CityGML zip (~3–10 GB per city), runs nusamai
+ tippecanoe, and produces the same `out_<slug>/` directory locally.
Requirements:

- [`nusamai`](https://github.com/MIERUNE/plateau-gis-converter) on
  `$PATH` — the Rust binary that parses i-UR extensions
- [`tippecanoe`](https://github.com/felt/tippecanoe) on `$PATH` —
  produces the PMTiles output

`--prune-cache` deletes the unzipped intermediate after the build
completes; without it, the on-disk footprint grows ~10 GB per city.

## Why we publish data at all

The alternative is "users build from source every time", which:

- requires installing nusamai + tippecanoe (~150 MB combined)
- takes 5–30 minutes per city
- requires ~10 GB of free disk per city
- needs a working internet connection to PLATEAU's CKAN

For an audience whose first encounter is "I want to query Tokyo
buildings in DuckDB", that's too many barriers. Pre-built bundles
turn the time-to-first-query into ~10 seconds.

The trade-off is that we have to actually run a release pipeline. The
code is wired up (`plateau cache push`, `release.yml` workflow); the
ongoing maintenance is ~30 min per dataset_year per city — most of
which is `plateau build` runtime, not human time.

## Open questions

- **Should we shard by region?** Currently one release contains all
  29 cities. If we grow to >50, splitting `data-tokyo-v1`,
  `data-kansai-v1`, etc. lets users mirror only what they need.
- **CDN-side aggregation?** A `plateau cache add tokyo-23-wards`
  one-shot would pull all 23 special-ward bundles at once. The
  index format supports group keys — the CLI just doesn't expose
  them yet.

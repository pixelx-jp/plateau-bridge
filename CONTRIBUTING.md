# Contributing

Thanks for taking the time. plateau-parquet is small enough that you can read the
whole codebase in an afternoon — please do, then pick from below.

plateau-parquet is built and maintained by **[Yodo Labs](https://yodolabs.jp)**
(PixelX Inc.). For non-trivial work — anything that touches gate orchestration,
the data contract, or the catalog registry — open an issue first so we can
align on direction before you spend the time.

## High-leverage starting points

### 1. Add a city to the catalog

`src/plateau_parquet/catalog_registry.json` is the registry. Add an entry, run

```bash
plateau build <code> --gates A --skip-3dtiles --no-hazards
```

and open a PR with the resulting `manifest.json` attached. **Detailed
walkthrough:** [docs/ADDING_A_CITY.md](docs/ADDING_A_CITY.md).
We accept any city with a published PLATEAU bldg dataset.

### 2. Add a row to `coverage_sources.json` (no code needed)

The biggest gap in our honest-hazard story is that all 61 catalog
hazard entries resolve to `declared_full_admin` instead of the more
precise `explicit_polygon`. The infrastructure to upgrade them is
already in tree (`src/plateau_parquet/sources/coverage_ksj.py`); what's
missing is the **mapping table** from PLATEAU source-document names
to MLIT KSJ download URLs.

**Adding a row immediately upgrades every matching city** with no
code change. Each row takes ~5–10 min:

1. Pick a watershed from the priority list in
   [docs/COVERAGE_ROADMAP.md](docs/COVERAGE_ROADMAP.md#phase-2--mapping-table-population-ongoing-community-driven)
   — Tokyo's 利根川 / 荒川 / 多摩川 / 鶴見川 are the highest-impact
   targets.
2. Find the canonical source name in any PLATEAU bundle's metadata
   XML (e.g. `metadata/udx_<city>_<year>_fld_op.xml`'s
   `<descriptiveKeywords>`).
3. Look up the matching KSJ URL on
   [nlftp.mlit.go.jp/ksj/](https://nlftp.mlit.go.jp/ksj/) (A31 for
   river flood, A40 tsunami, A48 landslide, A41 storm surge).
4. PR an entry to `src/plateau_parquet/data/coverage_sources.json`:

```jsonc
{
  "entries": {
    "利根川水系利根川洪水浸水想定区域図": {
      "hazard": "river_flood",
      "ksj_url": "https://nlftp.mlit.go.jp/ksj/.../A31-17_5339.zip",
      "published": "2017-07-20"
    }
  }
}
```

### 3. Custom deck.gl loader (bypass loaders.gl bug)

`examples/browser_deckgl/` runs in geometry-only mode because loaders.gl 4.x
can't parse PLATEAU's variable-length `gml_id` string property table
(`Not implemented - arrayOffsets for strings`). Writing a minimal
`Tiles3DLoader` subclass that skips the broken property-table parsing but
still exposes feature IDs would let the deck.gl demo do per-feature shading
like its sister demos. ~2–3 h estimated. See
[`examples/browser_deckgl/README.md`](examples/browser_deckgl/README.md)
for the current workaround and the three.js demo for the contract to match.

### 4. New CLI subcommands

`plateau diff`, `plateau verify`, `plateau bench`, `plateau hazard`, and
`plateau poster` are independent and each ~200 LOC. Same shape if you want
to add (say) `plateau export-osm`, `plateau zoning-summary`, etc.

## Local dev

```bash
pip install -e '.[dev,pmtiles]'
pytest          # unit tests, no network
ruff check src tests
```

You also need [`nusamai`](https://github.com/MIERUNE/plateau-gis-converter)
on `$PATH` for any test that touches `convert_buildings`. For PMTiles output
also install [`tippecanoe`](https://github.com/felt/tippecanoe).

## Pull requests

- One concern per PR. The smaller the PR, the faster it merges.
- New behaviour needs a test. Coverage-extent and honesty-invariant
  changes especially — those are the things this project's reputation
  rests on.
- We squash-merge; write a clean commit message.
- Don't bundle `LICENSE` / `README` edits with code changes.

## Issues

Bug reports are most useful with the relevant `manifest.json` attached. The
manifest tells us which datasets you used and what the coverage stats were.

For data-quality issues (a building that should be covered isn't, a hazard
depth looks wrong), include the `building_uid` and the city's `manifest.json`
— that pins enough provenance for us to reproduce locally.

## Contact

- General questions / issues: open a GitHub issue.
- Partnership inquiries, commercial use, or "we'd like to use this for X":
  [pan@yodolabs.jp](mailto:pan@yodolabs.jp).

## Code of conduct

Be kind. Be specific. Don't post issues without reading the existing ones.

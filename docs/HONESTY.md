# Honesty invariants

This is the philosophical core of plateau-parquet. Everything else can change —
file formats, CLI flags, rendering stacks — and the project survives. The
invariants below cannot break without making the data dishonest.

## 1. `covered=false` ≠ "no risk"

The single most important rule. When a hazard layer's `*_covered` column is
`false` for a building, that means **the survey did not look at this
location**, *not* that the building is safe.

> Concretely: 渋谷区 2023's bldg/fld coverage is 100%, so a building outside
> the inundation polygon has `river_flood_covered = true`, `river_flood_hit
> = false`, `river_flood_depth_max = NaN`. That's *surveyed safe*. A
> building in a city without an `fld` dataset has `river_flood_covered =
> false`, same NaN depth — that's *unknown*. These are different.

`apply_hazards` enforces this with a hard filter: it only intersects against
hazard polygons for buildings whose `*_covered` column is already `true`.
"Uncovered + hit" cannot exist.

Tests pinning the invariant:

- `tests/test_coverage_semantics.py::test_uncovered_building_is_unknown_even_when_intersects_inundation`
- `tests/test_verify.py::test_verify_catches_honesty_violation`
- `tests/test_centroid_mode.py::test_centroid_mode_matches_honesty_invariant`

## 2. Coverage extent must come from a published polygon or the data itself, never from synthesised boundaries

`sources/coverage.py` resolves a `CoverageExtent` in this order
(most → least trustworthy):

1. **explicit_polygon** — published 想定区域 / 調査範囲 polygon, either
   from the catalog's `coverage_extent_url` or via the KSJ
   auto-resolver in `coverage_sources.json`. Strongest claim.
2. **inundation_bounded** — when PLATEAU bundles per-building flood
   depth polygons (the `udx/fld/` data), we use the *union of those
   polygons themselves* as the extent. The literal truth of what the
   data tells us: inside the polygon = modelled with a depth value,
   outside = not modelled. **No buffering, no dilation.**
3. **declared_full_admin** — source metadata states full-admin
   coverage; intersect with the city's admin polygon. Used only when
   PLATEAU doesn't ship per-building depth polygons (catalog entry
   exists but the bundle lacks the underlying GIS).
4. **unknown** — none of the above → `*_covered = false`,
   `depth = null`. Downstream UIs MUST show grey, never green.

### What "reverse-engineering" means (and doesn't)

The rule "never reverse-engineer from inundation polygons" forbids
*synthesising* a boundary that doesn't exist in the source data —
typically by buffering / dilating:

```python
# ❌ FORBIDDEN — fabricates a "surveyed-safe" zone around floods
coverage_extent = union(inundation_polys).buffer(100)
```

That move conflates "near a known flood, surveyed-safe" with
"definitely not modelled". It's the canonical way to lie about
hazard coverage.

Using the inundation polygons **at their native boundary** with
**no dilation** is a different operation — it makes no synthetic
claim. The boundary IS where MLIT's model stops; we report exactly
that. That's `inundation_bounded` and it's strictly more honest than
`declared_full_admin` for any city where the underlying flood
projection covers less than the entire admin boundary (which is
nearly every Japanese city). See `docs/COVERAGE_ROADMAP.md` for the
discovery story.

### Why we don't just use `declared_full_admin` everywhere

MLIT does not survey every parcel in a city. They model flood
projections in flood-prone areas (along rivers / coast). A building
sitting uphill or far from water systems was simply not modelled —
it's "unknown safe" not "surveyed safe".

`declared_full_admin` claims "covered = true, depth = 0" for every
unmodelled building. For Tokyo's Suginami-ku that's ~63k buildings
fabricated as "surveyed and safe" when they were never investigated.
`inundation_bounded` correctly classifies them as `covered = false`.

The catalog format encodes the rule: `coverage_extent_url` is opt-in
and links to source-published data; `declared_full_admin` is a
boolean flag the human curator sets after reading the dataset's
metadata; `inundation_bounded` kicks in automatically when PLATEAU
bundles per-building flood polygons.

## 3. Centroid mode is a precision tradeoff, not a correctness compromise

`apply_coverage` / `apply_hazards` default to `centroid_mode=True`: each
building's representative point is tested against the hazard polygon with
`predicate="within"`, instead of the building's full footprint against
`intersects`.

This gives an ~120× speedup on Osaka (7 h 45 min → 3 min 45 s). The
precision cost is bounded by **half a building footprint width** (~5–10 m).
PLATEAU's hazard polygons are kilometre-scale and ~10 m-quantised upstream;
the loss is below the source data's positional accuracy.

Honest disclosure: a few edge cases differ between modes — a building whose
centroid sits just inside the polygon while its footprint mostly outside
will be `covered=true` in centroid mode but might be `covered=false` in
polygon mode. The reverse happens too. Both modes satisfy invariant #1.

`centroid_mode=False` is available for users who specifically need polygon-
vs-polygon precision against small hazard polygons. See
`docs/PERFORMANCE.md` for the wall-clock cost.

## 4. Provenance is recorded, never silent

Every `buildings.parquet` ships with a `manifest.json` carrying:

- The CKAN dataset id and PLATEAU year for every theme that touched the build
- The admin polygon's source (`Tokyo: © dataofjapan/land`, MLIT N03, …)
- `coverage_confidence_breakdown` per hazard
- A free-text `notes` array with the actual clipping ratio (e.g. "clipped
  from 90,299 → 41,858 buildings")
- The `plateau-parquet` version

`plateau verify` cross-checks the manifest against the parquet — every
hazard `*_coverage_source_ids` value referenced in the data must exist in
`manifest.sources`. Orphan source ids become a `warn` finding.

## 5. Honesty in the output stack

The honesty invariant must survive the output too. Concretely:

- **PMTiles** carry the hazard 4-tuples as scalar fields, so downstream 2D risk maps can
  surface "covered=false → grey" without re-running intersection.
- **Per-tile Arrow style tables** (Gate B) carry the same scalars.
- The **`coverage_confidence` enum** is in the PMTiles layer fields list so
  UIs can render `unknown` distinctly from `surveyed safe`.

## 6. What the project does *not* claim

- It does not claim hazard data is authoritative for evacuation planning.
  PLATEAU is the source; our job is to surface it faithfully.
- It does not claim cross-year temporal stability for `building_uid` — the
  versioned uid changes when `dataset_year` changes. A
  `canonical_building_id` (geometry-stable across years) is plan v2.
- It does not claim 100% attribute coverage. Real PLATEAU bldg datasets
  vary widely — e.g. Shibuya 2023 has 0% `yearOfConstruction` populated.
  The manifest's `field_coverage` surfaces this; downstream UIs must
  honour it.

## Reading the manifest

```bash
plateau verify out_osaka                  # human-readable health report
.venv/bin/python -c "
import json, pandas as pd
m = json.load(open('out_osaka/manifest.json'))
print('coverage_confidence per hazard:')
for cs in m['coverage_stats']:
    print(f'  {cs[\"kind\"]}: covered={cs[\"covered_count\"]:,}  hit={cs[\"hit_count\"]:,}  conf={dict(cs[\"coverage_confidence_breakdown\"])}')
"
```

The numbers should add up: covered + uncovered = total buildings, and
covered is partitioned by `coverage_confidence` into `explicit_polygon` /
`declared_full_admin` / `unknown` (the last is always 0 for covered rows
by construction).

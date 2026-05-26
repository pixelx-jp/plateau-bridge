# Hazard coverage extent — research & roadmap

This doc captures what we know about upgrading PLATEAU hazard datasets
from `declared_full_admin` → `explicit_polygon` coverage confidence,
and what's required to ship that upgrade.

`declared_full_admin` says: "the source claims to cover the entire
admin boundary, so any building inside that boundary is *surveyed*
(`covered = true`), and `depth = 0` legitimately means *safe*."
`explicit_polygon` says: "the source publishes a precise 想定区域 /
調査範囲 polygon — use it directly." Explicit is strictly more
honest where available: the survey extent rarely matches the admin
boundary exactly.

Current state (2026-05-25): **0 of 61** hazard entries in the bundled
registry use `explicit_polygon`. All 61 are `declared_full_admin`.

## What we checked, and what didn't pan out

### 1. Per-dataset metadata XML inside the PLATEAU bundle

Each PLATEAU CityGML bundle ships `metadata/udx_<city>_<year>_<theme>_op.xml`
for every theme. The `<extent><geographicElement><EX_GeographicBoundingBox>`
block looks promising, but:

- The bbox is **prefecture-level**, not per-city. For Suginami-ku
  (city centre ~139.65, 35.70), the bundled flood-metadata bbox is
  138.94–139.93, 35.50–35.90 — i.e. roughly all of Tokyo. Coarser
  than the admin boundary, so it would *downgrade* confidence, not
  upgrade.
- The `<extent>` is also a bbox, not a polygon — even if it were
  per-city, it wouldn't replicate the river/coast follow-curves of
  the actual 想定区域.

### 2. The hazard GML files themselves

`udx/fld/.../<id>_fld_<crs>_l2_op.gml` is the inundation depth
surface. Taking the union of inundation polygons + ε buffer would
look like an extent — but that's the project's single forbidden
operation (see [`HONESTY.md`](HONESTY.md)). A building outside the
flood polygon could still be *surveyed-and-safe*; we cannot recover
that distinction from the inundation surface.

### 3. The PLATEAU CKAN catalog page

PLATEAU datasets are catalogued at
`https://www.geospatial.jp/ckan/dataset/plateau-<city_code>-<slug>-<year>`.
The dataset's "resources" section sometimes lists supplementary files
beyond the main CityGML zip — but as of this check (2026-05),
**no PLATEAU dataset publishes a separate 想定区域 polygon resource**.
The polygon is referenced only in the bundle's metadata XML as a
free-text description (see below).

## The real source: MLIT KSJ (国土数値情報)

The 想定区域 polygons are published separately by MLIT as KSJ datasets:

| Hazard kind     | KSJ code      | Published as                             |
| ---             | ---           | ---                                      |
| river_flood     | **A31**       | 洪水浸水想定区域 (per-river-system polygons) |
| tsunami         | **A40**       | 津波浸水想定 (per-coastal-region polygons)   |
| landslide       | **A33** / A48 | 土砂災害警戒区域                         |
| inland_flood    | (none — internal flooding is municipal) | varies |
| storm_surge     | **A41**       | 高潮浸水想定区域                         |

URL pattern (example for A31):
`https://nlftp.mlit.go.jp/ksj/gml/data/A31/A31-{year}/<dataset>.zip`

License: KSJ ships under its own terms ("国土数値情報利用約款"). For
plateau-bridge's open-source distribution we need to confirm
redistribution permission per-KSJ-dataset, OR include only the
*reference URL* in the catalog and have users fetch directly.

## The cross-referencing problem

The PLATEAU bundle's metadata XML lists the *source documents* used.
Example from Suginami-ku 洪水:

> 利根川水系利根川洪水浸水想定区域図（平成29年7月20日）国土交通省関東地方整備局…
> 利根川水系江戸川洪水浸水想定区域図（平成29年7月20日）…
> 多摩川水系多摩川、浅川、大栗川洪水浸水想定区域図（平成28年5月30日）…
> 神田川流域浸水予想区域図 …
> (etc.)

Each line is a published 想定区域 with a release date. These ARE
the polygons we'd want — but cross-referencing them to KSJ datasets
requires a manually-curated mapping:

```
"利根川水系利根川洪水浸水想定区域図"   → KSJ A31, river_id "1080010001", 2017-07-20
"多摩川水系多摩川...洪水浸水想定区域図" → KSJ A31, river_id "1090010001", 2016-05-30
```

There's ~150 unique river systems in Japan with published 想定区域,
and many cities have 3–10 watersheds contributing. The mapping is
finite but non-trivial.

## Implementation status

### Phase 1 — Scaffolding ✅ shipped 2026-05-26

1. ✅ Metadata XML parser at
   `src/plateau_bridge/sources/metadata_xml.py` — extracts source-document
   list (free-text Japanese strings under `<descriptiveKeywords type=005>`)
   plus the JMP20 ISO-19115 bounding box and canonical title.
2. ✅ Mapping table at `src/plateau_bridge/data/coverage_sources.json` —
   schema-documented, ships with `entries: {}` (community-curated).
3. ✅ Resolver at `src/plateau_bridge/sources/coverage_ksj.py`:
   - `load_coverage_sources()` — bundled JSON → typed dict.
   - `_fetch_ksj_polygon(url, cache_dir)` — content-addressed
     download + Shapefile/GML read, EPSG:4326.
   - `resolve_explicit_polygon_from_metadata(entry, dataset_root, cache)`
     — walks metadata XML, canonicalises source-document names, looks
     up the mapping, downloads + unions matched polygons.
4. ✅ Hooked into `sources/coverage.py::resolve_coverage` between the
   catalog-pinned URL step (1) and `declared_full_admin` (2).
5. ✅ Both pipeline call sites (`pipeline/gate_a.py`,
   `pipeline/hazard_only.py`) now pass `dataset_root` through.

Tests in `tests/test_coverage_ksj.py` pin:
- Empty mapping → no-op (clean fall-through to declared_full_admin).
- Non-matching mapping → no-op.
- Wrong hazard-kind on a row → row skipped, NOT mismatched.
- Malformed `hazard` field → entry dropped with a warning, NOT crashed.
- JSON shape preserved across load cycles.

### Phase 2 — Mapping table population (ongoing; community-driven)

Each contributor adds 1–5 entries per PR to
`src/plateau_bridge/data/coverage_sources.json`. **No code changes are
needed** — the JSON ships in the wheel; a row going in immediately
upgrades any matching city on the next `plateau build` or `plateau
hazard` run.

Target the **20 highest-traffic watersheds first** for ~60 % of urban
population coverage:

```
Tokyo / Kanto
  利根川水系 (本流 + 江戸川 + 中川 + 綾瀬川)         → 5 rows
  荒川水系 (本流 + 入間川 + 神田川 + 隅田川)        → 4 rows
  多摩川水系 (本流 + 浅川 + 大栗川)                 → 3 rows
  鶴見川水系                                         → 1 row
Kansai
  淀川水系 + 大和川水系                              → 2 rows
Chubu
  庄内川 + 矢田川                                    → 2 rows
Kyushu / Hokkaido
  御笠川 / 那珂川（福岡）; 石狩川 / 豊平川（札幌）  → 3 rows
```

Each entry needs: canonical PLATEAU source-document name (from bundle
metadata) + KSJ download URL (from
[nlftp.mlit.go.jp/ksj/](https://nlftp.mlit.go.jp/ksj/)) + published
date. ~5–10 minutes per entry once you have the KSJ catalog page open.

### Phase 3 — Quality gates (not yet shipped)

`plateau verify --strict` should flag any city where:
- `declared_full_admin` is set, BUT
- the metadata XML lists ≥1 source-document we have NOT mapped to KSJ yet

This converts "missing mapping" from silent quality loss into a CI
signal. ~30 min to add once mapping table has real entries to validate
against.

## Why this matters

The honesty story is the project's reputation. A `declared_full_admin`
that's a slight overstatement (e.g. survey covered 95% of the city,
admin boundary covers 100%) is the most insidious failure mode: it
silently labels a few percent of buildings `covered = true, depth = 0`
when they're actually unsurveyed.

`explicit_polygon` from KSJ is the right ground truth. Until we ship
it, downstream UIs should treat `declared_full_admin` confidence as
a notch lower than `explicit_polygon` (which they already do via the
`coverage_confidence` enum — no API change needed).

## Out of scope

- **Inundation-union as a fallback** when no KSJ mapping exists.
  Banned by [HONESTY.md](HONESTY.md). The correct answer is `unknown`.
- **Crowd-sourcing the mapping** via OSM or similar — survey
  authority lives with MLIT; community-edited mappings would muddle
  the provenance chain.

## Status

**Phase 1 shipped** (2026-05-26) — infrastructure is in tree. Today
every catalog city still resolves to `declared_full_admin` because the
bundled `coverage_sources.json` ships with **zero mapping entries by
design** (see "Why the production JSON ships empty" below).

A 39-entry research preview (Tokyo + Osaka + Yokohama prefectural
KSJ A31) lives at
[`docs/coverage_sources.research-preview.json`](coverage_sources.research-preview.json).
To experiment, copy that file into
`src/plateau_bridge/data/coverage_sources.json` and rebuild — but read
the caveat below before doing so.

To claim a watershed: open an issue referencing the canonical
PLATEAU source-document name (from any city's bundle metadata XML),
then submit a PR adding the row.

## Why the production JSON ships empty (data-integrity lesson)

End-to-end verification on Suginami-ku (2026-05-26) revealed that
naive "PLATEAU source-document → KSJ A31 URL" mapping makes things
**worse**, not better. Concretely:

| Mode                            | covered=true | depth_max > 0 |
| ---                             | ---:         | ---:          |
| `declared_full_admin` (today)   | 142,660      | 79,912        |
| `explicit_polygon` (preview)    | 10,786       | **10,764**    |

The 69k drop in `depth_max > 0` count is the problem. PLATEAU 2024
intersects buildings with a **broader set of flood polygons** than
KSJ A31-21 publishes — it also ingests Tokyo Metropolitan
government's separately-published 流域浸水予想区域図 (e.g. 神田川
流域, 隅田川及び新河岸川流域). KSJ A31 covers river-management-area
flooding only; Tokyo Metro's urban-flooding maps are absent.

When the resolver uses KSJ A31 as the explicit extent, every
building outside that polygon (but inside Tokyo Metro's urban-flood
projection, which PLATEAU did intersect) gets `depth_max = NULL`
because we declare it "unsurveyed" — but **we actually have its
depth value, we just hid it**.

That's strictly worse than `declared_full_admin`. The project's
honesty principle is "never claim data we don't have", not "never
acknowledge data we do have".

### What "complete" Phase 2 would need

For the mapping to genuinely upgrade coverage without hiding data,
the resolved polygon must be a **superset** of the union of every
PLATEAU hit polygon. That means one of:

1. **Union KSJ A31 + Tokyo-Metro flood maps** — Tokyo Metro
   publishes its own 流域浸水予想区域 shapefiles separately
   (e.g. via 東京都オープンデータカタログサイト). Mapping entries
   would carry **multiple** URLs spanning national, prefectural,
   and municipal sources. Schema already supports `ksj_urls: list[str]`.
2. **Date-align KSJ releases** — PLATEAU 2024 may ingest sources
   newer than A31-21 (2021 release). When MLIT ships A31-24, the
   stale-mapping problem shrinks. But it never fully closes for
   municipal sources.
3. **Sanity-check at resolve time** — after computing the candidate
   explicit polygon, verify it contains the bounding box of all
   PLATEAU hit polygons before returning it. If it doesn't, fall
   through to `declared_full_admin` rather than silently masking
   depth data. This is implementable; just not shipped.

(3) is the safest near-term path. It would let us turn on the
research preview mapping today, with the resolver automatically
falling back when the polygon is insufficient.

### What's safe to ship today

The Phase 1 code itself (resolver, downloader, metadata parser, hook
into `resolve_coverage`) is **production-ready**. It's just gated by
an empty mapping table. When the data semantics are sorted (option 1
or 3 above), populating the JSON is a one-PR change.

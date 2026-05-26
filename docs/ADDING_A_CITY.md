# Adding a city to the bundled catalog

This is the highest-leverage way to contribute to plateau-bridge. Most cities
just need a JSON entry. Walkthrough below uses **横浜市 14100** as the example.

## 1. Find the CKAN URL

```bash
curl -s 'https://www.geospatial.jp/ckan/api/3/action/package_show?id=plateau-14100-yokohama-shi-2024' \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)['result']
for r in d['resources']:
    if r['format'] == 'ZIP' and 'CityGML' in r.get('name', ''):
        print(r['url'])
        break"
```

The dataset id pattern is `plateau-<JIS5>-<rōmaji>-<year>`. Try the latest
year first; fall back to earlier years if missing.

## 2. Check the admin polygon

```bash
python -c "from plateau_bridge.admin import load_admin; print(load_admin('14100'))"
```

If `None`, add it from MLIT 国土数値情報 N03 — see step 4 below.

## 3. Add a catalog entry

Append to `src/plateau_bridge/catalog_registry.json`:

```jsonc
{
  "city_code": "14100",
  "city_name": "Yokohama-shi",
  "dataset_year": 2024,
  "entries": [
    { "dataset_id": "plateau-14100-yokohama-shi-2024-bldg", "theme": "building",
      "year": 2024, "url": "https://assets.cms.plateau.reearth.io/.../citygml_*.zip",
      "udx_subdir": "udx/bldg" },
    { "dataset_id": "plateau-14100-yokohama-shi-2024-urf",  "theme": "zoning",
      "year": 2024, "url": "https://...",  "udx_subdir": "udx/urf" },
    { "dataset_id": "plateau-14100-yokohama-shi-2024-fld",  "theme": "hazard",
      "hazard_kind": "river_flood", "year": 2024,
      "url": "https://...",  "udx_subdir": "udx/fld",
      "declared_full_admin": true }
    // Add tnm/htd/lsld if the bundle has them. Pipeline tolerates missing.
  ]
}
```

A single bundle usually contains all themes under `udx/<theme>/` — share the
same `url` across entries; the downloader is content-addressed by URL so the
zip downloads once.

## 4. (If needed) Add admin polygon

For cities outside Tokyo + Osaka:

```bash
PREF=14  # prefecture code (神奈川 = 14)
curl -sL "https://nlftp.mlit.go.jp/ksj/gml/data/N03/N03-2024/N03-20240101_${PREF}_GML.zip" \
  -o /tmp/n03.zip && unzip -o /tmp/n03.zip -d /tmp/

python <<'PY'
import geopandas as gpd, pandas as pd
g = gpd.read_file('/tmp/N03-20240101_14.geojson')
city = g[g['N03_004'] == '横浜市'].copy()
city['city_code'] = '14100'
city['ward_ja'] = '横浜市'
city['ward_en'] = 'Yokohama-shi'
city = city.dissolve(by='city_code', as_index=False)[['city_code','ward_ja','ward_en','geometry']]
existing = gpd.read_file('src/plateau_bridge/data/japan_admin.geojson')
combined = gpd.GeoDataFrame(pd.concat([existing, city], ignore_index=True), crs='EPSG:4326')
combined.to_file('src/plateau_bridge/data/japan_admin.geojson', driver='GeoJSON')
PY
```

## 5. Verify

```bash
plateau info | grep 14100                        # sanity check catalog
plateau build 14100 --gates A --skip-3dtiles --no-hazards   # ~10 min download
plateau verify out                               # 0 errors expected
```

Then drop the `manifest.json` into your PR description as evidence.

## 6. (Bonus) Render a poster

```bash
plateau poster out/buildings.parquet -o docs/yokohama.png --color-by height
```

`docs/` is where the README's hero comes from — adding a screenshot for your
city helps prove "the pipeline scales beyond Tokyo".

## What you don't need to do

- **No code changes** for standard cities. The pipeline already handles
  missing hazard subdirs, missing optional themes, and mixed dataset_years.
- **No schema changes** unless the city ships an attribute the
  `ops/attributes.py` codelist mapping doesn't know about (rare).
- **No tests** for catalog additions — the existing `test_admin.py` already
  asserts every catalog city has a usable admin polygon.

## Cities currently bundled (8)

| code  | name      | year | hazards                                 |
|-------|-----------|------|-----------------------------------------|
| 01100 | 札幌市    | 2020 | river_flood                             |
| 13104 | 新宿区    | 2023 | river_flood, landslide                  |
| 13113 | 渋谷区    | 2023 | river_flood, landslide                  |
| 14100 | 横浜市    | 2024 | river_flood, tsunami, landslide         |
| 14204 | 鎌倉市    | 2024 | river_flood, tsunami, landslide         |
| 23100 | 名古屋市  | 2022 | river_flood                             |
| 27100 | 大阪市    | 2024 | river_flood, tsunami, storm_surge       |
| 40130 | 福岡市    | 2024 | river_flood, tsunami, landslide         |

Pull request adding a 9th city: 30 minutes work, 90 % data, 10 % JSON.

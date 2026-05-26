#!/usr/bin/env python3
"""Regenerate the `CITIES` array in every `examples/*/src/cities.ts`.

Picks up every `out_<slug>/buildings.parquet` in the repo root, joins
against the bundled admin polygon to derive a centroid, and writes
one entry per city. Run after a `plateau build` batch to expose the
new cities in the demo dropdowns.

    .venv/bin/python scripts/refresh_cities_ts.py

Idempotent. Diff-friendly: only the CITIES block between the two
sentinel comments is rewritten; the rest of cities.ts is preserved.
"""
from __future__ import annotations

import re
from pathlib import Path

import duckdb
import geopandas as gpd

ROOT = Path(__file__).resolve().parent.parent
ADMIN = ROOT / "src" / "plateau_parquet" / "data" / "japan_admin.geojson"
EXAMPLES = ROOT / "examples"
DEMOS = ("browser_colorby", "browser_cesium", "browser_deckgl")

# Hand-curated city_code → (slug, label) so the dropdown reads nicely.
# Slug must match the `out_<slug>/` directory the user passed to
# `plateau build --out`.
CITY_META: dict[str, tuple[str, str]] = {
    # Tokyo 23 special wards (13101–13123 except 13104 = Shinjuku special)
    "13101": ("chiyoda",    "Chiyoda 千代田区"),
    "13102": ("chuo",       "Chuo 中央区"),
    "13103": ("minato",     "Minato 港区"),
    "13104": ("shinjuku",   "Shinjuku 新宿区"),
    "13105": ("bunkyo",     "Bunkyo 文京区"),
    "13106": ("taito",      "Taito 台東区"),
    "13107": ("sumida",     "Sumida 墨田区"),
    "13108": ("koto",       "Koto 江東区"),
    "13109": ("shinagawa",  "Shinagawa 品川区"),
    "13110": ("meguro",     "Meguro 目黒区"),
    "13111": ("ota",        "Ota 大田区"),
    "13112": ("setagaya",   "Setagaya 世田谷区"),
    "13113": ("shibuya",    "Shibuya 渋谷区"),
    "13114": ("nakano",     "Nakano 中野区"),
    "13115": ("suginami",   "Suginami 杉並区"),
    "13116": ("toshima",    "Toshima 豊島区"),
    "13117": ("kita",       "Kita 北区"),
    "13118": ("arakawa",    "Arakawa 荒川区"),
    "13119": ("itabashi",   "Itabashi 板橋区"),
    "13120": ("nerima",     "Nerima 練馬区"),
    "13121": ("adachi",     "Adachi 足立区"),
    "13122": ("katsushika", "Katsushika 葛飾区"),
    "13123": ("edogawa",    "Edogawa 江戸川区"),
    # 5 major cities
    "14100": ("yokohama",   "Yokohama 横浜市"),
    "14204": ("kamakura",   "Kamakura 鎌倉市"),
    "23100": ("nagoya",     "Nagoya 名古屋市"),
    "27100": ("osaka",      "Osaka 大阪市"),
    "40130": ("fukuoka",    "Fukuoka 福岡市"),
    "01100": ("sapporo",    "Sapporo 札幌市"),
}

# Region groups → display order. Tokyo first because most users start there.
REGION_ORDER = [
    ("Tokyo 23 wards", [c for c in CITY_META if c.startswith("13")]),
    ("Greater Tokyo + other regions", ["14100", "14204", "23100", "27100", "40130", "01100"]),
]


def centroid(g: gpd.GeoDataFrame, code: str) -> tuple[float, float] | None:
    rows = g[g.city_code == code]
    if rows.empty:
        return None
    # Project to JGD2011 plane-rectangular zone IX for area-correct centroid
    # within the Tokyo / Kanto region. Centroids in WGS84 lat/lon directly
    # are slightly skewed for elongated wards.
    diss = rows.to_crs(6677).dissolve()
    c = diss.geometry.centroid.to_crs(4326).iloc[0]
    return float(c.x), float(c.y)


def building_count(slug: str) -> int | None:
    p = ROOT / f"out_{slug}" / "buildings.parquet"
    if not p.exists():
        return None
    return duckdb.sql(f"select count(*) from '{p}'").fetchone()[0]


def render_entries() -> str:
    g = gpd.read_file(ADMIN)
    lines: list[str] = []
    for region_label, codes in REGION_ORDER:
        lines.append(f"  // {region_label}")
        for code in codes:
            slug, label = CITY_META[code]
            cent = centroid(g, code)
            n = building_count(slug)
            if cent is None:
                # No admin polygon — skip silently. Adding it to admin
                # data is a separate PR.
                continue
            lon, lat = cent
            built = n if n is not None else 0
            comment = "" if n is not None else "  // not built yet — placeholder"
            lines.append(
                f'  {{ slug: "{slug}", '
                f'label: "{label}", '
                f'lon: {lon:.4f}, lat: {lat:.4f}, '
                f'buildings: {built} }},{comment}'
            )
    return "\n".join(lines)


SENTINEL_BEGIN = "// >>> cities-autogen >>>"
SENTINEL_END = "// <<< cities-autogen <<<"


def patch_one(path: Path, body: str) -> bool:
    text = path.read_text()
    block = f"export const CITIES: City[] = [\n{SENTINEL_BEGIN}\n{body}\n{SENTINEL_END}\n];"
    # Look for an existing autogen block first.
    pat_autogen = re.compile(
        r"export const CITIES: City\[\] = \[\n"
        + re.escape(SENTINEL_BEGIN)
        + r".*?"
        + re.escape(SENTINEL_END)
        + r"\n\];",
        re.DOTALL,
    )
    if pat_autogen.search(text):
        new_text = pat_autogen.sub(block, text)
    else:
        # First run — replace the legacy hand-edited array.
        pat_legacy = re.compile(r"export const CITIES: City\[\] = \[.*?\];", re.DOTALL)
        if not pat_legacy.search(text):
            print(f"  {path}: no CITIES array found, skipping")
            return False
        new_text = pat_legacy.sub(block, text)
    if new_text == text:
        return False
    path.write_text(new_text)
    return True


def main() -> None:
    body = render_entries()
    print("Computed entries:")
    for line in body.split("\n"):
        if line.strip().startswith("{"):
            print(f"  {line.strip()}")
    print()
    for demo in DEMOS:
        path = EXAMPLES / demo / "src" / "cities.ts"
        if not path.exists():
            print(f"skip {path} (does not exist)")
            continue
        changed = patch_one(path, body)
        print(f"{'wrote' if changed else 'unchanged'}: {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

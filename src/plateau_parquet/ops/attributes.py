"""Normalise nusamai GeoJSON property keys to our schema enums.

Key facts verified against real nusamai output on PLATEAU Shibuya 2023:

- Property keys in the GeoJSON output are local names from CityGML with the
  namespace prefix dropped and camelCase preserved
  (``bldg:measuredHeight`` → ``measuredHeight``).
- Scalars (``measuredHeight``, ``storeysAboveGround``) come through as plain
  numbers; strings come through as plain strings.
- ``Code`` values are resolved to their **codelist description** (the
  human-readable Japanese label), not the numeric code. We map by description.
- ``Vec<Code>`` (e.g. ``usage``) and nested data objects
  (``buildingDetailAttribute``) are emitted as **JSON-encoded strings**, not
  native arrays/objects. We ``json.loads`` them on demand.
- ``Feature.id`` and ``properties.id`` both carry the same gml_id; pyogrio
  surfaces ``properties.id`` and the citygml loader renames it to ``gml_id``.

Real-world coverage on PLATEAU Shibuya 2023 (from a 20k sample): height and
usage are 100% populated, but ``yearOfConstruction`` and
``buildingStructureType`` are 0%. The manifest's ``field_coverage`` surfaces
this so downstream UIs can grey-out unsupported queries instead of pretending.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import geopandas as gpd
import pandas as pd

from plateau_parquet.schema import Structure, Usage

log = logging.getLogger(__name__)


# Japanese description → Structure enum. Keys here are the strings nusamai
# emits after codelist resolution (BuildingDetailAttribute_buildingStructureType.xml).
STRUCTURE_MAP: dict[str, str] = {
    "木造": Structure.WOOD.value,
    "防火木造": Structure.WOOD.value,
    "鉄筋コンクリート造": Structure.RC.value,
    "鉄筋コンクリート(ＲＣ)造": Structure.RC.value,
    "鉄骨造": Structure.STEEL.value,
    "軽量鉄骨造": Structure.STEEL.value,
    "鉄骨鉄筋コンクリート造": Structure.SRC.value,
    "鉄骨鉄筋コンクリート(ＳＲＣ)造": Structure.SRC.value,
    "レンガ造": Structure.OTHER.value,
    "コンクリートブロック造": Structure.OTHER.value,
    "石造": Structure.OTHER.value,
    "その他": Structure.OTHER.value,
    "不明": Structure.OTHER.value,
}

# Japanese description → Usage enum.
USAGE_MAP: dict[str, str] = {
    "住宅": Usage.RESIDENTIAL.value,
    "共同住宅": Usage.RESIDENTIAL.value,
    "店舗等併用住宅": Usage.RESIDENTIAL.value,
    "店舗等併用共同住宅": Usage.RESIDENTIAL.value,
    "事務所": Usage.COMMERCIAL.value,
    "事務所建築物": Usage.COMMERCIAL.value,
    "店舗": Usage.COMMERCIAL.value,
    "業務施設": Usage.COMMERCIAL.value,
    "商業施設": Usage.COMMERCIAL.value,
    "工場": Usage.INDUSTRIAL.value,
    "作業所": Usage.INDUSTRIAL.value,
    "学校": Usage.EDUCATIONAL.value,
    "図書館・博物館": Usage.EDUCATIONAL.value,
    "文教厚生施設": Usage.EDUCATIONAL.value,
    "公共施設": Usage.PUBLIC.value,
    "官公庁施設": Usage.PUBLIC.value,
    "病院": Usage.PUBLIC.value,
    "厚生医療施設": Usage.PUBLIC.value,
    "神社・寺院": Usage.PUBLIC.value,
    "不明": Usage.OTHER.value,
    "その他": Usage.OTHER.value,
}


def _series_get(gdf: gpd.GeoDataFrame, *names: str) -> pd.Series | None:
    for n in names:
        if n in gdf.columns:
            return gdf[n]
    return None


def _maybe_json_loads(v: Any) -> Any:
    """Parse a JSON string if it looks like one; otherwise pass through."""
    if isinstance(v, (list, dict)):
        return v
    if not isinstance(v, str):
        return None
    if not v or v[0] not in "[{":
        return v
    try:
        return json.loads(v)
    except (json.JSONDecodeError, TypeError):
        return None


def _first_str(value: Any) -> str | None:
    """Take the first non-empty string from a possibly-JSON-stringified array.

    Used for ``usage`` (Vec<Code> → JSON string).
    """
    parsed = _maybe_json_loads(value)
    if parsed is None:
        return None
    if isinstance(parsed, str):
        return parsed or None
    if isinstance(parsed, list):
        for v in parsed:
            if isinstance(v, str) and v:
                return v
    return None


def _from_detail(series: pd.Series, key: str) -> pd.Series:
    """Pluck ``key`` from each row's ``buildingDetailAttribute`` JSON string.

    nusamai emits the nested data object as a JSON-encoded string; we
    ``json.loads`` it once per row and read the requested key from the first
    entry. Empty / unparseable rows yield ``None``.
    """

    def _pluck(v: Any) -> str | None:
        parsed = _maybe_json_loads(v)
        entries: list = []
        if isinstance(parsed, list):
            entries = parsed
        elif isinstance(parsed, dict):
            entries = [parsed]
        for entry in entries:
            if isinstance(entry, dict) and key in entry:
                val = entry[key]
                if isinstance(val, str) and val:
                    return val
                if val is None or val == "":
                    continue
                return str(val)
        return None

    return series.apply(_pluck)


def normalise(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Add normalised columns to a nusamai-derived buildings GeoDataFrame.

    Produces: ``year_built``, ``structure``, ``usage``, ``height``,
    ``floors_above``, ``floors_below``, ``fire_resistance``. All are nullable
    and tolerant of missing source attributes — Shibuya 2023 for instance
    has 0% ``yearOfConstruction`` coverage, which surfaces as an empty Int32
    column.
    """
    out = gdf.copy()

    yr_raw = _series_get(out, "yearOfConstruction", "year_of_construction")
    if yr_raw is not None:
        out["year_built"] = pd.to_numeric(yr_raw, errors="coerce").astype("Int32")
    else:
        out["year_built"] = pd.array([None] * len(out), dtype="Int32")

    h_raw = _series_get(out, "measuredHeight", "measured_height")
    if h_raw is not None:
        h = pd.to_numeric(h_raw, errors="coerce")
        # PLATEAU bldg files commonly use -9999 as a sentinel for "missing
        # height". Treat anything non-positive as missing.
        h = h.where(h > 0, other=pd.NA)
    else:
        h = pd.Series([pd.NA] * len(out), dtype="Float64")

    fa = _series_get(out, "storeysAboveGround", "storeys_above_ground")
    if fa is not None:
        fa_num = pd.to_numeric(fa, errors="coerce")
        # PLATEAU also uses 9999 as a sentinel for "unknown floor count".
        fa_num = fa_num.where((fa_num >= 0) & (fa_num < 200), other=pd.NA)
    else:
        fa_num = pd.Series([pd.NA] * len(out), dtype="Int16")
    out["floors_above"] = fa_num.astype("Int16")

    # When measuredHeight is sentinel but we have a real floor count, fall
    # back to floors × 3.5m. ~26% of "unknown-height" buildings in Shibuya
    # are recoverable this way; without it the viewer shows a salt-and-
    # pepper pattern of slate-grey buildings next to coloured ones, which
    # users mistake for a rendering bug. The estimate is loud + clearly
    # documented; downstream consumers can re-derive from raw fields if
    # they want strict-only.
    # Initial pass — leave `height` strictly from measuredHeight.
    # Gate B back-fills it with geometric height (computed from the 3D Tile
    # mesh's per-feature Y bbox) so the demos never have NULL heights to
    # render as "unknown grey". The geometry exists for 100% of buildings
    # in the tileset by definition, making this a complete fix instead of
    # a partial floors-based estimate.
    out["height"] = h.astype("Float32")
    out["height_source"] = pd.Categorical(
        ["measured" if m else "unknown" for m in h.notna()],
        categories=[
            "measured",            # cleaned measuredHeight from CityGML
            "geometric",           # max(y) - min(y) from 3D Tile mesh
            "floors_estimated",    # floors_above × 3.5m
            "footprint_fallback",  # PLATEAU only ships 2D footprint, no 3D
            "unknown",
        ],
    )

    fb = _series_get(out, "storeysBelowGround", "storeys_below_ground")
    out["floors_below"] = (
        pd.to_numeric(fb, errors="coerce").astype("Int16")
        if fb is not None
        else pd.array([None] * len(out), dtype="Int16")
    )

    # bldg:usage — Vec<Code>, serialised as a JSON-encoded array string.
    usage_raw = _series_get(out, "usage")
    if usage_raw is not None:
        out["usage"] = usage_raw.apply(_first_str).map(USAGE_MAP).fillna(Usage.OTHER.value)
    else:
        out["usage"] = None

    # uro:buildingStructureType — nested inside buildingDetailAttribute (JSON
    # string). Often unpopulated on real data; we keep that honesty in the
    # field_coverage stats.
    detail_raw = _series_get(out, "buildingDetailAttribute", "building_detail_attribute")
    if detail_raw is not None:
        struct_desc = _from_detail(detail_raw, "buildingStructureType")
        # If buildingStructureType is fully empty, leave structure as None
        # rather than defaulting every row to "other" (which would be a lie).
        if struct_desc.notna().any():
            out["structure"] = struct_desc.map(STRUCTURE_MAP).fillna(Structure.OTHER.value)
        else:
            out["structure"] = None
        out["fire_resistance"] = _from_detail(detail_raw, "fireproofStructureType")
    else:
        out["structure"] = None
        out["fire_resistance"] = None

    return out


def field_coverage(gdf: gpd.GeoDataFrame, fields: list[str]) -> dict[str, float]:
    """Fraction of non-null, non-empty values per field — for the manifest."""
    total = len(gdf)
    if total == 0:
        return {f: 0.0 for f in fields}
    out: dict[str, float] = {}
    for f in fields:
        if f not in gdf.columns:
            out[f] = 0.0
            continue
        col = gdf[f]
        nonnull = col.notna()
        is_textlike = pd.api.types.is_object_dtype(col) or pd.api.types.is_string_dtype(col)
        if is_textlike:
            def _truthy(v: Any) -> bool:
                if v is None:
                    return False
                if isinstance(v, list):
                    return len(v) > 0
                return bool(v)
            nonempty = col.apply(_truthy)
            nonnull = nonnull & nonempty
        out[f] = float(nonnull.sum() / total)
    return out

"""Client for the PLATEAU Data Catalog.

This module knows which dataset_id holds the building CityGML / each hazard theme
/ the zoning GML, for a given (city_code, dataset_year). It is intentionally a
thin layer: when the upstream API changes, only this file moves.

We bundle a static fallback registry keyed by (city_code, dataset_year). A live
HTTP query may be added later — the registry is the source of truth for `plateau
info` and CI fixtures.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, Field

from plateau_bridge.schema import HazardKind

Theme = Literal["building", "zoning", "hazard"]


class DatasetEntry(BaseModel):
    """Catalog entry for one theme of one city/year.

    Real PLATEAU bundles ship a single CityGML zip per (city, year) containing
    every theme under ``udx/<theme>/...``. Multiple ``DatasetEntry`` rows
    therefore typically share the same ``url`` and differ in ``udx_subdir``.
    The downloader is content-addressed by URL so the zip downloads once.
    """

    dataset_id: str
    theme: Theme
    hazard_kind: HazardKind | None = None
    year: int
    url: str
    udx_subdir: str = Field(
        default="",
        description=(
            "Subdirectory inside the unzipped bundle that holds this theme's "
            ".gml files, e.g. 'udx/bldg' or 'udx/fld'. Empty means use the "
            "bundle root."
        ),
    )
    coverage_extent_url: str | None = Field(
        default=None,
        description="URL of the 想定区域/調査範囲 polygon for hazard datasets, when published separately.",
    )
    declared_full_admin: bool = Field(
        default=False,
        description="Set when source metadata explicitly states full-administrative coverage.",
    )


class CityCatalog(BaseModel):
    city_code: str
    city_name: str
    dataset_year: int
    entries: list[DatasetEntry]

    def building(self) -> DatasetEntry:
        return next(e for e in self.entries if e.theme == "building")

    def zoning(self) -> DatasetEntry | None:
        return next((e for e in self.entries if e.theme == "zoning"), None)

    def hazards(self) -> dict[HazardKind, DatasetEntry]:
        return {e.hazard_kind: e for e in self.entries if e.theme == "hazard" and e.hazard_kind}


def load_registry() -> dict[tuple[str, int], CityCatalog]:
    """Load the bundled registry from `catalog_registry.json`.

    Returns an empty dict if the file is absent (lets users add cities incrementally).
    """
    try:
        raw = files("plateau_bridge").joinpath("catalog_registry.json").read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        return {}
    data = json.loads(raw)
    out: dict[tuple[str, int], CityCatalog] = {}
    for item in data:
        cat = CityCatalog.model_validate(item)
        out[(cat.city_code, cat.dataset_year)] = cat
    return out


_SLUG_TO_CODE: dict[str, str] = {
    # Tokyo 23 special wards
    "chiyoda":    "13101", "chuo":      "13102", "minato":     "13103",
    "shinjuku":   "13104", "bunkyo":    "13105", "taito":      "13106",
    "sumida":     "13107", "koto":      "13108", "shinagawa":  "13109",
    "meguro":     "13110", "ota":       "13111", "setagaya":   "13112",
    "shibuya":    "13113", "nakano":    "13114", "suginami":   "13115",
    "toshima":    "13116", "kita":      "13117", "arakawa":    "13118",
    "itabashi":   "13119", "nerima":    "13120", "adachi":     "13121",
    "katsushika": "13122", "edogawa":   "13123",
    # 6 regional cities
    "yokohama":   "14100", "kamakura":  "14204", "nagoya":     "23100",
    "osaka":      "27100", "fukuoka":   "40130", "sapporo":    "01100",
}


def resolve_city(identifier: str) -> str:
    """Coerce a slug or JIS code into a JIS code.

    Accepts ``"shibuya"`` / ``"Shibuya"`` / ``"13113"`` interchangeably,
    raising ``KeyError`` (with a helpful message) for anything else. This
    is the single source of truth used by every CLI subcommand that takes
    a city — keeps slug-vs-code disambiguation in one place.
    """
    s = identifier.strip().lower()
    if s in _SLUG_TO_CODE:
        return _SLUG_TO_CODE[s]
    # Already a JIS code (5 digits, plain ASCII)?
    if s.isdigit() and len(s) == 5:
        return s
    available = ", ".join(sorted(_SLUG_TO_CODE))
    raise KeyError(
        f"unknown city {identifier!r}; expected a 5-digit JIS code "
        f"(e.g. 13113) or one of: {available}"
    )


def get_catalog(city_code: str, dataset_year: int | None = None) -> CityCatalog:
    registry = load_registry()
    if dataset_year is not None:
        return registry[(city_code, dataset_year)]
    # Pick latest available year for the city.
    matches = sorted(
        (k for k in registry if k[0] == city_code),
        key=lambda k: k[1],
        reverse=True,
    )
    if not matches:
        raise KeyError(f"no catalog entry for city_code={city_code!r}")
    return registry[matches[0]]

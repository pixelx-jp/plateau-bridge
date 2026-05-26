"""Pydantic + Arrow schemas.

Two sources of truth:
- Pydantic models for in-memory validation and JSON manifest.
- `BUILDINGS_ARROW_SCHEMA` for the on-disk GeoParquet column layout.

Hazard fields follow a strict 4-tuple template per hazard kind. See plan §coverage.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field

ATTRIBUTION = "© Project PLATEAU / MLIT (CC BY 4.0)"


class HazardKind(StrEnum):
    RIVER_FLOOD = "river_flood"
    INLAND_FLOOD = "inland_flood"
    TSUNAMI = "tsunami"
    STORM_SURGE = "storm_surge"
    LANDSLIDE = "landslide"


class Structure(StrEnum):
    WOOD = "wood"
    RC = "rc"
    STEEL = "steel"
    SRC = "src"
    OTHER = "other"


class Usage(StrEnum):
    RESIDENTIAL = "residential"
    COMMERCIAL = "commercial"
    INDUSTRIAL = "industrial"
    EDUCATIONAL = "educational"
    PUBLIC = "public"
    OTHER = "other"


class CoverageConfidence(StrEnum):
    """Coverage confidence levels, most → least trustworthy.

    Resolve order in ``sources/coverage.py``:

    1. ``EXPLICIT_POLYGON`` — source publishes a precise 想定区域 / 調査範囲
       polygon (either via ``catalog.coverage_extent_url`` or via the KSJ
       mapping table in ``data/coverage_sources.json``). Strongest claim.
    2. ``INUNDATION_BOUNDED`` — no separately-published extent polygon
       exists, but the bundle DOES ship per-building flood depth data
       (PLATEAU's ``udx/fld/`` polygons). We use those polygons AS-IS
       as the extent — buildings inside have real modelled depth,
       buildings outside have no model. This is the literal truth of
       the data, not reverse-engineering (no buffer / dilation —
       see HONESTY.md "What 'reverse-engineering' means" section).
    3. ``DECLARED_FULL_ADMIN`` — source metadata claims full-admin
       coverage; intersect with the admin polygon. Weaker than (2)
       because it overstates: implies "modelled-and-safe" for
       buildings that were never modelled, just sit outside the
       flood-prone area entirely.
    4. ``UNKNOWN`` — no trustworthy extent available. The pipeline
       sets ``covered = false`` and ``depth_max = null``; downstream
       UIs must surface this as grey, **never green/safe**.
    """

    EXPLICIT_POLYGON = "explicit_polygon"
    INUNDATION_BOUNDED = "inundation_bounded"
    DECLARED_FULL_ADMIN = "declared_full_admin"
    UNKNOWN = "unknown"


# Hazard kinds that report a depth value. Landslide reports a zone flag instead.
DEPTH_HAZARDS: tuple[HazardKind, ...] = (
    HazardKind.RIVER_FLOOD,
    HazardKind.INLAND_FLOOD,
    HazardKind.TSUNAMI,
    HazardKind.STORM_SURGE,
)


def hazard_columns(kind: HazardKind) -> dict[str, pa.DataType]:
    """Generate the 4-column group for a hazard kind.

    For depth hazards: covered / coverage_source_ids / depth_max / hit_source_ids.
    For landslide:     covered / coverage_source_ids / in_zone   / hit_source_ids.
    """
    prefix = kind.value
    value_field = (
        (f"{prefix}_depth_max", pa.float32())
        if kind != HazardKind.LANDSLIDE
        else (f"{prefix}_in_zone", pa.bool_())
    )
    return {
        f"{prefix}_covered": pa.bool_(),
        f"{prefix}_coverage_source_ids": pa.string(),
        value_field[0]: value_field[1],
        f"{prefix}_hit_source_ids": pa.string(),
        f"{prefix}_coverage_confidence": pa.string(),
    }


def _build_arrow_schema() -> pa.Schema:
    fields: list[pa.Field] = [
        pa.field("building_uid", pa.string(), nullable=False),
        pa.field("gml_id", pa.string()),
        pa.field("city_code", pa.string(), nullable=False),
        pa.field("dataset_year", pa.int32(), nullable=False),
        pa.field("source_file_id", pa.string(), nullable=False),
        # WKB blob; downstream loaders (geopandas, pyogrio, duckdb spatial) all read it.
        pa.field("geometry", pa.binary(), nullable=False),
        pa.field("centroid_lat", pa.float64()),
        pa.field("centroid_lon", pa.float64()),
        pa.field("year_built", pa.int32()),
        pa.field("structure", pa.string()),
        pa.field("usage", pa.string()),
        pa.field("height", pa.float32()),
        pa.field("floors_above", pa.int16()),
        pa.field("floors_below", pa.int16()),
        pa.field("fire_resistance", pa.string()),
        pa.field("zoning_use", pa.string()),
        pa.field("far_max", pa.float32()),
        pa.field("tile_content_uri", pa.string()),
        pa.field("tile_feature_id", pa.int32()),
        pa.field("source_url", pa.string()),
        pa.field("source_dataset_id", pa.string()),
        pa.field("attribution", pa.string()),
    ]
    for kind in HazardKind:
        for name, dtype in hazard_columns(kind).items():
            fields.append(pa.field(name, dtype))
    return pa.schema(fields)


BUILDINGS_ARROW_SCHEMA: pa.Schema = _build_arrow_schema()


class HazardField(BaseModel):
    """In-memory hazard tuple for one building × one hazard kind."""

    model_config = ConfigDict(use_enum_values=True)

    kind: HazardKind
    covered: bool = False
    coverage_source_ids: list[str] = Field(default_factory=list)
    depth_max: float | None = None  # meters; None for landslide
    in_zone: bool | None = None  # only for landslide
    hit_source_ids: list[str] = Field(default_factory=list)
    coverage_confidence: CoverageConfidence = CoverageConfidence.UNKNOWN


class Building(BaseModel):
    """One row of buildings.parquet, validated."""

    model_config = ConfigDict(use_enum_values=True, arbitrary_types_allowed=True)

    building_uid: str
    gml_id: str | None = None
    city_code: str
    dataset_year: int
    source_file_id: str
    # GeoJSON-like mapping or WKB hex; concrete writers normalise to WKB.
    geometry_wkb: bytes
    centroid_lat: float | None = None
    centroid_lon: float | None = None
    year_built: int | None = None
    structure: Structure | None = None
    usage: Usage | None = None
    height: float | None = None
    floors_above: int | None = None
    floors_below: int | None = None
    fire_resistance: str | None = None
    zoning_use: str | None = None
    far_max: float | None = None
    tile_content_uri: str | None = None
    tile_feature_id: int | None = None
    source_url: str
    source_dataset_id: str
    attribution: str = ATTRIBUTION
    hazards: dict[HazardKind, HazardField] = Field(default_factory=dict)


class CoverageStats(BaseModel):
    """Per-hazard coverage rollup for the manifest."""

    kind: HazardKind
    covered_count: int
    hit_count: int
    coverage_confidence_breakdown: dict[CoverageConfidence, int]


class SourceRef(BaseModel):
    source_id: str
    dataset_id: str
    year: int
    url: str
    coverage_extent_url: str | None = None


class Manifest(BaseModel):
    """Provenance manifest emitted alongside each parquet output."""

    model_config = ConfigDict(use_enum_values=True)

    attribution: str = ATTRIBUTION
    tool: str = "plateau-bridge"
    tool_version: str
    generated_at: datetime
    city_code: str
    city_name: str = ""    # populated by build_manifest from catalog
    dataset_year: int
    n_buildings: int
    datasets: list[str]
    sources: dict[str, SourceRef]
    coverage_stats: list[CoverageStats]
    field_coverage: dict[str, float] = Field(
        default_factory=dict,
        description="Fraction of non-null values per CityGML-derived attribute.",
    )
    notes: list[str] = Field(default_factory=list)


# Convenience literals for the CLI.
Gate = Literal["A", "B", "C"]

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
from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class ConditionState(StrEnum):
    """Observed post-disaster triage state of a building (extension A).

    Mirrors Japan's 応急危険度判定 red/yellow/green tags. The fourth state,
    未調査 (not surveyed), is NOT a member here — it is represented honestly by
    ``condition_covered = false`` with ``condition_state = null`` (the same
    ``covered=false ⇒ value=null`` invariant the hazard columns already obey).
    Written by plateau-triage; stored + served + verified here.
    """

    DANGER = "危険"
    CAUTION = "要注意"
    INSPECTED = "調査済"


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


# The record `attribute` the condition columns populate (extension A).
CONDITION_ATTRIBUTE = "damage_state"


def condition_columns() -> dict[str, pa.DataType]:
    """The observation-state column group (extension A), all nullable.

    Optional: only populated when an observation layer (e.g. plateau-triage)
    writes it. Same honesty rule as hazards — ``condition_covered=false`` keeps
    ``condition_state`` / ``condition_confidence`` null.
    """
    return {
        "condition_covered": pa.bool_(),
        "condition_state": pa.string(),  # ConditionState value, or null
        "condition_confidence": pa.float32(),
        "condition_confidence_tier": pa.string(),
        "condition_source_ids": pa.string(),
        "condition_observed_at": pa.string(),  # RFC 3339
    }


def seismic_columns() -> dict[str, pa.DataType]:
    """The earthquake column group (extension B), all nullable except ``covered``.

    Unlike the 5 PLATEAU hazards (designated-zone polygons), earthquake comes
    from J-SHIS's nationwide **250 m probabilistic ground-motion mesh** — every
    building's centroid falls in exactly one mesh cell, so coverage is national
    rather than polygon-bounded. Same honesty rule as the hazards and condition
    groups: ``earthquake_covered=false`` (J-SHIS unreachable / outside the
    published mesh) keeps the probability and amplification null — never a
    silent 0 (which would read as "no seismic risk").
    """
    return {
        "earthquake_covered": pa.bool_(),
        # 30-year probability of JMA seismic intensity 6-lower or above (0..1).
        "earthquake_prob_strong_shaking_30yr": pa.float32(),
        # 表層地盤増幅率 (ARV, Vs=400m/s → surface); amplifies shaking & liquefaction.
        "earthquake_amplification": pa.float32(),
        # The 250 m mesh code the values were joined from (provenance/debug).
        "earthquake_meshcode": pa.string(),
        "earthquake_source_ids": pa.string(),
        "earthquake_coverage_confidence": pa.string(),
    }


def flood_susceptibility_columns() -> dict[str, pa.DataType]:
    """DEM-derived flood-susceptibility column group (extension C), all nullable.

    A **terrain-derived reference** (HAND from GSI DEM) — NOT an official 浸水想定
    depth. Distinct from the ``river_flood`` columns precisely so it's never
    confused with the surveyed hazard. Same honesty rule: covered=false keeps
    level/HAND null; "low" susceptibility is never "safe".
    """
    return {
        "flood_susceptibility_covered": pa.bool_(),
        "flood_susceptibility_level": pa.string(),     # low | medium | high
        "flood_susceptibility_hand_m": pa.float32(),   # height above nearest drainage (m)
        "flood_susceptibility_source_ids": pa.string(),
        "flood_susceptibility_coverage_confidence": pa.string(),
    }


def landslide_susceptibility_columns() -> dict[str, pa.DataType]:
    """DEM-derived landslide-susceptibility column group (extension D), all nullable.

    A **terrain-derived reference** (slope from GSI DEM) — NOT an official
    土砂災害警戒区域 designation. Distinct from the ``landslide`` (in_zone)
    columns precisely so it's never confused with the surveyed hazard. Same
    honesty rule: covered=false keeps level/slope null; "low" susceptibility is
    never "safe".
    """
    return {
        "landslide_susceptibility_covered": pa.bool_(),
        "landslide_susceptibility_level": pa.string(),       # low | medium | high
        "landslide_susceptibility_slope_deg": pa.float32(),  # max terrain slope (degrees)
        "landslide_susceptibility_source_ids": pa.string(),
        "landslide_susceptibility_coverage_confidence": pa.string(),
    }


def coastal_scope_columns() -> dict[str, pa.DataType]:
    """対象外 (terrain-certain non-exposure) flags for coastal hazards (extension G).

    ``<kind>_na`` = the building is, by terrain, outside any credible 津波/高潮
    inundation (inland ward with no designated area, or above the max modelled
    depth). A deterministic exclusion, not a risk value. Never True where the
    official ``<kind>_covered`` is True.
    """
    return {
        "tsunami_na": pa.bool_(),
        "storm_surge_na": pa.bool_(),
    }


def inland_flood_susceptibility_columns() -> dict[str, pa.DataType]:
    """DEM-derived inland (pluvial) flood-susceptibility column group (extension E).

    A **terrain-derived reference** (depression fill-and-spill from GSI DEM) — NOT
    an official 内水/雨水出水浸水想定. Distinct from ``river_flood`` (fluvial) and
    from ``flood_susceptibility`` (HAND, also fluvial): this is rainfall-driven
    ponding in local depressions. Honesty: covered=false ⇒ level/pond/tci null;
    "low" (well-drained) is never "safe"; ignores the storm-sewer network.
    """
    return {
        "inland_flood_susceptibility_covered": pa.bool_(),
        "inland_flood_susceptibility_level": pa.string(),     # low | medium | high
        "inland_flood_susceptibility_pond_m": pa.float32(),   # potential ponding depth (m)
        "inland_flood_susceptibility_tci": pa.float32(),      # depression TCI ln(A·√S/V), relative
        "inland_flood_susceptibility_source_ids": pa.string(),
        "inland_flood_susceptibility_coverage_confidence": pa.string(),
    }


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
    # extension B — earthquake (J-SHIS 250m mesh), nationwide rather than polygon-bounded
    for name, dtype in seismic_columns().items():
        fields.append(pa.field(name, dtype))
    # extension C — DEM-derived flood susceptibility (HAND), a terrain reference
    for name, dtype in flood_susceptibility_columns().items():
        fields.append(pa.field(name, dtype))
    # extension D — DEM-derived landslide susceptibility (slope), a terrain reference
    for name, dtype in landslide_susceptibility_columns().items():
        fields.append(pa.field(name, dtype))
    # extension E — DEM-derived inland (pluvial) flood susceptibility, a terrain reference
    for name, dtype in inland_flood_susceptibility_columns().items():
        fields.append(pa.field(name, dtype))
    # extension G — 対象外 (terrain-certain non-exposure) flags for coastal hazards
    for name, dtype in coastal_scope_columns().items():
        fields.append(pa.field(name, dtype))
    # extension A — optional observation-state columns (all nullable)
    for name, dtype in condition_columns().items():
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


class EarthquakeField(BaseModel):
    """In-memory earthquake tuple for one building (extension B, J-SHIS).

    Honesty invariant (mirrors hazards/condition): ``covered=false`` keeps the
    probability and amplification null — a building J-SHIS couldn't be joined to
    is never defaulted to 0 (which would read as "no seismic risk").
    """

    model_config = ConfigDict(use_enum_values=True)

    covered: bool = False
    prob_strong_shaking_30yr: float | None = None  # 0..1; None when not covered
    amplification: float | None = None  # ARV; None when not covered/unknown
    meshcode: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    coverage_confidence: CoverageConfidence = CoverageConfidence.UNKNOWN

    @model_validator(mode="after")
    def _honesty(self) -> EarthquakeField:
        if not self.covered and (
            self.prob_strong_shaking_30yr is not None or self.amplification is not None
        ):
            raise ValueError(
                "earthquake honesty invariant: covered=false requires "
                "prob_strong_shaking_30yr=None and amplification=None"
            )
        if self.prob_strong_shaking_30yr is not None and not (
            0.0 <= self.prob_strong_shaking_30yr <= 1.0
        ):
            raise ValueError("prob_strong_shaking_30yr must be in [0, 1]")
        return self


class FloodSusceptibilityField(BaseModel):
    """In-memory DEM-derived flood-susceptibility tuple (extension C, HAND).

    Honesty: ``covered=false`` keeps level and HAND null. A reference estimate —
    "low" is never "safe", and it is never an official 浸水想定 depth.
    """

    model_config = ConfigDict(use_enum_values=True)

    covered: bool = False
    level: str | None = None  # low | medium | high
    hand_m: float | None = None
    source_ids: list[str] = Field(default_factory=list)
    coverage_confidence: CoverageConfidence = CoverageConfidence.UNKNOWN

    @model_validator(mode="after")
    def _honesty(self) -> FloodSusceptibilityField:
        if not self.covered and (self.level is not None or self.hand_m is not None):
            raise ValueError(
                "flood_susceptibility honesty invariant: covered=false requires "
                "level=None and hand_m=None"
            )
        if self.level is not None and self.level not in ("low", "medium", "high"):
            raise ValueError("level must be low/medium/high")
        return self


class LandslideSusceptibilityField(BaseModel):
    """In-memory DEM-derived landslide-susceptibility tuple (extension D, slope).

    Honesty: ``covered=false`` keeps level and slope null. A reference estimate —
    "low" is never "safe", and it is never an official 土砂災害警戒区域 designation.
    """

    model_config = ConfigDict(use_enum_values=True)

    covered: bool = False
    level: str | None = None  # low | medium | high
    slope_deg: float | None = None
    source_ids: list[str] = Field(default_factory=list)
    coverage_confidence: CoverageConfidence = CoverageConfidence.UNKNOWN

    @model_validator(mode="after")
    def _honesty(self) -> LandslideSusceptibilityField:
        if not self.covered and (self.level is not None or self.slope_deg is not None):
            raise ValueError(
                "landslide_susceptibility honesty invariant: covered=false requires "
                "level=None and slope_deg=None"
            )
        if self.level is not None and self.level not in ("low", "medium", "high"):
            raise ValueError("level must be low/medium/high")
        return self


class InlandFloodSusceptibilityField(BaseModel):
    """In-memory DEM-derived inland (pluvial) flood-susceptibility tuple (extension E).

    Honesty: ``covered=false`` keeps level/pond/tci null. A reference estimate —
    "low" is never "safe", it is never an official 内水浸水想定, and it ignores the
    storm-sewer drainage network.
    """

    model_config = ConfigDict(use_enum_values=True)

    covered: bool = False
    level: str | None = None  # low | medium | high
    pond_m: float | None = None
    tci: float | None = None
    source_ids: list[str] = Field(default_factory=list)
    coverage_confidence: CoverageConfidence = CoverageConfidence.UNKNOWN

    @model_validator(mode="after")
    def _honesty(self) -> InlandFloodSusceptibilityField:
        if not self.covered and (self.level is not None or self.pond_m is not None):
            raise ValueError(
                "inland_flood_susceptibility honesty invariant: covered=false requires "
                "level=None and pond_m=None"
            )
        if self.level is not None and self.level not in ("low", "medium", "high"):
            raise ValueError("level must be low/medium/high")
        return self


class ConditionField(BaseModel):
    """In-memory observation-state tuple for one building (extension A).

    Enforces the honesty invariant: ``covered=false`` (未調査) keeps ``state``
    and ``confidence`` null — a building nobody surveyed is never defaulted to
    a state.
    """

    model_config = ConfigDict(use_enum_values=True)

    covered: bool = False
    state: ConditionState | None = None
    confidence: float | None = None
    confidence_tier: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    observed_at: str | None = None

    @model_validator(mode="after")
    def _honesty(self) -> ConditionField:
        if not self.covered and (self.state is not None or self.confidence is not None):
            raise ValueError(
                "condition honesty invariant: covered=false requires state=None "
                "and confidence=None"
            )
        return self


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
    seismic: EarthquakeField | None = None  # extension B — earthquake (J-SHIS 250m mesh)
    flood_susceptibility: FloodSusceptibilityField | None = None  # extension C — DEM HAND
    landslide_susceptibility: LandslideSusceptibilityField | None = None  # extension D — DEM slope
    inland_flood_susceptibility: InlandFloodSusceptibilityField | None = None  # extension E — DEM pluvial
    condition: ConditionField | None = None  # extension A — optional observation state


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

"""The universal honest record — the shared query core's output shape.

This is plateau-bridge's reference implementation of the cross-repo
``record.schema.json`` contract: state, rules and hazard are all the *same*
record, differing only by ``attribute``. The query+honesty core (``query.py``)
emits these; consumers (caveat-mcp / sixth-sense / terra-incognita / triage /
robo-permit) read them.

The honesty invariant is enforced structurally here so it is impossible to mint
a record that claims a value for something nobody assessed:

    covered=false  ⇒  value is None  AND  confidence is None
    covered=true   ⇒  confidence_tier ∈ {measured, inferred, modelled}

This mirrors the on-disk rule already enforced for hazard columns
(``covered=false ⇒ depth_max=null``, see ``verify.py``) — one invariant, now
also expressed at the record boundary the whole ecosystem reads.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from plateau_bridge.schema import ATTRIBUTION

SCHEMA_VERSION = "0.1"
DEFAULT_CRS = "EPSG:6668"


class AssetIdScheme(StrEnum):
    SURROGATE = "surrogate"
    PLATEAU_BLDG_ID = "plateau_bldg_id"
    PLATEAU_GML_ID = "plateau_gml_id"
    REAL_ESTATE_ID = "real_estate_id"
    DATASET_FOOTPRINT = "dataset_footprint"
    JARTIC_COORD = "jartic_coord"
    HOKONAVI_PLACE_ID = "hokonavi_place_id"
    SHISETSU_ID = "shisetsu_id"
    OTHER = "other"


class AssetClass(StrEnum):
    BUILDING = "building"
    BRIDGE = "bridge"
    TUNNEL = "tunnel"
    ROAD_SEGMENT = "road_segment"
    AREA = "area"
    POLE = "pole"
    SIGN = "sign"
    FACADE = "facade"
    OTHER = "other"


class ConfidenceTier(StrEnum):
    MEASURED = "measured"
    INFERRED = "inferred"
    MODELLED = "modelled"
    UNKNOWN = "unknown"


ASSESSED_TIERS: frozenset[ConfidenceTier] = frozenset(
    {ConfidenceTier.MEASURED, ConfidenceTier.INFERRED, ConfidenceTier.MODELLED}
)


class Modality(StrEnum):
    DRONE = "drone"
    STREET = "street"
    SATELLITE = "satellite"
    SAR = "sar"
    FIELD_INSPECTION = "field_inspection"
    PUBLIC_DATASET = "public_dataset"
    OFFICIAL_API = "official_api"
    FUSED = "fused"
    MANUAL = "manual"


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lat: float
    lon: float
    elev: float | None = None


class Source(BaseModel):
    """Provenance. ``modality`` required; at least one locator required."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    modality: Modality
    dataset_id: str | None = None
    capture_id: str | None = None
    url: str | None = None
    device: str | None = None
    operator: str | None = None
    method: str | None = None
    model_version: str | None = None

    @model_validator(mode="after")
    def _traceable(self) -> Source:
        if not (self.dataset_id or self.capture_id or self.url):
            raise ValueError(
                "source must be traceable: provide at least one of "
                "dataset_id / capture_id / url"
            )
        return self


class Record(BaseModel):
    """A universal honest record (one ``attribute`` of one asset)."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    schema_version: str = SCHEMA_VERSION
    asset_id: str
    asset_id_scheme: AssetIdScheme
    asset_class: AssetClass
    location: Location | None = None
    geometry_wkb: str | None = None
    crs: str | None = DEFAULT_CRS
    attribute: str
    value: str | float | bool | None = None
    unit: str | None = None
    covered: bool
    confidence_tier: ConfidenceTier | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    source: Source
    observed_at: str
    valid_until: str | None = None
    geom_binding_conf: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _enforce_honesty(self) -> Record:
        if self.covered:
            if self.confidence_tier is None:
                raise ValueError("covered=true requires a confidence_tier")
            if ConfidenceTier(self.confidence_tier) not in ASSESSED_TIERS:
                raise ValueError(
                    "covered=true requires confidence_tier in "
                    f"{sorted(t.value for t in ASSESSED_TIERS)} (got {self.confidence_tier!r})"
                )
        else:
            if self.value is not None:
                raise ValueError(
                    "honesty invariant violated: covered=false requires value=None "
                    f"(got {self.value!r})"
                )
            if self.confidence is not None:
                raise ValueError("honesty invariant: covered=false requires confidence=None")
            if self.confidence_tier not in (None, ConfidenceTier.UNKNOWN.value):
                raise ValueError(
                    "covered=false requires confidence_tier None or 'unknown' "
                    f"(got {self.confidence_tier!r})"
                )
        return self

    def to_contract_dict(self) -> dict[str, Any]:
        """Serialise to a dict conforming to the frozen ``record.schema.json``.

        Optional fields whose schema type forbids ``null`` (only
        ``confidence_tier``) are omitted when None; nullable fields are kept so
        ``value: null`` stays explicit on the wire.
        """
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "asset_id": self.asset_id,
            "asset_id_scheme": self.asset_id_scheme,
            "asset_class": self.asset_class,
            "attribute": self.attribute,
            "value": self.value,
            "covered": self.covered,
            "source": self.source.model_dump(exclude_none=True),
            "observed_at": self.observed_at,
            "confidence": self.confidence,
        }
        if self.location is not None:
            out["location"] = self.location.model_dump(exclude_none=True)
        if self.geometry_wkb is not None:
            out["geometry_wkb"] = self.geometry_wkb
        if self.crs is not None:
            out["crs"] = self.crs
        if self.unit is not None:
            out["unit"] = self.unit
        if self.confidence_tier is not None:
            out["confidence_tier"] = self.confidence_tier
        if self.valid_until is not None:
            out["valid_until"] = self.valid_until
        if self.geom_binding_conf is not None:
            out["geom_binding_conf"] = self.geom_binding_conf
        return out

    @classmethod
    def unknown(
        cls,
        *,
        asset_id: str,
        asset_id_scheme: AssetIdScheme | str,
        attribute: str,
        source: Source | dict[str, Any],
        observed_at: str,
        asset_class: AssetClass | str = AssetClass.BUILDING,
        **extra: Any,
    ) -> Record:
        """The explicit unknown: ``covered=false, value=null``."""
        extra.pop("value", None)
        extra.pop("confidence", None)
        return cls(
            asset_id=asset_id,
            asset_id_scheme=asset_id_scheme,
            asset_class=asset_class,
            attribute=attribute,
            value=None,
            covered=False,
            confidence_tier=ConfidenceTier.UNKNOWN,
            confidence=None,
            source=source,
            observed_at=observed_at,
            **extra,
        )


def provenance_chain(record: Record) -> dict[str, Any]:
    """``cite(record)`` — the provenance chain behind a record (contract §②)."""
    src = record.source
    return {
        "asset_id": record.asset_id,
        "asset_id_scheme": record.asset_id_scheme,
        "attribute": record.attribute,
        "covered": record.covered,
        "observed_at": record.observed_at,
        "attribution": ATTRIBUTION,
        "source": src.model_dump(exclude_none=True),
    }

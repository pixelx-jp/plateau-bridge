"""The shared record-query + honesty-enforcement core (extension C).

Write once, reused four+ ways — caveat-mcp (LLM), sixth-sense (ROS costmap),
terra-incognita (context_api), plateau-triage (read/write). Honesty lives in
*one* place so it cannot be re-implemented four times and drift.

It reads a built ``buildings.parquet`` via DuckDB and emits the universal
:class:`~plateau_bridge.records.Record` shape, for both hazard attributes (e.g.
``river_flood_depth_max``) and the optional observation-state attribute
(``damage_state``, extension A). The guarantee, enforced at the single choke
points :meth:`RecordQuery._hazard_record` / :meth:`RecordQuery._condition_record`:

    a building outside an attribute's coverage  ⇒  covered=false, value=null

i.e. *missing = unknown, never defaulted to 0/safe* — the read-side twin of the
on-disk rule ``verify.py`` enforces for the parquet itself.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from plateau_bridge.records import (
    AssetClass,
    AssetIdScheme,
    ConfidenceTier,
    Modality,
    Record,
    Source,
    provenance_chain,
)
from plateau_bridge.schema import CONDITION_ATTRIBUTE, DEPTH_HAZARDS, HazardKind


@dataclass(frozen=True)
class HazardAttr:
    """Maps a record ``attribute`` to its on-disk hazard column group."""

    attribute: str
    kind: HazardKind
    value_col: str
    is_depth: bool
    unit: str | None

    @property
    def covered_col(self) -> str:
        return f"{self.kind.value}_covered"

    @property
    def confidence_col(self) -> str:
        return f"{self.kind.value}_coverage_confidence"

    @property
    def hit_col(self) -> str:
        return f"{self.kind.value}_hit_source_ids"

    @property
    def coverage_src_col(self) -> str:
        return f"{self.kind.value}_coverage_source_ids"

    def columns(self) -> tuple[str, ...]:
        return (self.value_col, self.covered_col, self.confidence_col,
                self.hit_col, self.coverage_src_col)


def _build_registry() -> dict[str, HazardAttr]:
    reg: dict[str, HazardAttr] = {}
    for kind in HazardKind:
        if kind in DEPTH_HAZARDS:
            attr = f"{kind.value}_depth_max"
            reg[attr] = HazardAttr(attr, kind, attr, is_depth=True, unit="m")
        else:  # landslide
            attr = f"{kind.value}_in_zone"
            reg[attr] = HazardAttr(attr, kind, attr, is_depth=False, unit=None)
    return reg


HAZARD_ATTRS: dict[str, HazardAttr] = _build_registry()

# Condition (extension A) column group + the covered flag that gates it.
CONDITION_COVERED_COL = "condition_covered"
CONDITION_COLS = (
    CONDITION_COVERED_COL,
    "condition_state",
    "condition_confidence",
    "condition_confidence_tier",
    "condition_source_ids",
    "condition_observed_at",
)

# Base (non-attribute) columns every record needs.
_BASE_COLS = (
    "building_uid",
    "gml_id",
    "city_code",
    "dataset_year",
    "centroid_lat",
    "centroid_lon",
    "geometry",
    "source_dataset_id",
    "source_url",
)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


class UnknownAssetError(LookupError):
    """Raised when an asset_id is not present in the parquet."""


class RecordQuery:
    """Query a built ``buildings.parquet`` for honest records."""

    def __init__(self, parquet_path: str | Path):
        self.parquet_path = str(parquet_path)
        self._con = duckdb.connect(":memory:")
        self._con.execute(f"CREATE VIEW b AS SELECT * FROM '{self.parquet_path}'")
        self._cols: set[str] = {r[0] for r in self._con.execute("DESCRIBE b").fetchall()}

    # -- public API (contract §②) ------------------------------------------
    @property
    def attributes(self) -> list[str]:
        """Attributes available in this parquet (hazards + condition if present)."""
        attrs = [a for a, h in HAZARD_ATTRS.items() if h.covered_col in self._cols]
        if CONDITION_COVERED_COL in self._cols:
            attrs.append(CONDITION_ATTRIBUTE)
        return attrs

    def get_record(self, asset_id: str, attribute: str) -> Record:
        """One attribute of one asset. Uncovered ⇒ covered=false, value=null."""
        attr = self._require_attr(attribute)
        row = self._fetch_one(asset_id, attr)
        if row is None:
            raise UnknownAssetError(f"asset_id {asset_id!r} not found")
        return self._record_from_row(row, attr)

    def query_asset(self, asset_id: str, attributes: list[str] | None = None) -> list[Record]:
        """All (or selected) attributes for one asset."""
        attrs = self._resolve_attrs(attributes)
        out: list[Record] = []
        for attr in attrs:
            row = self._fetch_one(asset_id, attr)
            if row is None:
                raise UnknownAssetError(f"asset_id {asset_id!r} not found")
            out.append(self._record_from_row(row, attr))
        return out

    def query_point(
        self,
        lat: float,
        lon: float,
        radius_m: float,
        attributes: list[str] | None = None,
    ) -> list[Record]:
        """Records for every asset within ``radius_m`` of (lat, lon)."""
        attrs = self._resolve_attrs(attributes)
        deg_lat = radius_m / 111_320.0
        deg_lon = radius_m / (111_320.0 * max(0.01, math.cos(math.radians(lat))))
        select_cols = self._select_columns(attrs)
        rows = self._con.execute(
            f"SELECT {select_cols} FROM b "
            "WHERE centroid_lat BETWEEN ? AND ? AND centroid_lon BETWEEN ? AND ?",
            [lat - deg_lat, lat + deg_lat, lon - deg_lon, lon + deg_lon],
        ).fetchall()
        names = [d[0] for d in self._con.description]
        out: list[Record] = []
        for raw in rows:
            row = dict(zip(names, raw, strict=True))
            if row["centroid_lat"] is None or row["centroid_lon"] is None:
                continue
            if _haversine_m(lat, lon, row["centroid_lat"], row["centroid_lon"]) > radius_m:
                continue
            for attr in attrs:
                out.append(self._record_from_row(row, attr))
        return out

    def coverage(self, bbox: tuple[float, float, float, float] | None = None) -> dict[str, Any]:
        """Coverage rollup — "how far does our knowledge go" (contract §②).

        ``bbox`` = (min_lat, min_lon, max_lat, max_lon), optional.
        """
        where, params = "", []
        if bbox is not None:
            where = " WHERE centroid_lat BETWEEN ? AND ? AND centroid_lon BETWEEN ? AND ?"
            params = [bbox[0], bbox[2], bbox[1], bbox[3]]
        total = self._con.execute(f"SELECT COUNT(*) FROM b{where}", params).fetchone()[0]
        by_attribute: dict[str, dict[str, int]] = {}
        for attr in self.attributes:
            covered_col = self._covered_col(attr)
            covered = self._con.execute(
                f"SELECT COUNT(*) FROM b{where}{' AND ' if where else ' WHERE '}{covered_col}",
                params,
            ).fetchone()[0]
            by_attribute[attr] = {
                "total": int(total),
                "covered": int(covered),
                "unknown": int(total - covered),
            }
        return {
            "total": int(total),
            "by_attribute": by_attribute,
            "parquet": self.parquet_path,
        }

    @staticmethod
    def cite(record: Record) -> dict[str, Any]:
        """The provenance chain behind a record."""
        return provenance_chain(record)

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> RecordQuery:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- internals ---------------------------------------------------------
    def _is_condition(self, attribute: str) -> bool:
        return attribute == CONDITION_ATTRIBUTE

    def _covered_col(self, attribute: str) -> str:
        return CONDITION_COVERED_COL if self._is_condition(attribute) else HAZARD_ATTRS[attribute].covered_col

    def _require_attr(self, attribute: str) -> str:
        if self._is_condition(attribute):
            if CONDITION_COVERED_COL not in self._cols:
                raise ValueError(f"attribute {attribute!r} not present in this parquet")
            return attribute
        if attribute not in HAZARD_ATTRS:
            known = sorted([*HAZARD_ATTRS, CONDITION_ATTRIBUTE])
            raise ValueError(f"unknown attribute {attribute!r}; available: {known}")
        if HAZARD_ATTRS[attribute].covered_col not in self._cols:
            raise ValueError(f"attribute {attribute!r} not present in this parquet")
        return attribute

    def _resolve_attrs(self, attributes: list[str] | None) -> list[str]:
        if attributes is None:
            return list(self.attributes)
        return [self._require_attr(a) for a in attributes]

    def _columns_for(self, attribute: str) -> tuple[str, ...]:
        return CONDITION_COLS if self._is_condition(attribute) else HAZARD_ATTRS[attribute].columns()

    def _select_columns(self, attributes: list[str]) -> str:
        cols: list[str] = [c for c in _BASE_COLS if c in self._cols]
        for attr in attributes:
            for c in self._columns_for(attr):
                if c in self._cols and c not in cols:
                    cols.append(c)
        return ", ".join(cols)

    def _fetch_one(self, asset_id: str, attribute: str) -> dict[str, Any] | None:
        select_cols = self._select_columns([attribute])
        cur = self._con.execute(
            f"SELECT {select_cols} FROM b WHERE building_uid = ?", [asset_id]
        )
        raw = cur.fetchone()
        if raw is None:
            return None
        return dict(zip([d[0] for d in cur.description], raw, strict=True))

    def _record_from_row(self, row: dict[str, Any], attribute: str) -> Record:
        if self._is_condition(attribute):
            return self._condition_record(row)
        return self._hazard_record(row, HAZARD_ATTRS[attribute])

    def _common_fields(self, row: dict[str, Any], attribute: str) -> dict[str, Any]:
        location = None
        if row.get("centroid_lat") is not None and row.get("centroid_lon") is not None:
            location = {"lat": row["centroid_lat"], "lon": row["centroid_lon"]}
        geom = row.get("geometry")
        geometry_wkb = geom.hex() if isinstance(geom, (bytes, bytearray)) else None
        return dict(
            asset_id=row["building_uid"],
            asset_id_scheme=AssetIdScheme.OTHER,  # building_uid is bridge's stable surrogate
            asset_class=AssetClass.BUILDING,
            attribute=attribute,
            location=location,
            geometry_wkb=geometry_wkb,
        )

    def _hazard_record(self, row: dict[str, Any], haz: HazardAttr) -> Record:
        covered = bool(row.get(haz.covered_col))
        observed_at = f"{int(row['dataset_year'])}-01-01T00:00:00Z"
        confidence_label = row.get(haz.confidence_col)
        source = Source(
            modality=Modality.PUBLIC_DATASET,
            dataset_id=row.get("source_dataset_id"),
            url=row.get("source_url"),
            method=(
                f"hazard_model; coverage_confidence={confidence_label}"
                if confidence_label
                else "hazard_model"
            ),
        )
        common = self._common_fields(row, haz.attribute)
        common.update(source=source, observed_at=observed_at)
        if not covered:
            # honesty choke point: uncovered ⇒ unknown, never safe
            return Record.unknown(**common)

        raw_value = row.get(haz.value_col)
        value: float | bool | None
        if haz.is_depth:
            value = float(raw_value) if raw_value is not None else None
        else:
            value = bool(raw_value) if raw_value is not None else None
        return Record(
            value=value,
            unit=haz.unit,
            covered=True,
            confidence_tier=ConfidenceTier.MODELLED,
            confidence=None,  # qualitative tier only; no calibrated probability invented
            **common,
        )

    def _condition_record(self, row: dict[str, Any]) -> Record:
        covered = bool(row.get(CONDITION_COVERED_COL))
        observed_at = row.get("condition_observed_at") or f"{int(row['dataset_year'])}-01-01T00:00:00Z"
        source_ids = row.get("condition_source_ids")
        source = Source(
            modality=Modality.FUSED,
            dataset_id=source_ids or row.get("source_dataset_id"),
            url=row.get("source_url"),
            method="observation_layer",
        )
        common = self._common_fields(row, CONDITION_ATTRIBUTE)
        common.update(source=source, observed_at=observed_at)
        if not covered:
            return Record.unknown(**common)

        tier = row.get("condition_confidence_tier") or ConfidenceTier.INFERRED.value
        conf = row.get("condition_confidence")
        return Record(
            value=row.get("condition_state"),
            covered=True,
            confidence_tier=tier,
            confidence=float(conf) if conf is not None else None,
            **common,
        )

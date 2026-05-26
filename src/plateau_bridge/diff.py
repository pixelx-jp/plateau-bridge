"""Compare two ``buildings.parquet`` files across dataset_years.

`plateau diff old.parquet new.parquet` answers questions like:

- How many buildings appear in the new dataset but not the old? (new construction)
- How many disappeared? (demolitions — *as PLATEAU sees them*; not authoritative)
- For buildings that exist in both, did the hazard intersection result change?
  (could be: building moved category because a new flood survey was published,
  or because the building grew taller / footprint changed)

This is a server-side analyst tool, not a UI primitive. The output is a
DiffReport you can pretty-print, ship to a notebook, or assert on in a CI
"don't regress" check.

The matching key:

* If both sides have the same ``dataset_year`` → match on ``building_uid``
  (strict, both sides agree on the same PLATEAU release).
* If years differ → match on ``gml_id`` (gml_id is *meant* to be year-stable
  in PLATEAU's i-UR spec, but in practice it's renamed often; we report the
  unmatched counts on both sides so you can see the churn).

A future ``canonical_building_id`` would match by geometry too — out of v1 scope.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import geopandas as gpd

from plateau_bridge.schema import DEPTH_HAZARDS, HazardKind

log = logging.getLogger(__name__)


@dataclass
class DiffReport:
    a_path: Path
    b_path: Path
    n_a: int
    n_b: int
    match_key: str
    n_matched: int
    n_only_in_a: int     # disappeared between A and B
    n_only_in_b: int     # appeared between A and B
    hazard_deltas: dict[str, dict[str, int]] = field(default_factory=dict)
    """Per-hazard counts: {kind → {newly_covered, newly_hit, no_longer_hit, depth_grew, depth_shrank}}."""

    def ok(self) -> bool:
        """No correctness contract — diff is informational. Always returns True."""
        return True


def _pick_match_key(a: gpd.GeoDataFrame, b: gpd.GeoDataFrame) -> str:
    """If both sides are the same dataset_year, use building_uid for strictness.
    Otherwise fall back to gml_id (which is intended to be year-stable)."""
    if "dataset_year" not in a.columns or "dataset_year" not in b.columns:
        return "gml_id"
    ya = set(a["dataset_year"].dropna().unique())
    yb = set(b["dataset_year"].dropna().unique())
    if ya == yb and len(ya) == 1:
        return "building_uid"
    return "gml_id"


def _hazard_delta(
    a: gpd.GeoDataFrame, b: gpd.GeoDataFrame, kind: HazardKind, key: str,
) -> dict[str, int]:
    cov_col = f"{kind.value}_covered"
    if cov_col not in a.columns or cov_col not in b.columns:
        return {}
    ix = a[[key, cov_col]].merge(
        b[[key, cov_col]], on=key, how="inner", suffixes=("_a", "_b")
    )
    newly_covered = int(((~ix[f"{cov_col}_a"].astype(bool)) & ix[f"{cov_col}_b"].astype(bool)).sum())

    out: dict[str, int] = {"newly_covered": newly_covered}

    if kind in DEPTH_HAZARDS:
        depth_col = f"{kind.value}_depth_max"
        if depth_col in a.columns and depth_col in b.columns:
            dx = a[[key, depth_col]].merge(
                b[[key, depth_col]], on=key, how="inner", suffixes=("_a", "_b")
            )
            a_hit = dx[f"{depth_col}_a"].fillna(-1) > 0
            b_hit = dx[f"{depth_col}_b"].fillna(-1) > 0
            out["newly_hit"] = int(((~a_hit) & b_hit).sum())
            out["no_longer_hit"] = int((a_hit & (~b_hit)).sum())
            both = a_hit & b_hit
            out["depth_grew"] = int(
                (both & (dx[f"{depth_col}_b"].fillna(0) > dx[f"{depth_col}_a"].fillna(0))).sum()
            )
            out["depth_shrank"] = int(
                (both & (dx[f"{depth_col}_b"].fillna(0) < dx[f"{depth_col}_a"].fillna(0))).sum()
            )
    else:
        # Landslide uses _in_zone (bool).
        zone_col = f"{kind.value}_in_zone"
        if zone_col in a.columns and zone_col in b.columns:
            dx = a[[key, zone_col]].merge(
                b[[key, zone_col]], on=key, how="inner", suffixes=("_a", "_b")
            )
            out["newly_hit"] = int(
                ((~dx[f"{zone_col}_a"].astype(bool)) & dx[f"{zone_col}_b"].astype(bool)).sum()
            )
            out["no_longer_hit"] = int(
                (dx[f"{zone_col}_a"].astype(bool) & (~dx[f"{zone_col}_b"].astype(bool))).sum()
            )
    return out


def diff(a_path: Path, b_path: Path) -> DiffReport:
    """Compute the diff. Both sides loaded as parquet; geometry not compared."""
    a = gpd.read_parquet(a_path)
    b = gpd.read_parquet(b_path)
    key = _pick_match_key(a, b)
    if key not in a.columns or key not in b.columns:
        raise ValueError(f"diff key {key!r} missing in one of the inputs")

    a_ids = set(a[key].dropna().astype(str))
    b_ids = set(b[key].dropna().astype(str))
    matched = a_ids & b_ids

    hazard_deltas: dict[str, dict[str, int]] = {}
    for kind in HazardKind:
        delta = _hazard_delta(a, b, kind, key)
        if delta:
            hazard_deltas[kind.value] = delta

    return DiffReport(
        a_path=a_path, b_path=b_path,
        n_a=len(a), n_b=len(b), match_key=key,
        n_matched=len(matched),
        n_only_in_a=len(a_ids - b_ids),
        n_only_in_b=len(b_ids - a_ids),
        hazard_deltas=hazard_deltas,
    )

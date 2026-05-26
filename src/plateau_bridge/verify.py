"""Health report for a built artifact bundle.

``plateau verify ./out`` is the single command that tells you whether a build
is publishable: does it cover the schema, does it honour the honesty
invariants, are the source IDs cross-referenced consistently in the manifest?

This is the *output* analog of unit tests — code tests guard logic, ``verify``
guards data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from plateau_bridge.schema import DEPTH_HAZARDS, HazardKind, Manifest


@dataclass
class Finding:
    severity: str   # "error" | "warn" | "info"
    code: str
    message: str


@dataclass
class VerifyReport:
    out_dir: Path
    n_buildings: int
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warn"]

    def ok(self) -> bool:
        return not self.errors


def _expected_columns() -> list[str]:
    cols: list[str] = [
        "building_uid", "gml_id", "city_code", "dataset_year", "source_file_id",
        "centroid_lat", "centroid_lon",
        "year_built", "structure", "usage", "height",
        "floors_above", "floors_below", "fire_resistance",
        "zoning_use", "far_max",
        "tile_content_uri", "tile_feature_id",
        "source_url", "source_dataset_id", "attribution",
    ]
    for kind in HazardKind:
        cols.append(f"{kind.value}_covered")
        cols.append(f"{kind.value}_coverage_source_ids")
        cols.append(f"{kind.value}_coverage_confidence")
        cols.append(f"{kind.value}_hit_source_ids")
        cols.append(
            f"{kind.value}_depth_max" if kind in DEPTH_HAZARDS else f"{kind.value}_in_zone"
        )
    return cols


def verify(out_dir: Path) -> VerifyReport:
    parquet = out_dir / "buildings.parquet"
    manifest_path = out_dir / "manifest.json"

    findings: list[Finding] = []
    if not parquet.exists():
        return VerifyReport(out_dir, 0, [Finding("error", "missing_parquet", str(parquet))])
    if not manifest_path.exists():
        findings.append(Finding("error", "missing_manifest", str(manifest_path)))

    manifest = (
        Manifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.exists()
        else None
    )

    con = duckdb.connect(":memory:")
    con.execute(f"CREATE VIEW b AS SELECT * FROM '{parquet}'")
    n = con.execute("SELECT COUNT(*) FROM b").fetchone()[0]

    # 1. Schema coverage.
    cols = {r[0] for r in con.execute("DESCRIBE b").fetchall()}
    for expected in _expected_columns():
        if expected not in cols:
            findings.append(
                Finding("error", "missing_column", f"buildings.parquet missing {expected!r}")
            )

    # 2. UID uniqueness.
    dup = con.execute(
        "SELECT building_uid, COUNT(*) c FROM b GROUP BY 1 HAVING c > 1 LIMIT 5"
    ).fetchall()
    if dup:
        findings.append(
            Finding("error", "uid_not_unique", f"{len(dup)} duplicate building_uid(s) sample: {dup}")
        )

    # 3. Honesty invariant: covered=false MUST NOT carry a depth_max value.
    for kind in DEPTH_HAZARDS:
        cov = f"{kind.value}_covered"
        depth = f"{kind.value}_depth_max"
        if cov in cols and depth in cols:
            row = con.execute(
                f"SELECT COUNT(*) FROM b WHERE NOT {cov} AND {depth} IS NOT NULL AND {depth} > 0"
            ).fetchone()
            if row and row[0] > 0:
                findings.append(
                    Finding(
                        "error",
                        "honesty_violation",
                        f"{row[0]} rows have {depth}>0 while {cov}=false (uncovered hits)",
                    )
                )

    # 4. Manifest cross-consistency.
    if manifest is not None:
        if manifest.n_buildings != n:
            findings.append(
                Finding(
                    "warn",
                    "manifest_count_mismatch",
                    f"manifest.n_buildings={manifest.n_buildings} but parquet has {n}",
                )
            )
        # Every coverage_source_ids in the data must appear in manifest.sources.
        # Coverage source_ids carry an annotation suffix (`+inundation_bounded`,
        # `+ksj:<source-docs>`) that records how the extent was resolved.
        # Strip these annotations before manifest lookup — the base
        # `dataset_id` is what's pinned in the manifest.
        known = set(manifest.sources)
        for kind in HazardKind:
            cov_src = f"{kind.value}_coverage_source_ids"
            if cov_src not in cols:
                continue
            rows = con.execute(
                f"SELECT DISTINCT {cov_src} FROM b WHERE {cov_src} <> ''"
            ).fetchall()
            for (csv,) in rows:
                for src in str(csv).split(","):
                    if not src:
                        continue
                    base = src.split("+", 1)[0]
                    if base not in known:
                        findings.append(
                            Finding(
                                "warn",
                                "orphan_source_id",
                                f"{cov_src} references {src!r} not in manifest.sources",
                            )
                        )

    # 5. Attribution invariant: every row must carry attribution.
    if "attribution" in cols:
        missing = con.execute(
            "SELECT COUNT(*) FROM b WHERE attribution IS NULL OR attribution = ''"
        ).fetchone()
        if missing and missing[0] > 0:
            findings.append(
                Finding("error", "missing_attribution", f"{missing[0]} rows have no attribution string")
            )

    # 6. Coverage stats — informational. We separate "PLATEAU has no
    # dataset for this hazard kind in this city" (expected for most cities
    # — e.g. Tokyo has no storm_surge dataset, Osaka has no landslide) from
    # "dataset present but extent resolver failed" (a real issue).
    if manifest is not None:
        dataset_kinds = {
            src_id.split("-")[-1] for src_id in manifest.sources
        }
        kind_to_udx = {
            "river_flood": "fld", "inland_flood": "ifld", "tsunami": "tnm",
            "storm_surge": "htd", "landslide": "lsld",
        }
        for cs in manifest.coverage_stats:
            if cs.covered_count == 0:
                udx = kind_to_udx.get(cs.kind, cs.kind)
                if udx not in dataset_kinds:
                    msg = (f"{cs.kind}: no dataset in catalog for this city "
                           f"(expected — PLATEAU doesn't ship this hazard kind for every city)")
                else:
                    msg = (f"{cs.kind}: dataset present but 0 buildings covered "
                           f"— check coverage_extent_url or declared_full_admin in catalog")
                findings.append(Finding("info", "no_coverage", msg))

    return VerifyReport(out_dir=out_dir, n_buildings=int(n), findings=findings)

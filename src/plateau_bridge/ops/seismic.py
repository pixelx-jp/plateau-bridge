"""Building × J-SHIS seismic join (extension B).

Earthquake is national, not polygon-bounded: every building centroid falls in
exactly one 250 m mesh cell. So instead of a spatial intersection we:

1. compute each building's 250 m mesh code (``ops.meshcode``),
2. dedup to unique cells and resolve J-SHIS values (``sources.jshis``),
3. write the ``earthquake_*`` column group.

Honesty (same invariant as hazards/condition): a cell J-SHIS couldn't resolve
leaves the building ``earthquake_covered = false`` with null probability and
amplification — never a silent 0. With ``provider=None`` (default in Gate A,
so offline/unit builds need no network) every building is left uncovered but
the columns still exist, keeping the parquet schema stable.
"""

from __future__ import annotations

import logging

import geopandas as gpd
import numpy as np

from plateau_bridge.ops.meshcode import meshcode_250m
from plateau_bridge.sources.jshis import JSHIS_SOURCE_ID, JshisMeshProvider

log = logging.getLogger(__name__)

# J-SHIS covers the whole country, so a resolved cell is effectively a
# full-admin declaration (the strongest confidence the polygon hazards reach
# short of an explicit 想定区域 polygon).
_COVERED_CONFIDENCE = "declared_full_admin"


def apply_seismic(
    buildings: gpd.GeoDataFrame,
    provider: JshisMeshProvider | None,
    *,
    source_id: str = JSHIS_SOURCE_ID,
) -> gpd.GeoDataFrame:
    """Add the ``earthquake_*`` columns by joining centroids to J-SHIS mesh values."""
    out = buildings.copy()
    n = len(out)
    # Initialise uncovered/null so the schema is stable even with provider=None.
    out["earthquake_covered"] = False
    out["earthquake_prob_strong_shaking_30yr"] = np.nan
    out["earthquake_amplification"] = np.nan
    out["earthquake_meshcode"] = None
    out["earthquake_source_ids"] = ""
    out["earthquake_coverage_confidence"] = "unknown"
    if provider is None or n == 0:
        if provider is None:
            log.info("apply_seismic: no provider; earthquake columns left uncovered")
        return out

    # 1. centroid → 250 m mesh code (invalid centroid → None → uncovered).
    codes: list[str | None] = []
    for lat, lon in zip(out["centroid_lat"], out["centroid_lon"], strict=False):
        try:
            codes.append(meshcode_250m(float(lat), float(lon)))
        except (ValueError, TypeError):
            codes.append(None)
    out["earthquake_meshcode"] = codes

    # 2. dedup → resolve unique cells.
    values = provider.values_for([c for c in codes if c])
    log.info("apply_seismic: %d buildings, %d unique cells, %d resolved",
             n, len({c for c in codes if c}), len(values))

    # 3. assign per building (covered only when the cell resolved).
    covered = [bool(c and c in values) for c in codes]
    out["earthquake_covered"] = covered
    out["earthquake_prob_strong_shaking_30yr"] = [
        values[c].prob_strong_shaking_30yr if (c and c in values) else np.nan for c in codes
    ]
    out["earthquake_amplification"] = [
        values[c].amplification if (c and c in values and values[c].amplification is not None)
        else np.nan
        for c in codes
    ]
    out["earthquake_source_ids"] = [source_id if cov else "" for cov in covered]
    out["earthquake_coverage_confidence"] = [
        _COVERED_CONFIDENCE if cov else "unknown" for cov in covered
    ]
    return out

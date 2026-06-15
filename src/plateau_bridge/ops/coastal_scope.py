"""Terrain-certain "out of scope" (対象外) classification for coastal hazards.

For 津波 / 高潮, most of the remaining "no data" is buildings that are simply
NOT reachable by the hazard — inland wards with no designated inundation area,
or buildings on high ground (武蔵野台地 etc. sit well above any modelled
inundation). Marking those honestly as 対象外 (想定対象外, coverage_state
not_applicable) — rather than leaving them as ambiguous grey "no data" — is a
deterministic terrain exclusion, NOT a fabricated risk value.

Rule per coastal hazard H (applied AFTER official ingest, so coverage is known):
  - officially covered            → leave as-is (known official risk; not 対象外)
  - building ground elevation ≥ a conservative max-inundation reach for H
    (tsunami 20 m, storm surge 10 m — above any modelled depth band) → 対象外
  - otherwise (low-lying & uncovered) → left as no-data (HONEST: a low building
    with no official 想定 is genuinely uncertain — we must NOT call a 0 m
    waterfront ward "out of scope" just because the prefecture hasn't designated
    it. A future bathtub 推測 can fill these; elevation alone can't exclude them.)

Elevation comes from the GSI DEM (same mosaic source as the other terrain
layers). ``fetch`` is injectable so tests run offline.
"""

from __future__ import annotations

import logging

import numpy as np

from plateau_bridge.ops.hand import Fetch, _default_fetch, fetch_dem_mosaic, lonlat_to_3857

log = logging.getLogger(__name__)

# Conservative "above any modelled inundation" elevation (m, T.P.) per hazard.
# A building higher than this cannot be reached by the worst official depth band
# for these bays, so it is terrain-certain out of scope.
MAX_REACH_M: dict[str, float] = {
    "tsunami": 20.0,
    "storm_surge": 10.0,
}


def apply_coastal_scope(
    buildings,
    kind: str,
    *,
    enabled: bool = False,
    zoom: int = 14,
    fetch: Fetch = _default_fetch,
):
    """Add the ``<kind>_na`` (対象外/not-applicable) boolean column for a coastal hazard.

    Opt-in. Requires the official ``<kind>_covered`` column to already reflect
    official ingest. Sets ``<kind>_na=True`` for terrain-certain non-exposed
    buildings (see module docstring); never True where officially covered.
    """
    import geopandas as gpd

    out: gpd.GeoDataFrame = buildings.copy()
    n = len(out)
    na_col = f"{kind}_na"
    out[na_col] = False
    if not enabled or n == 0 or kind not in MAX_REACH_M:
        return out

    covered_col = f"{kind}_covered"
    covered = (
        out[covered_col].fillna(False).astype(bool).to_numpy()
        if covered_col in out.columns else np.zeros(n, dtype=bool)
    )
    reach = MAX_REACH_M[kind]

    minlon = float(out["centroid_lon"].min())
    maxlon = float(out["centroid_lon"].max())
    minlat = float(out["centroid_lat"].min())
    maxlat = float(out["centroid_lat"].max())
    elev, x0, y0, px = fetch_dem_mosaic((minlon, minlat, maxlon, maxlat), zoom, fetch=fetch)
    H, W = elev.shape

    na = [False] * n
    for i, (lon, lat) in enumerate(zip(out["centroid_lon"], out["centroid_lat"], strict=False)):
        if covered[i]:
            continue  # officially at risk → not 対象外
        try:
            x, y = lonlat_to_3857(float(lon), float(lat))
            col = int((x - x0) / px)
            row = int((y0 - y) / px)
        except (ValueError, TypeError):
            continue
        if (0 <= row < H and 0 <= col < W and np.isfinite(elev[row, col])
                and float(elev[row, col]) >= reach):
            na[i] = True  # above any modelled inundation → terrain-certain out of scope
        # else: low-lying & uncovered → leave as no-data (honest; cannot exclude)

    out[na_col] = na
    log.info("coastal_scope %s: 対象外 %d/%d (elevation ≥ %sm)", kind, sum(na), n, reach)
    return out

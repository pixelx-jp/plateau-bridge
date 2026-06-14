"""J-SHIS 250 m mesh provider — nationwide probabilistic ground motion + amplification.

J-SHIS (防災科学技術研究所) publishes the national 確率論的地震動予測地図 and 表層地盤
data on a 250 m mesh. Unlike the 5 PLATEAU hazards (designated polygons), this
covers all of Japan, so we join by **mesh code**, not spatial intersection:
compute each building's 250 m mesh code from its centroid, dedup, and look up
the J-SHIS value per unique cell.

Honesty: a mesh the API can't return (network failure / outside the published
grid / invalid value) is simply **absent from the result dict** → the building
is left ``earthquake_covered = false`` with null values. Never a silent 0.

``fetch`` is injectable so tests run offline; the default uses httpx (already a
project dependency). Verified live: meshcode 5339461132 → T30_I55_PS≈0.377, ARV≈1.27.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

PSHM_VERSION = "Y2024"
SSTRCT_VERSION = "V4"
# 30-year probability of JMA seismic intensity 6-lower (震度6弱) or above.
PROB_ATTR = "T30_I55_PS"
AMP_ATTR = "ARV"  # 表層地盤増幅率 (Vs=400m/s → surface)

JSHIS_SOURCE_ID = f"jshis-pshm-{PSHM_VERSION}"
JSHIS_ATTRIBUTION = "出典：防災科学技術研究所 J-SHIS（確率論的地震動予測地図・表層地盤データ）"

_BASE = "https://www.j-shis.bosai.go.jp/map/api"

Fetch = Callable[[str], "str | None"]


@dataclass(frozen=True)
class SeismicValue:
    prob_strong_shaking_30yr: float
    amplification: float | None


def _default_fetch(url: str) -> str | None:
    try:
        r = httpx.get(url, timeout=20.0, headers={"User-Agent": "plateau-bridge"})
        return r.text if r.status_code == 200 else None
    except Exception:  # noqa: BLE001 — any network error → no data (honest), never raise
        return None


def _parse_attr(body: str | None, attr: str) -> float | None:
    """Extract ``features[0].properties[attr]`` as a finite float, else None."""
    if not body:
        return None
    try:
        doc = json.loads(body)
    except json.JSONDecodeError:
        return None
    if doc.get("status") != "Success":
        return None
    feats = doc.get("features") or []
    if not feats:
        return None
    raw = (feats[0].get("properties") or {}).get(attr)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


class JshisMeshProvider:
    """Resolve J-SHIS seismic values per 250 m mesh code (cached, dedup-friendly)."""

    def __init__(
        self,
        *,
        fetch: Fetch = _default_fetch,
        pshm_version: str = PSHM_VERSION,
        sstrct_version: str = SSTRCT_VERSION,
    ) -> None:
        self._fetch = fetch
        self._pshm = pshm_version
        self._sstrct = sstrct_version
        self._cache: dict[str, SeismicValue | None] = {}

    def value_for(self, meshcode: str) -> SeismicValue | None:
        if meshcode in self._cache:
            return self._cache[meshcode]
        prob_url = (
            f"{_BASE}/pshm/{self._pshm}/AVR/TTL_MTTL/meshinfo.geojson"
            f"?meshcode={meshcode}&attr={PROB_ATTR}"
        )
        prob = _parse_attr(self._fetch(prob_url), PROB_ATTR)
        if prob is None or not (0.0 <= prob <= 1.0):
            self._cache[meshcode] = None  # no coverage / invalid → uncovered (honest)
            return None
        amp_url = (
            f"{_BASE}/sstrct/{self._sstrct}/meshinfo.geojson?meshcode={meshcode}&attr={AMP_ATTR}"
        )
        amp = _parse_attr(self._fetch(amp_url), AMP_ATTR)  # optional; None is fine
        val = SeismicValue(prob_strong_shaking_30yr=prob, amplification=amp)
        self._cache[meshcode] = val
        return val

    def values_for(self, meshcodes: Iterable[str]) -> dict[str, SeismicValue]:
        """Look up many mesh codes; only successful (covered) cells are returned."""
        out: dict[str, SeismicValue] = {}
        for mc in dict.fromkeys(meshcodes):  # dedup, deterministic order
            v = self.value_for(mc)
            if v is not None:
                out[mc] = v
        return out

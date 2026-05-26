"""Group geometrically-attached buildings into "complexes".

PLATEAU's CityGML authoring often models a single visual building mass as
multiple ``bldg:Building`` features — a tower on top of a podium, two
attached wings of an office block, or a row of attached townhouses each
get their own feature with their own ``measuredHeight``.

That's faithful to the data but produces a jarring colour cliff when a
height-coded renderer paints adjacent footprints with wildly different
colours (a 9 m podium next to a 243 m tower is a real case in Shibuya
2023 — see top-5 audit in `tests/test_building_complex.py`).

This module computes a stable ``complex_uid`` for every building, defined
as "the connected component when buildings are linked by near-shared
footprints", plus a ``complex_max_height`` companion that renderers use
when they want one colour per visual mass instead of one per feature.

We expose both the per-building (`height`) and per-complex
(`complex_max_height`) signals so analyst tools keep PLATEAU's per-feature
truth while front-end visualisations get the unified look users expect.

## How "attached" is defined

Two footprints belong to the same complex when their **buffered geometry
intersects** within a 0.1 m epsilon — small enough that buildings across
a narrow alley don't fuse (Tokyo alleys are typically ≥ 1 m), large
enough that PLATEAU's positional noise (~30 cm) is absorbed. We use
GEOS's ``dwithin`` predicate which avoids materialising buffered
geometries (~30× faster than ``buffer(eps).intersects``).

## Cluster ID

``complex_uid`` is the **shortest ``building_uid`` in the component**.
Stable across re-runs as long as the parquet input is sorted by
building_uid (the gate_a guarantee). Singleton buildings have
``complex_uid == building_uid``.
"""

from __future__ import annotations

import logging
from collections import defaultdict

import geopandas as gpd
import numpy as np

log = logging.getLogger(__name__)

# Touch epsilon in metres. Empirically tuned on Shibuya 2023:
# - PLATEAU positional accuracy ≈ 30 cm
# - Real-world construction tolerance: adjacent buildings frequently have
#   a 20–40 cm modelled gap that doesn't exist physically.
# - Minimum alley width in Tokyo ≈ 1 m
# - 0.5 m is a Goldilocks zone: catches the "almost touching" cases that
#   are visually one mass (which 0.1 m missed in user-reported screenshots)
#   without bridging buildings across narrow alleys.
DEFAULT_TOUCH_EPS_M = 0.5


class _UnionFind:
    """Compact union-find for int keys."""

    __slots__ = ("parent", "rank")

    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        # Iterative with path compression.
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


def compute_complexes(
    gdf: gpd.GeoDataFrame,
    *,
    eps_m: float = DEFAULT_TOUCH_EPS_M,
    metric_crs: str = "EPSG:3857",
) -> gpd.GeoDataFrame:
    """Return ``gdf`` augmented with ``complex_uid`` and ``complex_max_height``.

    Inputs must have ``geometry`` (polygon footprints), ``building_uid``,
    and ``height`` columns. Geometry CRS doesn't matter — we project to
    ``metric_crs`` (Web Mercator by default) for the metres-scale
    ``dwithin`` query.

    Singletons get ``complex_uid = building_uid`` and
    ``complex_max_height = height`` (so downstream code can unconditionally
    use the new columns).
    """
    if "building_uid" not in gdf.columns:
        raise KeyError("building_uid column required")
    if "height" not in gdf.columns:
        raise KeyError("height column required")
    n = len(gdf)
    if n == 0:
        out = gdf.copy()
        out["complex_uid"] = []
        out["complex_max_height"] = []
        return out

    # Project to metres for the dwithin query.
    src = gdf.reset_index(drop=True)
    metric = src.to_crs(metric_crs) if src.crs is not None and str(src.crs) != metric_crs else src

    pairs = gpd.sjoin(
        metric[["geometry"]],
        metric[["geometry"]],
        how="inner",
        predicate="dwithin",
        distance=eps_m,
    )
    # Drop self-pairs and dedupe (a,b) == (b,a).
    pairs = pairs[pairs.index < pairs["index_right"]]

    uf = _UnionFind(n)
    for a, b in zip(pairs.index.to_numpy(), pairs["index_right"].to_numpy(), strict=True):
        uf.union(int(a), int(b))

    # Group rows by component root.
    by_root: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        by_root[uf.find(i)].append(i)

    complex_uid = np.empty(n, dtype=object)
    complex_max_height = np.empty(n, dtype="float32")
    heights = src["height"].to_numpy()
    uids = src["building_uid"].to_numpy()
    for members in by_root.values():
        # complex_uid = shortest building_uid in the cluster (stable + readable).
        rep = min(members, key=lambda i: (len(uids[i]) if uids[i] is not None else 0, uids[i] or ""))
        cu = uids[rep]
        # max height in the cluster (NaN-safe).
        h_max = np.nanmax(heights[members]) if len(members) > 1 else heights[members[0]]
        for i in members:
            complex_uid[i] = cu
            complex_max_height[i] = h_max

    out = src.copy()
    out["complex_uid"] = complex_uid
    out["complex_max_height"] = complex_max_height
    n_clusters = len(by_root)
    n_multi = sum(1 for v in by_root.values() if len(v) > 1)
    log.info(
        "computed %d complexes from %d buildings (%d are multi-building clusters)",
        n_clusters, n, n_multi,
    )
    return out

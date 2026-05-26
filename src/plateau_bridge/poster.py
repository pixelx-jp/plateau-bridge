"""Generate the Building Age Rainbow poster from a buildings.parquet.

This is plateau-bridge's headline visual: every building in a city, coloured by
construction decade. The poster is the README screenshot — the thing that
makes someone click ⭐.

The output is a PNG (with optional SVG/PDF) that auto-embeds the project's
attribution string per `attribution.png_corner_text`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import font_manager
from matplotlib.colors import LinearSegmentedColormap, Normalize
from matplotlib.patches import Patch

from plateau_bridge.attribution import png_corner_text

# Candidate CJK fonts in install-priority order. matplotlib's bundled DejaVu
# can't render kanji, so we try to detect a system Japanese font; if none is
# present we transliterate the labels to ASCII-safe equivalents instead of
# rendering tofu boxes.
_CJK_FONT_CANDIDATES: tuple[str, ...] = (
    "Hiragino Sans", "Hiragino Kaku Gothic Pro", "Hiragino Maru Gothic Pro",
    "Yu Gothic", "Meiryo", "MS Gothic",
    "Noto Sans CJK JP", "Noto Sans JP", "Source Han Sans JP",
    "IPAGothic", "IPAexGothic", "TakaoGothic",
)


def _pick_cjk_font() -> str | None:
    """Return the family name of a system CJK font if one is installed."""
    available = {f.name for f in font_manager.fontManager.ttflist}
    for f in _CJK_FONT_CANDIDATES:
        if f in available:
            return f
    return None


_ASCII_TRANSLITERATIONS: tuple[tuple[str, str], ...] = (
    ("旧耐震", "(pre-1981 seismic code)"),
    ("渋谷区", "Shibuya-ku"),
    ("新宿区", "Shinjuku-ku"),
)


def _asciify(s: str) -> str:
    for kanji, latin in _ASCII_TRANSLITERATIONS:
        s = s.replace(kanji, latin)
    return "".join(c if ord(c) < 0x3000 else "?" for c in s)

log = logging.getLogger(__name__)


# Decade buckets shown in the legend.
DECADE_BREAKS: list[int] = [1950, 1960, 1970, 1981, 1990, 2000, 2010, 2020]
# 1981 split is intentional: pre-1981 = 旧耐震基準, the policy-relevant break.

# Warm-to-cool rainbow with a deliberate red on pre-1981 for risk emphasis.
RAINBOW = LinearSegmentedColormap.from_list(
    "plateau_age",
    [
        (0.00, "#7f1d1d"),   # ≤ 1960
        (0.20, "#dc2626"),   # 1960s
        (0.35, "#f59e0b"),   # 1970s (still old-seismic)
        (0.50, "#fbbf24"),   # 1981 cutoff
        (0.65, "#84cc16"),   # 1990s
        (0.80, "#10b981"),   # 2000s
        (1.00, "#3b82f6"),   # ≥ 2010
    ],
)


@dataclass(frozen=True)
class PosterResult:
    png_path: Path
    svg_path: Path | None
    n_buildings_rendered: int


def render_age_rainbow(
    parquet_path: Path,
    out_path: Path,
    *,
    title: str | None = None,
    subtitle: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    dpi: int = 220,
    also_svg: bool = False,
    background: str = "#0c0e12",
    color_by: str = "year_built",
) -> PosterResult:
    """Render a poster PNG (optionally SVG) from a buildings.parquet.

    Args:
        parquet_path: path to ``buildings.parquet``.
        out_path: destination PNG.
        title / subtitle: header text. Defaults to city_code + dataset_year.
        bbox: clip to ``(min_lon, min_lat, max_lon, max_lat)``. ``None`` = full extent.
        dpi: DPI for raster output.
        also_svg: also emit ``<out>.svg`` (and return its path).
        background: hex background color.
    """
    gdf = gpd.read_parquet(parquet_path)
    if color_by not in gdf.columns:
        raise ValueError(f"parquet has no {color_by!r} column")
    if bbox is not None:
        gdf = gdf.cx[bbox[0]:bbox[2], bbox[1]:bbox[3]]
    if gdf.empty:
        raise ValueError("no buildings in selection")
    # If the user picked year_built but it's fully unpopulated, bail loudly —
    # PLATEAU's bldg datasets vary a lot in attribute coverage and this is
    # the most common cause of an "all grey" poster.
    if color_by == "year_built" and not bool(gdf["year_built"].notna().any()):
        raise ValueError(
            "year_built is empty for every building in this parquet. "
            "Real PLATEAU bldg datasets (e.g. Shibuya 2023) ship with 0% "
            "yearOfConstruction coverage; try --color-by height or "
            "--color-by usage instead."
        )

    cjk_font = _pick_cjk_font()
    if cjk_font:
        plt.rcParams["font.family"] = [cjk_font, "sans-serif"]

    if title is None:
        city = gdf["city_code"].iloc[0] if "city_code" in gdf.columns else "?"
        year = gdf["dataset_year"].iloc[0] if "dataset_year" in gdf.columns else "?"
        title = f"Building Age Rainbow · {city} · {year}"
    if subtitle is None:
        subtitle = f"{len(gdf):,} buildings · pre-1981 (旧耐震) highlighted"
    if not cjk_font:
        title = _asciify(title)
        subtitle = _asciify(subtitle)

    # Aspect from bbox so the figure isn't distorted.
    minx, miny, maxx, maxy = gdf.total_bounds
    aspect = (maxx - minx) / max(1e-9, (maxy - miny))
    fig_h = 12.0
    fig_w = max(8.0, min(24.0, fig_h * aspect))
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), facecolor=background)
    ax.set_facecolor(background)
    ax.set_aspect("equal")

    col = gdf[color_by]
    has_val = col.notna()
    legend_entries: list[Patch] = []
    if color_by == "year_built":
        norm = Normalize(vmin=DECADE_BREAKS[0], vmax=DECADE_BREAKS[-1])
        if bool(has_val.any()):
            gdf[has_val].plot(
                ax=ax, column=color_by, cmap=RAINBOW, norm=norm,
                linewidth=0, antialiased=False,
            )
        for lo, hi in zip(DECADE_BREAKS[:-1], DECADE_BREAKS[1:], strict=True):
            mid = (lo + hi) / 2
            legend_entries.append(
                Patch(facecolor=RAINBOW(norm(mid)), edgecolor="none",
                      label=f"{lo}–{hi - 1}")
            )
    elif pd.api.types.is_numeric_dtype(col):
        # Continuous numeric, e.g. height: simple sequential colormap.
        # Use robust percentiles (1st-99th) so single outliers don't squash
        # the colour scale — common when PLATEAU has a -9999 sentinel or a
        # mistakenly tall feature.
        from matplotlib import colormaps
        cmap = colormaps["magma"]
        valid = col.dropna()
        vmin = float(valid.quantile(0.01)) if len(valid) else 0.0
        vmax = float(valid.quantile(0.99)) if len(valid) else 1.0
        if vmin == vmax:
            vmax = vmin + 1
        norm = Normalize(vmin=vmin, vmax=vmax)
        gdf[has_val].plot(
            ax=ax, column=color_by, cmap=cmap, norm=norm,
            linewidth=0, antialiased=False,
        )
        ticks = np.linspace(vmin, vmax, 5)
        for t in ticks:
            legend_entries.append(
                Patch(facecolor=cmap(norm(t)), edgecolor="none",
                      label=f"{color_by} {t:.0f}")
            )
    else:
        # Categorical: one colour per unique label, sorted by frequency.
        from matplotlib import colormaps
        counts = col.value_counts().head(8)
        palette = colormaps["tab10"]
        cat_to_color = {cat: palette(i / max(1, len(counts) - 1))
                         for i, cat in enumerate(counts.index)}
        for cat, color in cat_to_color.items():
            mask = col == cat
            if bool(mask.any()):
                gdf[mask].plot(ax=ax, color=color, linewidth=0, antialiased=False)
                legend_entries.append(
                    Patch(facecolor=color, edgecolor="none", label=str(cat))
                )
    # Unknown / null values in muted grey.
    if bool((~has_val).any()):
        gdf[~has_val].plot(ax=ax, color="#374151", linewidth=0, antialiased=False)
        legend_entries.append(Patch(facecolor="#374151", edgecolor="none", label="unknown"))

    ax.set_axis_off()

    # Titles.
    fig.text(0.03, 0.96, title, color="#f3f4f6", fontsize=22, fontweight="bold")
    fig.text(0.03, 0.93, subtitle, color="#9ca3af", fontsize=12)

    # Legend in figure margin.
    leg = fig.legend(
        handles=legend_entries,
        loc="lower left",
        bbox_to_anchor=(0.03, 0.02),
        frameon=False,
        ncol=min(8, max(1, len(legend_entries) // 2 + 1)),
        fontsize=9,
        handlelength=1.2,
        handleheight=1.1,
        columnspacing=1.6,
    )
    for text in leg.get_texts():
        text.set_color("#e5e7eb")

    # Attribution corner.
    fig.text(
        0.99,
        0.01,
        png_corner_text(),
        color="#9ca3af",
        fontsize=8,
        ha="right",
        va="bottom",
        alpha=0.85,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight", facecolor=background)
    svg_path = None
    if also_svg:
        svg_path = out_path.with_suffix(".svg")
        fig.savefig(svg_path, bbox_inches="tight", facecolor=background)
    plt.close(fig)

    return PosterResult(png_path=out_path, svg_path=svg_path, n_buildings_rendered=len(gdf))

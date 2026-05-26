"""User-side cache for downloaded artifacts.

Location resolved via ``platformdirs.user_cache_dir`` so it follows OS
convention (~/.cache/plateau-parquet on Linux, ~/Library/Caches/plateau-parquet
on macOS, %LOCALAPPDATA%\\plateau-parquet on Windows). Override with the
``PLATEAU_CACHE_DIR`` env var. Layout::

    <cache_dir>/
      datasets/<sha16>/...        # unzipped CityGML/hazard sources
      bundles/<bundle>.tar.zst    # pre-built bundles fetched via `cache add`
      builds/<city>/<year>/...    # reserved; not currently used by CLI
"""

from __future__ import annotations

from pathlib import Path

from plateau_parquet.config import load_settings


def datasets_root() -> Path:
    p = load_settings().cache_dir / "datasets"
    p.mkdir(parents=True, exist_ok=True)
    return p


def build_dir(city_code: str, dataset_year: int) -> Path:
    p = load_settings().cache_dir / "builds" / city_code / str(dataset_year)
    p.mkdir(parents=True, exist_ok=True)
    return p

"""Runtime configuration. Keep this small; CLI flags > env > defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_cache_dir


@dataclass(frozen=True)
class Settings:
    cache_dir: Path
    converter_bin: str
    """Path or name of MIERUNE plateau-gis-converter on $PATH."""
    tippecanoe_bin: str
    """Path or name of tippecanoe on $PATH (required for PMTiles writer)."""
    http_timeout_s: float = 60.0
    max_concurrency: int = 4


def load_settings() -> Settings:
    cache = Path(os.environ.get("PLATEAU_CACHE_DIR") or user_cache_dir("plateau-parquet"))
    cache.mkdir(parents=True, exist_ok=True)
    return Settings(
        cache_dir=cache,
        converter_bin=os.environ.get("PLATEAU_CONVERTER_BIN", "plateau-gis-converter"),
        tippecanoe_bin=os.environ.get("PLATEAU_TIPPECANOE_BIN", "tippecanoe"),
    )

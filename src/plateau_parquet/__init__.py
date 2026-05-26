"""plateau-parquet: a trustworthy building index + hazard intersection pipeline for PLATEAU."""

__version__ = "0.1.0"

from plateau_parquet.schema import Building, HazardKind, Manifest

__all__ = ["Building", "HazardKind", "Manifest", "__version__"]

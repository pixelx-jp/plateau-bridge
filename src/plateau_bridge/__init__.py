"""plateau-bridge: a trustworthy building index + hazard intersection pipeline for PLATEAU."""

__version__ = "0.1.1"

from plateau_bridge.schema import Building, HazardKind, Manifest

__all__ = ["Building", "HazardKind", "Manifest", "__version__"]

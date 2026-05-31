"""Wrapper around MIERUNE's ``nusamai`` CLI (the binary for plateau-gis-converter).

The Rust converter is the only authoritative parser for PLATEAU's i-UR
extensions, and it's an order of magnitude faster than anything we'd write.
This module is the only place that knows the converter exists.

CLI shape (verified against
https://github.com/MIERUNE/plateau-gis-converter):

    nusamai <glob>... --sink <format> --output <path> [-t use_lod=...] [-o ...]

- Inputs are **positional** glob args, not ``--input``.
- ``--sink`` and ``--output`` are required; supported sinks include
  ``geojson``, ``3d-tiles``, ``mvt``, ``gpkg``, etc.
- Producing both GeoJSON and 3D Tiles requires **two separate invocations**
  — the CLI has no "also" mode.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd

log = logging.getLogger(__name__)

# GDAL OGR's GeoJSON driver caps single-feature size at 200 MB by default,
# which fails on PLATEAU's prefecture-wide tsunami/storm_surge layers where
# nusamai emits one giant FeatureCollection per kind (Osaka 大阪市 tsunami
# clocks in at ~180 MB per feature). Setting to 0 disables the limit.
# We set this at import time so every pyogrio/fiona read picks it up.
os.environ.setdefault("OGR_GEOJSON_MAX_OBJ_SIZE", "0")


class ConverterNotFound(RuntimeError):
    """Raised when nusamai isn't on $PATH."""


@dataclass(frozen=True)
class ConvertResult:
    geojson_path: Path
    tiles3d_dir: Path | None


def _resolve_bin(bin_name: str) -> str:
    resolved = shutil.which(bin_name)
    if not resolved and bin_name != "nusamai":
        resolved = shutil.which("nusamai")
    if not resolved:
        raise ConverterNotFound(
            f"{bin_name!r} (or nusamai) not found on $PATH. Install from "
            "https://github.com/MIERUNE/plateau-gis-converter/releases"
        )
    return resolved


_URO_31_NS = "https://www.geospatial.jp/iur/uro/3.1"
_BLDG_NS = "http://www.opengis.net/citygml/building/2.0"

# Common PLATEAU namespaces — registering them keeps ElementTree's output
# using the expected prefixes (otherwise it emits `ns0:` / `ns1:` which
# nusamai accepts but produces visually-noisy diffs and confuses humans).
_PLATEAU_NS = {
    "gml": "http://www.opengis.net/gml",
    "core": "http://www.opengis.net/citygml/2.0",
    "bldg": _BLDG_NS,
    "app": "http://www.opengis.net/citygml/appearance/2.0",
    "gen": "http://www.opengis.net/citygml/generics/2.0",
    "uro": _URO_31_NS,
    "xAL": "urn:oasis:names:tc:ciq:xsdschema:xAL:2.0",
    "xlink": "http://www.w3.org/1999/xlink",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}
for _prefix, _uri in _PLATEAU_NS.items():
    ET.register_namespace(_prefix, _uri)


def _sanitise_uro_duplicates(src: Path, dst: Path) -> int:
    """Strip duplicate ``uro:bldgRealEstateIDAttribute`` from a GML file.

    PLATEAU's published bldg data for at least Toshima-ku 13116, Kita-ku
    13117, and Itabashi-ku 13119 contains 2 ``uro:bldgRealEstateIDAttribute``
    children under some Building elements. uro 3.1's schema allows at
    most one, and nusamai's parser refuses the file outright with::

        ParseError(SchemaViolation(
          "uro:bldgRealEstateIDAttribute/uro:RealEstateIDAttribute "
          "must not occur two or more times."))

    We keep the first occurrence per Building, drop the rest, and write
    a sanitised copy. The first occurrence is what every other ward's
    valid data carries, so this is the most conservative fix.

    Returns the number of duplicate elements removed (0 if the file is
    clean, in which case ``dst`` is *not* written — the caller should
    fall back to the original ``src``).

    Raises ``ET.ParseError`` on malformed XML; the converter will then
    surface the underlying problem rather than us silently dropping the
    file.
    """
    tree = ET.parse(src)
    root = tree.getroot()
    removed = 0
    target = f"{{{_URO_31_NS}}}bldgRealEstateIDAttribute"
    for building in root.iter(f"{{{_BLDG_NS}}}Building"):
        extras = [c for c in list(building) if c.tag == target]
        if len(extras) <= 1:
            continue
        for el in extras[1:]:
            building.remove(el)
            removed += 1
    if removed == 0:
        return 0
    dst.parent.mkdir(parents=True, exist_ok=True)
    tree.write(dst, xml_declaration=True, encoding="utf-8")
    return removed


def _file_contains(path: Path, needle: bytes, chunk_size: int = 1 << 20) -> bool:
    """Stream-scan ``path`` for ``needle`` without loading it into memory.

    PLATEAU building GML bundles can be multiple GB; the previous marker check
    did ``path.read_text()``, materializing the entire file as a Python string
    (plus a UTF-8 decode) just to run one substring search. This reads in 1 MiB
    chunks, returns as soon as the marker is found, and keeps only a small
    overlap so a marker straddling a chunk boundary is still detected. ``needle``
    must be ASCII (our markers are), so a byte search matches the old UTF-8 one.
    """
    overlap = len(needle) - 1
    with path.open("rb") as fh:
        tail = b""
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                return False
            buf = tail + chunk
            if needle in buf:
                return True
            tail = buf[-overlap:] if overlap else b""


def _maybe_sanitise_inputs(gml_args: list[str], work_dir: Path) -> list[str]:
    """Pre-process inputs to strip nusamai-breaking schema violations.

    For each input GML, if it contains the duplicate-uro-attr defect
    documented in :func:`_sanitise_uro_duplicates`, write a clean copy
    under ``work_dir/sanitised/`` and substitute that path. Otherwise
    keep the original path.

    Currently only inspects ``.gml`` files (hazard XSDs use ``.xml`` and
    haven't shown this defect). Cheap to extend if more violations appear.
    """
    out: list[str] = []
    sanitised_dir = work_dir / "sanitised"
    n_files_changed = 0
    n_dupes_total = 0
    for p in gml_args:
        src = Path(p)
        if src.suffix.lower() != ".gml":
            out.append(p)
            continue
        # Cheap pre-check: only files that contain the marker text are worth
        # parsing. Saves an XML parse on every clean file (~95 % of them), and
        # streams the scan so we never hold a multi-GB file in memory at once.
        try:
            if not _file_contains(src, b"bldgRealEstateIDAttribute"):
                out.append(p)
                continue
        except OSError:
            out.append(p)
            continue
        dst = sanitised_dir / src.name
        try:
            n = _sanitise_uro_duplicates(src, dst)
        except ET.ParseError as e:
            log.warning("could not pre-sanitise %s: %s; passing through", src.name, e)
            out.append(p)
            continue
        if n > 0:
            n_files_changed += 1
            n_dupes_total += n
            out.append(str(dst))
        else:
            out.append(p)
    if n_files_changed:
        log.info(
            "sanitised %d file(s) (%d duplicate uro:bldgRealEstateIDAttribute removed)",
            n_files_changed,
            n_dupes_total,
        )
    return out


def _expand_gml_glob(citygml_input: Path) -> list[str]:
    """Accept either a directory of .gml files or a single .gml/glob string.

    Returns the expanded shell arguments. We expand in Python rather than
    relying on the shell so behaviour is identical across zsh / bash / fish.
    """
    if citygml_input.is_dir():
        files = sorted(citygml_input.rglob("*.gml"))
        if not files:
            raise FileNotFoundError(f"no .gml under {citygml_input}")
        return [str(p) for p in files]
    if citygml_input.is_file():
        return [str(citygml_input)]
    matches = glob.glob(str(citygml_input))
    if not matches:
        raise FileNotFoundError(f"no files match {citygml_input}")
    return sorted(matches)


def _run_nusamai(
    bin_path: str,
    gml_args: list[str],
    sink: str,
    output: Path,
    *,
    use_lod: str = "max_lod",
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    gml_args = _maybe_sanitise_inputs(gml_args, output.parent)
    cmd = [bin_path, *gml_args, "--sink", sink, "--output", str(output), "-t", f"use_lod={use_lod}"]
    log.info("nusamai: --sink %s --output %s  (%d input gml)", sink, output, len(gml_args))
    log.debug("full cmd: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)


def convert_buildings(
    citygml_input: Path,
    out_dir: Path,
    *,
    converter_bin: str = "nusamai",
    emit_3dtiles: bool = True,
    use_lod: str = "max_lod",
    feature_name: str = "Building",
) -> ConvertResult:
    """Run nusamai (GeoJSON + optional 3D Tiles) against a CityGML input.

    The ``geojson`` sink writes a *directory* containing one ``<FeatureType>.geojson``
    file per CityGML feature class (e.g. ``Building.geojson``,
    ``BuildingInstallation.geojson``). We return the path of the requested
    ``feature_name`` file (default ``Building``); for hazard layers callers
    override ``feature_name`` to e.g. ``FloodingRiskAttribute``.

    Args:
        citygml_input: directory of .gml files, a single .gml file, or a
            glob string (e.g. ``.../bldg/*.gml``).
        out_dir: output directory. nusamai will write a subdir
            ``geojson/`` under it; 3D Tiles go under ``3dtiles/``.
        converter_bin: bin name or path. Falls back to ``nusamai`` if the
            named binary isn't found — supports older configs that still
            reference ``plateau-gis-converter``.
        emit_3dtiles: also produce 3D Tiles 1.1 in ``out_dir/3dtiles``.
        use_lod: ``max_lod`` / ``min_lod`` / ``textured_max_lod``.
        feature_name: which feature class file to read back. Default
            ``Building`` matches the bldg module.
    """
    bin_path = _resolve_bin(converter_bin)
    out_dir.mkdir(parents=True, exist_ok=True)

    geojson_dir = out_dir / "geojson"
    target = geojson_dir / f"{feature_name}.geojson"
    # Idempotence: skip the expensive re-conversion if the target already exists.
    # nusamai conversion of Shibuya bldg (90k features) takes ~2 min; not worth
    # repeating when the underlying GML hasn't changed.
    if not target.exists():
        gml_args = _expand_gml_glob(citygml_input)
        _run_nusamai(bin_path, gml_args, "geojson", geojson_dir, use_lod=use_lod)
    else:
        log.info("reusing cached %s", target)
    target = geojson_dir / f"{feature_name}.geojson"
    if not target.exists():
        # Some nusamai versions write directly into a file rather than a dir
        # (when only one feature class is produced); accept that too.
        alt = out_dir / "geojson.geojson"
        if alt.exists():
            target = alt
        else:
            existing = list(geojson_dir.glob("*.geojson"))
            raise RuntimeError(
                f"nusamai did not write {target}; geojson dir contains "
                f"{[p.name for p in existing]}"
            )
    geojson_out = target

    tiles_dir: Path | None = None
    if emit_3dtiles:
        tiles_dir = out_dir / "3dtiles"
        if not (tiles_dir / "tileset.json").exists():
            tile_gml_args = _expand_gml_glob(citygml_input)
            _run_nusamai(bin_path, tile_gml_args, "3dtiles", tiles_dir, use_lod=use_lod)
        else:
            log.info("reusing cached %s", tiles_dir / "tileset.json")
        if not (tiles_dir / "tileset.json").exists():
            log.warning("3dtiles sink ran but no tileset.json in %s", tiles_dir)
            tiles_dir = None

    return ConvertResult(geojson_path=geojson_out, tiles3d_dir=tiles_dir)


def convert_pmtiles(
    citygml_input: Path,
    pmtiles_path: Path,
    *,
    converter_bin: str = "nusamai",
    use_lod: str = "max_lod",
) -> Path:
    """Produce a PMTiles output directly with nusamai's native pmtiles sink.

    This replaces the tippecanoe-based path; nusamai writes PMTiles directly
    from CityGML, so we save a GeoJSON intermediate and avoid an extra binary
    dependency.
    """
    bin_path = _resolve_bin(converter_bin)
    gml_args = _expand_gml_glob(citygml_input)
    _run_nusamai(bin_path, gml_args, "pmtiles", pmtiles_path, use_lod=use_lod)
    if not pmtiles_path.exists():
        raise RuntimeError(f"nusamai did not write {pmtiles_path}")
    return pmtiles_path


def load_geojson(path: Path) -> gpd.GeoDataFrame:
    """Read nusamai's GeoJSON output into a 2D GeoDataFrame in WGS84.

    Two real-data quirks handled here:

    1. nusamai geometry carries a Z coordinate (PLATEAU is 3D). We force it
       to 2D at load time since the rest of the pipeline is footprint-only;
       Z survives in the 3D Tiles output.
    2. gml_id lives on the GeoJSON ``Feature.id`` and is also duplicated in
       ``properties.id``. pyogrio surfaces the latter as column ``id``.
       We rename it to ``gml_id`` and drop the duplicate.
    """
    gdf = gpd.read_file(path)
    if "id" in gdf.columns:
        gdf = gdf.rename(columns={"id": "gml_id"})
    if gdf.crs is None:
        log.warning("no CRS on %s, assuming EPSG:4326", path)
        gdf.set_crs(4326, inplace=True)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)
    # Force geometry to 2D (drop Z). pyarrow GeoParquet supports 3D but our
    # spatial joins assume 2D and downstream PMTiles/FGB are 2D anyway.
    from shapely.ops import transform
    def _drop_z(x, y, z=None):  # noqa: ARG001
        return (x, y)
    gdf["geometry"] = gdf.geometry.apply(lambda g: transform(_drop_z, g) if g is not None else None)
    return gdf


def read_tileset_index(tiles3d_dir: Path) -> dict:
    """Return the top-level tileset.json as a dict."""
    return json.loads((tiles3d_dir / "tileset.json").read_text(encoding="utf-8"))

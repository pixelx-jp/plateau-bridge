"""Parser for PLATEAU bundle metadata XML.

Each PLATEAU CityGML bundle ships per-theme metadata at
``metadata/udx_<city>_<year>_<theme>_op.xml``. The file is a Japanese
JMP20 ISO-19115 profile.

This module extracts the bits useful for *coverage roadmap* work
(see ``docs/COVERAGE_ROADMAP.md``):

- The free-text source-document list under ``<descriptiveKeywords>``
  — the names of the 想定区域 / 調査範囲 source documents that
  feed each hazard layer.
- The bounding box under ``<EX_GeographicBoundingBox>`` — useful as
  a coarse sanity check, but per-prefecture (not per-city) so not a
  direct coverage upgrade.

The module is read-only and best-effort; missing fields return
``None``-shaped values rather than raising, so callers can probe
opportunistically.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

log = logging.getLogger(__name__)

# JMP20 default namespace. PLATEAU files declare it as the default,
# so element names appear unprefixed when ET parses them.
NS = {"jmp20": "http://zgate.gsi.go.jp/ch/jmp/"}


@dataclass(frozen=True)
class BBox4326:
    west: float
    east: float
    south: float
    north: float


@dataclass(frozen=True)
class MetadataExtract:
    """What we pulled from one ``metadata/udx_*_<theme>_op.xml`` file."""

    title: str | None
    """First ``<citation><title>`` — e.g. ``洪水浸水想定区域3Dモデル_13115_city_2024_op``."""
    bbox: BBox4326 | None
    """Top-level identification bounding box (prefecture-scale in practice)."""
    source_documents: tuple[str, ...]
    """Free-text source-document names — the keywords feeding hazard layers.

    Example entries::

        利根川水系利根川洪水浸水想定区域図（平成29年7月20日）国土交通省関東地方整備局…
        多摩川水系多摩川、浅川、大栗川洪水浸水想定区域図（平成28年5月30日）…

    Strip the date / publisher tail to get the canonical document name,
    which is what a future ``coverage_sources.json`` mapping table
    should be keyed on.
    """


_SOURCE_DOC_TAIL = re.compile(r"（[^）]*）.*$")
# Matches `（平成29年7月20日）国土交通省関東地方整備局…` — strip to bare name.


def parse_metadata_xml(path: Path) -> MetadataExtract | None:
    """Parse one PLATEAU metadata XML. Returns ``None`` on read / parse error."""
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError) as e:
        log.warning("failed to parse metadata xml %s: %s", path, e)
        return None
    root = tree.getroot()

    title = _findtext(root, ".//jmp20:citation/jmp20:title")
    bbox = _parse_bbox(root)
    source_docs = _parse_source_documents(root)

    return MetadataExtract(title=title, bbox=bbox, source_documents=source_docs)


def find_metadata_files(dataset_root: Path) -> list[Path]:
    """Locate every ``metadata/udx_*_op.xml`` under a dataset root."""
    meta_dir = dataset_root / "metadata"
    if not meta_dir.is_dir():
        return []
    return sorted(p for p in meta_dir.glob("udx_*_op.xml") if p.is_file())


def canonicalise_source_document(s: str) -> str:
    """Strip date / publisher tail so the document can be used as a map key.

    Input::

        利根川水系利根川洪水浸水想定区域図（平成29年7月20日）国土交通省関東地方整備局…

    Output::

        利根川水系利根川洪水浸水想定区域図
    """
    return _SOURCE_DOC_TAIL.sub("", s).strip()


# --- internals ---------------------------------------------------------------


def _findtext(root: ET.Element, xpath: str) -> str | None:
    el = root.find(xpath, NS)
    if el is None or el.text is None:
        return None
    return el.text.strip() or None


def _parse_bbox(root: ET.Element) -> BBox4326 | None:
    # Take the *first* EX_GeographicBoundingBox under MD_DataIdentification —
    # subsequent ones live inside dataQualityInfo for finer-grained scopes.
    bb = root.find(".//jmp20:MD_DataIdentification//jmp20:EX_GeographicBoundingBox", NS)
    if bb is None:
        return None
    try:
        west = float(_findtext(bb, "jmp20:westBoundLongitude") or "")
        east = float(_findtext(bb, "jmp20:eastBoundLongitude") or "")
        south = float(_findtext(bb, "jmp20:southBoundLatitude") or "")
        north = float(_findtext(bb, "jmp20:northBoundLatitude") or "")
    except ValueError:
        return None
    return BBox4326(west=west, east=east, south=south, north=north)


def _parse_source_documents(root: ET.Element) -> tuple[str, ...]:
    """Pull every ``<keyword>`` whose parent ``<MD_Keywords>`` has type 005.

    PLATEAU's metadata convention: keyword-type ``005`` = subject/topic,
    ``002`` = place. The source-document names sit under 005.
    """
    out: list[str] = []
    for kw_block in root.findall(".//jmp20:descriptiveKeywords/jmp20:MD_Keywords", NS):
        kw_type = _findtext(kw_block, "jmp20:type")
        if kw_type != "005":
            continue
        for kw in kw_block.findall("jmp20:keyword", NS):
            if kw.text:
                t = kw.text.strip()
                # Source documents are long & contain the "想定区域図" /
                # "予想区域図" suffix. Other 005 keywords (e.g. LOD1, 防災)
                # are short topic tags — filter them out.
                if len(t) > 12 and ("想定区域図" in t or "予想区域図" in t or "調査範囲" in t):
                    out.append(t)
    # Dedupe preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for s in out:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    return tuple(deduped)

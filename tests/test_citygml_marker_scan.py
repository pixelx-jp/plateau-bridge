"""Tests for the streaming GML marker pre-check (`_file_contains`).

The marker scan replaced a `path.read_text()` (which loaded multi-GB GML files
fully into memory) with a chunked byte scan. The behaviour MUST stay identical
to a plain ``needle in contents`` check — including the tricky case where the
marker straddles a read-chunk boundary, which the overlap logic exists to
handle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from plateau_bridge.sources.citygml import _file_contains

MARKER = b"bldgRealEstateIDAttribute"


def _write(tmp_path: Path, data: bytes) -> Path:
    p = tmp_path / "sample.gml"
    p.write_bytes(data)
    return p


def test_empty_file_returns_false(tmp_path: Path) -> None:
    assert _file_contains(_write(tmp_path, b""), MARKER) is False


def test_marker_absent_returns_false(tmp_path: Path) -> None:
    assert _file_contains(_write(tmp_path, b"x" * 10_000), MARKER) is False


def test_marker_present_single_chunk(tmp_path: Path) -> None:
    data = b"<Building>" + MARKER + b"</Building>"
    assert _file_contains(_write(tmp_path, data), MARKER) is True


def test_marker_at_end_of_file(tmp_path: Path) -> None:
    assert _file_contains(_write(tmp_path, b"y" * 500 + MARKER), MARKER) is True


def test_partial_marker_is_not_a_match(tmp_path: Path) -> None:
    # A prefix of the marker (no full occurrence) must not register as found.
    assert _file_contains(_write(tmp_path, b"bldgRealEstateID" * 100), MARKER) is False


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 8, 13, 24, 25, 26])
def test_marker_straddling_chunk_boundary(tmp_path: Path, chunk_size: int) -> None:
    # With a chunk_size smaller than the marker, the needle necessarily spans
    # multiple reads; the overlap retention must still reassemble it. Sweep
    # several sizes including ones just below/at/above the marker length.
    data = b"AAAA" + MARKER + b"BBBB"
    assert _file_contains(_write(tmp_path, data), MARKER, chunk_size=chunk_size) is True


@pytest.mark.parametrize("chunk_size", [1, 3, 8, 64])
def test_chunked_scan_matches_substring_semantics(tmp_path: Path, chunk_size: int) -> None:
    # Property check: the streaming scan agrees with a plain `in` over the full
    # bytes, regardless of chunk size, for both hit and miss inputs.
    for data in (
        b"",
        b"no marker here",
        b"prefix" + MARKER,
        MARKER + b"suffix",
        b"a" * 33 + MARKER + b"b" * 17,
        b"bldgRealEstate",  # near-miss
    ):
        p = _write(tmp_path, data)
        assert _file_contains(p, MARKER, chunk_size=chunk_size) is (MARKER in data)

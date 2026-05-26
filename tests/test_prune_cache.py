"""Regression for `cache_path_for_url` — pins the URL→path contract that
`plateau build --prune-cache` relies on. If the hashing scheme drifts,
prune would silently no-op while the cache grows; this test catches that.
"""
from __future__ import annotations

from pathlib import Path

from plateau_bridge.sources.download import _url_key, cache_path_for_url


def test_cache_path_matches_extraction_target(tmp_path: Path) -> None:
    url = "https://example.com/plateau/13113_shibuya.zip"
    expected = tmp_path / _url_key(url)
    assert cache_path_for_url(url, tmp_path) == expected


def test_cache_path_stable_per_url(tmp_path: Path) -> None:
    url = "https://example.com/13113_shibuya.zip"
    assert cache_path_for_url(url, tmp_path) == cache_path_for_url(url, tmp_path)


def test_cache_path_differs_per_url(tmp_path: Path) -> None:
    a = cache_path_for_url("https://example.com/a.zip", tmp_path)
    b = cache_path_for_url("https://example.com/b.zip", tmp_path)
    assert a != b

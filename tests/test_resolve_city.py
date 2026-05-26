"""Pin slug-or-code city resolution.

Every CLI subcommand that takes a city goes through ``resolve_city``,
so this is the single contract that determines whether
``plateau cache add shibuya`` and ``plateau cache add 13113`` are
equivalent. If the slug table drifts or the JIS-code passthrough
breaks, every command silently breaks.
"""
from __future__ import annotations

import pytest

from plateau_parquet.catalog import resolve_city


def test_slug_resolves_to_jis_code() -> None:
    assert resolve_city("shibuya") == "13113"
    assert resolve_city("osaka") == "27100"
    assert resolve_city("sapporo") == "01100"


def test_slug_is_case_insensitive() -> None:
    assert resolve_city("Shibuya") == "13113"
    assert resolve_city("SHIBUYA") == "13113"
    assert resolve_city("  Yokohama  ") == "14100"


def test_jis_code_passthrough() -> None:
    assert resolve_city("13113") == "13113"
    # Cities with leading zero (Sapporo) must not lose it
    assert resolve_city("01100") == "01100"


def test_all_29_cities_in_table() -> None:
    """Every catalog city must be resolvable by slug — no half-coverage."""
    from plateau_parquet.catalog import _SLUG_TO_CODE, load_registry
    catalog_codes = {code for code, _year in load_registry()}
    slug_codes = set(_SLUG_TO_CODE.values())
    missing = catalog_codes - slug_codes
    assert not missing, f"slugs missing for: {missing}"


def test_unknown_raises_helpful_error() -> None:
    with pytest.raises(KeyError) as exc_info:
        resolve_city("manhattan")
    msg = str(exc_info.value)
    assert "manhattan" in msg
    # Helpful error must list at least one known slug
    assert "shibuya" in msg or "osaka" in msg


def test_partial_jis_code_rejected() -> None:
    # 4-digit / non-numeric strings that aren't slugs must fail loudly
    # rather than silently passing through as "JIS codes".
    with pytest.raises(KeyError):
        resolve_city("1311")
    with pytest.raises(KeyError):
        resolve_city("131130")  # 6 digits

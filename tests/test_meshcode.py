"""250 m mesh code (JIS X 0410) — the J-SHIS join key."""

from __future__ import annotations

import pytest

from plateau_bridge.ops.meshcode import meshcode_250m


def test_tokyo_station_known_value():
    # Tokyo Station — 3rd-mesh prefix 53394611 matches PLATEAU building_uid usage.
    code = meshcode_250m(35.6812, 139.7671)
    assert code == "5339461132"
    assert code[:8] == "53394611"  # 1km 3rd-mesh prefix
    assert len(code) == 10


def test_quarter_digit_in_range():
    # m (digit 9) and n (digit 10) are quarter selectors in 1..4.
    for lat, lon in [(35.0, 139.0), (43.06, 141.35), (34.70, 135.50), (33.59, 130.40)]:
        code = meshcode_250m(lat, lon)
        assert len(code) == 10
        assert code[8] in "1234"
        assert code[9] in "1234"


def test_deterministic_and_dedups_within_cell():
    # Same coordinate → identical code (the dedup key must be stable).
    assert meshcode_250m(35.6812, 139.7671) == meshcode_250m(35.6812, 139.7671)
    # Two points ~20 m apart fall in the same 250 m cell → same full code,
    # which is exactly why centroid→mesh dedup collapses many buildings to
    # far fewer unique J-SHIS lookups.
    assert meshcode_250m(35.6800, 139.7700) == meshcode_250m(35.68015, 139.77015)


def test_distant_points_differ_in_1km_prefix():
    # ~1 km apart → different 3rd-mesh (8-digit) prefix.
    assert meshcode_250m(35.6800, 139.7700)[:8] != meshcode_250m(35.6900, 139.7800)[:8]


def test_rejects_out_of_range_and_nonfinite():
    with pytest.raises(ValueError):
        meshcode_250m(0.0, 0.0)  # outside Japan
    with pytest.raises(ValueError):
        meshcode_250m(float("nan"), 139.0)

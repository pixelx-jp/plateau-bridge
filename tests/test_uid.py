from __future__ import annotations

import pytest

from plateau_bridge.ops.uid import batch_uids, make_uid, parse_uid


def test_make_uid_roundtrip() -> None:
    uid = make_uid("13113", 2024, "shibuya_bldg_2024", "bldg_xyz")
    assert uid == "13113/2024/shibuya_bldg_2024/bldg_xyz"
    assert parse_uid(uid) == ("13113", 2024, "shibuya_bldg_2024", "bldg_xyz")


def test_uid_forbids_slash_in_part() -> None:
    with pytest.raises(ValueError):
        make_uid("13/113", 2024, "x", "y")


def test_batch_uids_strict_zip() -> None:
    out = batch_uids(
        city_code="13113",
        dataset_year=2024,
        source_file_ids=["a", "b"],
        gml_ids=["g1", "g2"],
    )
    assert out == [
        "13113/2024/a/g1",
        "13113/2024/b/g2",
    ]

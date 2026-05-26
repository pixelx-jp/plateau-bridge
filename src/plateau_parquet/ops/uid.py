"""building_uid generation.

Per plan §schema: ``building_uid = {city_code}/{dataset_year}/{source_file_id}/{gml_id}``.
This is a **versioned** key — it changes when the dataset year changes. A future
``canonical_building_id`` (geometry-stable across years) will live alongside,
but is explicitly out of scope for v1.
"""

from __future__ import annotations

from collections.abc import Iterable


def make_uid(city_code: str, dataset_year: int, source_file_id: str, gml_id: str) -> str:
    """Build a single uid. All four parts are required.

    We do not URL-encode here — gml_id is already a stable identifier without
    delimiters in the PLATEAU dataset. We assert no slashes to keep parseability.
    """
    for part_name, part in (
        ("city_code", city_code),
        ("source_file_id", source_file_id),
        ("gml_id", gml_id),
    ):
        if "/" in part:
            raise ValueError(f"{part_name} must not contain '/': {part!r}")
    return f"{city_code}/{dataset_year}/{source_file_id}/{gml_id}"


def parse_uid(uid: str) -> tuple[str, int, str, str]:
    parts = uid.split("/")
    if len(parts) != 4:
        raise ValueError(f"malformed building_uid: {uid!r}")
    city, year, file_id, gml = parts
    return city, int(year), file_id, gml


def batch_uids(
    *,
    city_code: str,
    dataset_year: int,
    source_file_ids: Iterable[str],
    gml_ids: Iterable[str],
) -> list[str]:
    return [
        make_uid(city_code, dataset_year, sf, gid)
        for sf, gid in zip(source_file_ids, gml_ids, strict=True)
    ]

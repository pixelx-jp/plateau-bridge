"""Regression for `_sanitise_uro_duplicates`.

PLATEAU's published bldg data for Toshima 13116, Kita 13117, and
Itabashi 13119 violates uro 3.1's "at most one
``bldgRealEstateIDAttribute`` per Building" rule. nusamai refuses to
parse the file. We pre-sanitise by keeping the first occurrence per
Building.

This test pins both the detection and the "no-op on clean input"
property so we don't regress to either:
  - reverting the workaround and breaking 3 cities again, or
  - "fixing" clean wards by mutating their data unnecessarily.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from plateau_bridge.sources.citygml import _maybe_sanitise_inputs, _sanitise_uro_duplicates

CLEAN_GML = dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <core:CityModel
      xmlns:core="http://www.opengis.net/citygml/2.0"
      xmlns:bldg="http://www.opengis.net/citygml/building/2.0"
      xmlns:uro="https://www.geospatial.jp/iur/uro/3.1"
      xmlns:gml="http://www.opengis.net/gml">
      <core:cityObjectMember>
        <bldg:Building gml:id="bldg_clean_1">
          <uro:bldgRealEstateIDAttribute>
            <uro:RealEstateIDAttribute>
              <uro:realEstateIDOfBuilding>X-1</uro:realEstateIDOfBuilding>
            </uro:RealEstateIDAttribute>
          </uro:bldgRealEstateIDAttribute>
        </bldg:Building>
      </core:cityObjectMember>
    </core:CityModel>
""")

DIRTY_GML = dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <core:CityModel
      xmlns:core="http://www.opengis.net/citygml/2.0"
      xmlns:bldg="http://www.opengis.net/citygml/building/2.0"
      xmlns:uro="https://www.geospatial.jp/iur/uro/3.1"
      xmlns:gml="http://www.opengis.net/gml">
      <core:cityObjectMember>
        <bldg:Building gml:id="bldg_dirty_1">
          <uro:bldgRealEstateIDAttribute>
            <uro:RealEstateIDAttribute>
              <uro:realEstateIDOfBuilding>A-1</uro:realEstateIDOfBuilding>
            </uro:RealEstateIDAttribute>
          </uro:bldgRealEstateIDAttribute>
          <uro:bldgRealEstateIDAttribute>
            <uro:RealEstateIDAttribute>
              <uro:realEstateIDOfBuilding>A-2</uro:realEstateIDOfBuilding>
            </uro:RealEstateIDAttribute>
          </uro:bldgRealEstateIDAttribute>
        </bldg:Building>
      </core:cityObjectMember>
    </core:CityModel>
""")


def test_clean_file_no_op(tmp_path: Path) -> None:
    src = tmp_path / "clean.gml"
    dst = tmp_path / "out" / "clean.gml"
    src.write_text(CLEAN_GML, encoding="utf-8")
    n = _sanitise_uro_duplicates(src, dst)
    assert n == 0
    # When nothing changes, the dst is NOT written — caller falls back to src.
    assert not dst.exists()


def test_dirty_file_drops_extras(tmp_path: Path) -> None:
    src = tmp_path / "dirty.gml"
    dst = tmp_path / "out" / "dirty.gml"
    src.write_text(DIRTY_GML, encoding="utf-8")
    n = _sanitise_uro_duplicates(src, dst)
    assert n == 1
    assert dst.exists()
    body = dst.read_text(encoding="utf-8")
    # First occurrence kept (A-1), second removed (A-2 gone).
    assert "A-1" in body
    assert "A-2" not in body


def test_maybe_sanitise_inputs_substitutes_paths(tmp_path: Path) -> None:
    clean = tmp_path / "clean.gml"
    clean.write_text(CLEAN_GML, encoding="utf-8")
    dirty = tmp_path / "dirty.gml"
    dirty.write_text(DIRTY_GML, encoding="utf-8")
    other = tmp_path / "metadata.xml"
    other.write_text("<x/>", encoding="utf-8")

    work_dir = tmp_path / "_work"
    out = _maybe_sanitise_inputs([str(clean), str(dirty), str(other)], work_dir)
    assert out[0] == str(clean)              # clean passes through
    assert "sanitised" in out[1]              # dirty substituted
    assert Path(out[1]).exists()
    assert out[2] == str(other)               # non-.gml passes through


def test_maybe_sanitise_skips_files_without_marker(tmp_path: Path) -> None:
    """If the file doesn't even contain `bldgRealEstateIDAttribute`,
    we skip the XML parse entirely — important for hot-path performance
    on the ~95 % of files that don't need fixing."""
    src = tmp_path / "no_marker.gml"
    src.write_text("<root xmlns:bldg='http://www.opengis.net/citygml/building/2.0'/>")
    out = _maybe_sanitise_inputs([str(src)], tmp_path / "_w")
    assert out == [str(src)]
    assert not (tmp_path / "_w" / "sanitised").exists()

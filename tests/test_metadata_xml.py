"""Test PLATEAU metadata XML parsing on a real Suginami flood metadata file.

We don't ship the full PLATEAU metadata XML as a fixture (it's a 30 KB
ISO-19115 document). Instead we synthesise a minimal-but-realistic
example with the JMP20 namespace and key tags wired up.
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from plateau_parquet.sources.metadata_xml import (
    canonicalise_source_document,
    find_metadata_files,
    parse_metadata_xml,
)

SAMPLE = dedent("""\
    <?xml version="1.0" encoding="utf-8"?>
    <MD_Metadata xmlns="http://zgate.gsi.go.jp/ch/jmp/">
      <identificationInfo>
        <MD_DataIdentification>
          <citation>
            <title>洪水浸水想定区域3Dモデル_13115_city_2024_op</title>
          </citation>
          <descriptiveKeywords>
            <MD_Keywords>
              <keyword>東京都杉並区</keyword>
              <type>002</type>
            </MD_Keywords>
            <MD_Keywords>
              <keyword>洪水浸水想定区域</keyword>
              <keyword>LOD1</keyword>
              <keyword>防災</keyword>
              <type>005</type>
            </MD_Keywords>
            <MD_Keywords>
              <keyword>利根川水系利根川洪水浸水想定区域図（平成29年7月20日）国土交通省関東地方整備局利根川下流河川事務所</keyword>
              <keyword>多摩川水系多摩川、浅川、大栗川洪水浸水想定区域図（平成28年5月30日）国土交通省関東地方整備局京浜河川事務所</keyword>
              <keyword>神田川流域浸水予想区域図 平成26年4月 東京都建設局河川部</keyword>
              <type>005</type>
            </MD_Keywords>
          </descriptiveKeywords>
          <extent>
            <geographicElement>
              <EX_GeographicBoundingBox>
                <westBoundLongitude>138.943002</westBoundLongitude>
                <eastBoundLongitude>139.925</eastBoundLongitude>
                <southBoundLatitude>35.501889</southBoundLatitude>
                <northBoundLatitude>35.898452</northBoundLatitude>
              </EX_GeographicBoundingBox>
            </geographicElement>
          </extent>
        </MD_DataIdentification>
      </identificationInfo>
    </MD_Metadata>
""")


def test_parses_title_bbox_and_sources(tmp_path: Path) -> None:
    p = tmp_path / "metadata" / "udx_13115_city_2024_fld_op.xml"
    p.parent.mkdir()
    p.write_text(SAMPLE, encoding="utf-8")

    out = parse_metadata_xml(p)
    assert out is not None
    assert out.title == "洪水浸水想定区域3Dモデル_13115_city_2024_op"
    assert out.bbox is not None
    assert out.bbox.west == 138.943002
    assert out.bbox.east == 139.925
    # 3 source documents — the topic keywords (洪水浸水想定区域, LOD1, 防災)
    # are filtered out because they're too short.
    assert len(out.source_documents) == 3
    assert any("利根川水系利根川洪水浸水想定区域図" in s for s in out.source_documents)


def test_find_metadata_files(tmp_path: Path) -> None:
    meta = tmp_path / "metadata"
    meta.mkdir()
    (meta / "udx_13115_city_2024_fld_op.xml").touch()
    (meta / "udx_13115_city_2024_lsld_op.xml").touch()
    (meta / "udx_13115_city_2024_bldg_op.xml").touch()
    (meta / "something_else.xml").touch()

    files = find_metadata_files(tmp_path)
    assert len(files) == 3
    names = {f.name for f in files}
    assert names == {
        "udx_13115_city_2024_fld_op.xml",
        "udx_13115_city_2024_lsld_op.xml",
        "udx_13115_city_2024_bldg_op.xml",
    }


def test_canonicalise_strips_date_and_publisher() -> None:
    raw = "利根川水系利根川洪水浸水想定区域図（平成29年7月20日）国土交通省関東地方整備局利根川下流河川事務所"
    assert canonicalise_source_document(raw) == "利根川水系利根川洪水浸水想定区域図"


def test_canonicalise_no_op_when_no_tail() -> None:
    assert canonicalise_source_document("利根川水系利根川洪水浸水想定区域図") == "利根川水系利根川洪水浸水想定区域図"


def test_returns_none_on_garbage(tmp_path: Path) -> None:
    p = tmp_path / "junk.xml"
    p.write_text("not xml")
    assert parse_metadata_xml(p) is None


def test_returns_none_on_missing(tmp_path: Path) -> None:
    assert parse_metadata_xml(tmp_path / "does_not_exist.xml") is None

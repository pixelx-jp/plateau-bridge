"""Universal honest record — invariant + contract-shape tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from plateau_bridge.records import (
    ASSESSED_TIERS,
    ConfidenceTier,
    Record,
    Source,
    provenance_chain,
)

GOOD_SOURCE = {"modality": "public_dataset", "dataset_id": "plateau_kuni_2023"}


def _covered(**kw):
    base = dict(
        asset_id="b1",
        asset_id_scheme="other",
        asset_class="building",
        attribute="river_flood_depth_max",
        value=2.5,
        unit="m",
        covered=True,
        confidence_tier="modelled",
        confidence=0.9,
        source=GOOD_SOURCE,
        observed_at="2023-01-01T00:00:00Z",
    )
    base.update(kw)
    return Record(**base)


def test_covered_false_rejects_value():
    with pytest.raises(ValidationError, match="honesty invariant"):
        Record(
            asset_id="b1", asset_id_scheme="other", asset_class="building",
            attribute="river_flood_depth_max", value=2.5, covered=False,
            source=GOOD_SOURCE, observed_at="2023-01-01T00:00:00Z",
        )


def test_covered_false_rejects_confidence():
    with pytest.raises(ValidationError, match="confidence=None"):
        Record(
            asset_id="b1", asset_id_scheme="other", asset_class="building",
            attribute="river_flood_depth_max", value=None, covered=False,
            confidence=0.5, source=GOOD_SOURCE, observed_at="2023-01-01T00:00:00Z",
        )


def test_covered_true_requires_assessed_tier():
    with pytest.raises(ValidationError):
        _covered(confidence_tier=None)
    with pytest.raises(ValidationError):
        _covered(confidence_tier="unknown")


def test_source_must_be_traceable():
    with pytest.raises(ValidationError, match="traceable"):
        Source(modality="public_dataset")


def test_unknown_constructor_is_honest():
    r = Record.unknown(
        asset_id="b9", asset_id_scheme="other", attribute="tsunami_depth_max",
        source=GOOD_SOURCE, observed_at="2023-01-01T00:00:00Z",
    )
    assert r.covered is False and r.value is None and r.confidence is None


def test_to_contract_dict_shape():
    d = _covered().to_contract_dict()
    assert d["value"] == 2.5 and d["covered"] is True
    assert d["confidence_tier"] == "modelled"
    assert d["source"]["modality"] == "public_dataset"

    grey = Record.unknown(
        asset_id="b9", asset_id_scheme="other", attribute="tsunami_depth_max",
        source=GOOD_SOURCE, observed_at="2023-01-01T00:00:00Z",
    ).to_contract_dict()
    assert grey["value"] is None and grey["confidence"] is None
    # confidence_tier enum has no null member -> 'unknown', never null
    assert grey["confidence_tier"] == "unknown"


def test_provenance_chain():
    chain = provenance_chain(_covered())
    assert chain["attribute"] == "river_flood_depth_max"
    assert chain["source"]["dataset_id"] == "plateau_kuni_2023"
    assert "PLATEAU" in chain["attribution"]


def test_assessed_tiers_excludes_unknown():
    assert ConfidenceTier.UNKNOWN not in ASSESSED_TIERS

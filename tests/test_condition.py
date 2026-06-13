"""Extension A — condition columns: schema presence + honesty enforcement."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from plateau_bridge.schema import (
    BUILDINGS_ARROW_SCHEMA,
    ConditionField,
    ConditionState,
    condition_columns,
)


def test_condition_columns_present_in_arrow_schema():
    names = set(BUILDINGS_ARROW_SCHEMA.names)
    for col in condition_columns():
        assert col in names


def test_condition_columns_are_additive_not_replacing_hazards():
    names = set(BUILDINGS_ARROW_SCHEMA.names)
    # the existing hazard + base columns must still be there (no rename/remove)
    for col in ("river_flood_covered", "river_flood_depth_max", "building_uid"):
        assert col in names


def test_condition_field_honesty_invariant():
    # covered=false must not carry a state or confidence
    with pytest.raises(ValidationError, match="honesty invariant"):
        ConditionField(covered=False, state=ConditionState.DANGER)
    with pytest.raises(ValidationError, match="honesty invariant"):
        ConditionField(covered=False, confidence=0.5)
    # the honest unknown is fine
    ok = ConditionField(covered=False)
    assert ok.state is None


def test_condition_field_assessed():
    c = ConditionField(
        covered=True, state=ConditionState.CAUTION, confidence=0.6,
        confidence_tier="inferred", observed_at="2024-01-05T00:00:00Z",
    )
    assert c.state == "要注意"

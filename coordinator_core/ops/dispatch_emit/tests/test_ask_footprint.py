"""ask_footprint: the footprint-owed truth table and the leg's prompt and schema."""

import pytest

from coordinator_core.ops.dispatch_emit.ask_footprint import FOOTPRINT_SCHEMA, footprint_owed, footprint_prompt
from coordinator_core.ops.dispatch_emit.sizing_fire import ARM_M_PLUS, ARM_S, ARM_XS


def test_owed_for_xs_without_writes_not_sized_in_run():
    assert footprint_owed(arm=ARM_XS, writes=[], sized_in_run=False) is True


@pytest.mark.parametrize(
    "arm,writes,sized",
    [
        (ARM_XS, ["a.py"], False),
        (ARM_XS, [], True),
        (ARM_S, [], False),
        (ARM_M_PLUS, [], False),
        (None, [], False),
    ],
)
def test_not_owed_otherwise(arm, writes, sized):
    assert footprint_owed(arm=arm, writes=writes, sized_in_run=sized) is False


def test_prompt_names_sizing_and_baton():
    p = footprint_prompt(sizing_rel="state/sizings/s.yaml", baton_path="state/handoffs/b.md")
    assert "state/sizings/s.yaml" in p and "state/handoffs/b.md" in p
    assert "no globs and no directories" in p


def test_prompt_omits_baton_clause_when_none():
    p = footprint_prompt(sizing_rel="state/sizings/s.yaml", baton_path=None)
    assert "baton" not in p.lower()


def test_schema_requires_writes():
    assert FOOTPRINT_SCHEMA["required"] == ["writes"]
    assert "writes" in FOOTPRINT_SCHEMA["properties"]

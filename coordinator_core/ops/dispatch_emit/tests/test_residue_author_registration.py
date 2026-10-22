"""Pins coordinator:residue-author into the four engine agent-type sets without a DoE checkout."""
from coordinator_core.attribution import roster
from coordinator_core.hooks.support import named_dispatch_strip
from coordinator_core.ops.dispatch_emit import emit, review_edit_source

_FULL = "coordinator:residue-author"
_SHORT = "residue-author"


def test_model_table_pins_sonnet():
    assert emit.resolve_agent_model(_FULL) == "sonnet"


def test_roster_identifiers_carry_short_name():
    assert _SHORT in roster.AGENT_IDENTIFIERS


def test_non_review_subagent_types_carry_short_name():
    assert _SHORT in review_edit_source.NON_REVIEW_SUBAGENT_TYPES


def test_reporting_subagent_types_carry_full_name():
    assert _FULL in named_dispatch_strip._REPORTING_SUBAGENT_TYPES

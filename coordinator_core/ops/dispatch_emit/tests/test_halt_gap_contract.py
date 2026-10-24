"""The shared halt-gap frame names exist and say what the reviewer and land checks key on."""

from coordinator_core.ops.dispatch_emit import ask_contract as c


def test_gap_frame_names_exist():
    assert c.GAPS_CLOSED_FIELD == "gaps_closed"
    assert c.GAP_TITLE_PREFIX.endswith(": ")
    assert c.HALT_GAP_UNCLOSED_REASON
    assert c.HALT_GAP_RULE


def test_gap_rule_separates_closing_from_pinning():
    assert "no longer holds" in c.HALT_GAP_RULE
    assert "does not close it" in c.HALT_GAP_RULE

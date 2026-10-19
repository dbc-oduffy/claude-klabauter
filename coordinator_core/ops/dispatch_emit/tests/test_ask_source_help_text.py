"""The --ask-source help states that an absent marking is relayed and only an explicit pm marking is PM."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import cli as cli_module


def _ask_source_help() -> str:
    parser = cli_module._build_parser()
    action = next(a for a in parser._actions if a.dest == "ask_source")
    return " ".join(action.help.split())


def test_help_states_absent_means_relayed():
    text = _ask_source_help()
    assert "absent" in text
    assert "relayed" in text
    assert "'relayed' or absent" in text


def test_help_does_not_claim_turn_log_pm_derivation():
    assert "recorded PM turn of the" not in _ask_source_help()

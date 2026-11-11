"""--help names workflow.revise with its params shape, without importing the op registry."""

from coordinator_core.invoke.__main__ import _build_arg_parser
from coordinator_core.ops.dispatch_emit import requirements_ledger


def test_help_names_workflow_revise_and_params():
    text = _build_arg_parser().format_help()
    assert "coordinator-invoke workflow.revise" in text
    assert "--describe" in text


def test_help_names_ask_spool_withdraw_and_every_param():
    from coordinator_core.ops import ask_spool_withdraw

    epilog = _build_arg_parser().epilog
    assert "coordinator-invoke ask_spool.withdraw" in epilog
    for name in ask_spool_withdraw._SCHEMA["properties"]:
        assert f"{name} (" in epilog or f"{name}:" in epilog, name


def test_spooled_refusal_hint_names_withdraw_with_spool_id():
    from coordinator_core.hooks import submit_engine_ask

    hint = submit_engine_ask._WITHDRAW_HINT.format(spool_id="0123456789abcdef")
    assert "coordinator-invoke ask_spool.withdraw" in hint
    assert '"spool_id":"0123456789abcdef"' in hint


def test_help_names_every_workflow_revise_param():
    """The epilog is hand-written; this fails when a param is added to the op and not to the help."""
    epilog = _build_arg_parser().epilog
    for field in requirements_ledger._PARAMS:
        assert f"{field.name} (" in epilog, field.name

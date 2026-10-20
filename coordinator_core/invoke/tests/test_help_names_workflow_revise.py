"""--help names workflow.revise with its params shape, without importing the op registry."""

from coordinator_core.invoke.__main__ import _build_arg_parser
from coordinator_core.ops.dispatch_emit import requirements_ledger


def test_help_names_workflow_revise_and_params():
    text = _build_arg_parser().format_help()
    assert "coordinator-invoke workflow.revise" in text
    assert "--describe" in text


def test_help_names_every_workflow_revise_param():
    """The epilog is hand-written; this fails when a param is added to the op and not to the help."""
    epilog = _build_arg_parser().epilog
    for field in requirements_ledger._PARAMS:
        assert f"{field.name} (" in epilog, field.name

"""--help names workflow.revise with its params shape, without importing the op registry."""

import json
import re
from pathlib import Path

from coordinator_core.invoke.__main__ import _build_arg_parser
from coordinator_core.ops.dispatch_emit import refire_unchanged, requirements_ledger


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


def _remedy_sentence() -> str:
    source = Path(refire_unchanged.__file__).read_text(encoding="utf-8")
    match = re.search(r"send workflow\.revise[^\"]*", source)
    assert match, "refire-unchanged refusal no longer names workflow.revise"
    return match.group(0)


def _remedy_params() -> list[str]:
    names = {f.name for f in requirements_ledger._PARAMS}
    return [w for w in re.findall(r"\w+", _remedy_sentence()) if w in names]


def test_unchanged_refusal_remedy_names_params_workflow_revise_describes():
    params = _remedy_params()
    assert "add_paths" in params and "note" in params
    doc = requirements_ledger._handler.__doc__
    for name in params:
        assert f"`{name}`" in doc, name


def test_op_catalog_describes_every_param_the_unchanged_refusal_names():
    catalog = Path(requirements_ledger.__file__).parents[2] / "contract" / "op-catalog.json"
    ops = json.loads(catalog.read_text(encoding="utf-8"))["ops"]
    description = next(o["description"] for o in ops if o["name"] == "workflow.revise")
    for name in _remedy_params():
        assert f"`{name}`" in description, name

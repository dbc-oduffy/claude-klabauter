"""A re-ruling-only refire's leg footprint lists the writes of staged rows whose register id the sizing withholds."""

from __future__ import annotations

from types import SimpleNamespace

from coordinator_core.ops.dispatch_emit.ask_contract import LEG_DISPATCH, LEG_SKIP
from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import remaining_leg_plan

SIZING = "state/sizings/s.yaml"
_SIZING_TEXT = """requirement_register:
  rows:
  - id: R1
    source_text: old rule
    anchor: relayed
    status: deferred
    ruling: {source: pm, quote: q, ref: "workflow.revise:ask-1:out_of_scope", on: "2026-10-27"}
  - id: R2
    source_text: live rule
    anchor: relayed
    status: open
"""


def _root(tmp_path, text=_SIZING_TEXT):
    (tmp_path / "state/sizings").mkdir(parents=True)
    (tmp_path / SIZING).write_text(text, encoding="utf-8")
    return tmp_path


def _row(writes, register_rows):
    return SimpleNamespace(writes=tuple(writes), register_rows=tuple(register_rows))


def _plan(root, rows, notes=("met by 2fd9d96c",)):
    return remaining_leg_plan(
        {"R2": {"remaining": ["do the thing"]}}, list(notes), rows, ["R2"], [], SIZING, root,
    )


def test_re_ruling_only_refire_footprint_lists_ruled_out_row_writes(tmp_path):
    plan = _plan(_root(tmp_path), [_row(["pkg/venv.py"], ["R1"])])
    assert plan["action"] == LEG_SKIP
    assert plan["writes"] == ["pkg/venv.py"]


def test_dispatch_footprint_keeps_serving_and_ruled_out_writes(tmp_path):
    rows = [_row(["pkg/a.py"], ["R2"]), _row(["pkg/venv.py"], ["R1"])]
    plan = _plan(_root(tmp_path), rows, notes=())
    assert plan["action"] == LEG_DISPATCH
    assert plan["writes"] == ["pkg/a.py", "pkg/venv.py"]


def test_row_serving_a_live_unblocking_id_stays_out(tmp_path):
    root = _root(tmp_path, _SIZING_TEXT + "  - id: R3\n    source_text: x\n    anchor: relayed\n    status: open\n")
    plan = _plan(root, [_row(["pkg/other.py"], ["R3"]), _row(["pkg/mixed.py"], ["R1", "R3"])])
    assert plan["action"] == LEG_SKIP
    assert plan["writes"] == []


def test_unreadable_sizing_keeps_footprint_strict(tmp_path):
    plan = _plan(tmp_path, [_row(["pkg/venv.py"], ["R1"])])
    assert plan["writes"] == []

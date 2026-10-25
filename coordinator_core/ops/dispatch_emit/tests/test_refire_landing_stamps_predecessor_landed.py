"""A refire's landing stamps its `resumed` predecessor halted record landed at the landing sha."""

import inspect
import json

from coordinator_core.ops.dispatch_emit import halted_run as hr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

SIZING = "state/sizings/2026-10-16-foo.yaml"
SHA = "c" * 40


def _halt_and_refire(root, halted_id, refire_id):
    hr.write_halt_marker(record_dir(root, halted_id), halted="criterion_not_met", sizing_rel=SIZING)
    hr.mark_resumed(record_dir(root, halted_id))
    rl.start(root, refire_id, resumed_from=halted_id)


def _marker(root, run_id):
    return json.loads((record_dir(root, run_id) / hr.HALTED_FILE).read_text())


def test_refire_landing_stamps_predecessor_landed(tmp_path):
    _halt_and_refire(tmp_path, "ask-20261016T010000-aaaaaa", "ask-20261016T020000-bbbbbb")
    assert hr.mark_predecessors_landed(tmp_path, "ask-20261016T020000-bbbbbb", SHA) == ["ask-20261016T010000-aaaaaa"]
    data = _marker(tmp_path, "ask-20261016T010000-aaaaaa")
    assert data["status"] == hr.STATUS_LANDED and data["sha"] == SHA


def test_chain_is_stamped_and_non_resumed_predecessor_is_left_alone(tmp_path):
    first, second, third = "ask-20261016T010000-aaaaaa", "ask-20261016T020000-bbbbbb", "ask-20261016T030000-cccccc"
    hr.write_halt_marker(record_dir(tmp_path, first), halted="x", sizing_rel=SIZING)
    hr.mark_resumed(record_dir(tmp_path, first))
    rl.start(tmp_path, second, resumed_from=first)
    hr.write_halt_marker(record_dir(tmp_path, second), halted="x", sizing_rel=SIZING)
    hr.mark_resumed(record_dir(tmp_path, second))
    rl.start(tmp_path, third, resumed_from=second)
    assert hr.mark_predecessors_landed(tmp_path, third, SHA) == [second, first]

    other = "ask-20261016T040000-dddddd"
    hr.write_halt_marker(record_dir(tmp_path, other), halted="x", sizing_rel=SIZING)
    rl.start(tmp_path, "ask-20261016T050000-eeeeee", resumed_from=other)
    assert hr.mark_predecessors_landed(tmp_path, "ask-20261016T050000-eeeeee", SHA) == []
    assert _marker(tmp_path, other)["status"] == "halted"


def test_run_without_ledger_link_stamps_nothing(tmp_path):
    assert hr.mark_predecessors_landed(tmp_path, "ask-20261016T060000-ffffff", SHA) == []


def test_terminal_commit_calls_it_at_landing():
    assert "mark_predecessors_landed" in inspect.getsource(tc._terminal_commit_inner)

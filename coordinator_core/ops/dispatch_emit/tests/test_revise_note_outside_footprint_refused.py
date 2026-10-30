"""workflow.revise never replies applied for a note whose edit-verb clause names a repo file outside a staged
run's footprint (or a halt's uncovered needs_paths): the note is not stored, `refused` names the paths and
the plan.tasks.mutate add-task call, and a note that merely cites a file stays accepted."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

RUN = "warp-2026-10-30-x-75f803-20261030T101437-1e48f4"
INSIDE = "pkg/inside.py"
OUTSIDE = "pkg/outside.py"
OTHER = "pkg/other.py"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    for rel in (INSIDE, OUTSIDE, OTHER):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n", encoding="utf-8")
    rl.start(tmp_path, RUN)
    manifest = {"doc_writes": [], "rows": [{"id": "X1", "writes": [INSIDE], "writes_under": []}]}
    (rl.run_dir(tmp_path, RUN) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def _notes(repo):
    return rl.read_notes(rl.run_dir(repo, RUN))


def _call(repo, **params):
    return rl._handler({"run_id": RUN, **params}, repo)


def test_edit_verb_note_outside_the_footprint_is_refused_not_applied(repo):
    reply = _call(repo, note=f"add a field to {OUTSIDE}")
    assert "note" not in reply["applied"] and reply["ok"] is False
    (entry,) = [r for r in reply["refused"] if r.get("input") == "note"]
    assert OUTSIDE in entry["paths"] and OUTSIDE in entry["reason"]
    assert "plan.tasks.mutate" in entry["reason"] and "add-task" in entry["reason"]
    assert _notes(repo) == []


def test_refused_note_does_not_stop_the_rest_of_the_call(repo):
    reply = _call(repo, note=f"fix {OUTSIDE}", withhold_paths=[{"path": INSIDE, "reason": "peer hunks"}])
    assert reply["ok"] is True and "withhold_paths" in reply["applied"] and "note" not in reply["applied"]
    assert [r["input"] for r in reply["refused"]] == ["note"]


def test_note_naming_a_file_without_an_edit_verb_is_applied(repo):
    reply = _call(repo, note=f"run {OUTSIDE} to regenerate")
    assert "note" in reply["applied"] and reply["ok"] is True
    assert len(_notes(repo)) == 1


def test_edit_verb_note_naming_a_covered_file_is_applied(repo):
    reply = _call(repo, note=f"fix {INSIDE}")
    assert "note" in reply["applied"]


def test_only_the_edit_clause_counts(repo):
    reply = _call(repo, note=f"fix {INSIDE}. Source text came from {OUTSIDE}.")
    assert "note" in reply["applied"]
    reply = _call(repo, note=f"Source text came from {OUTSIDE}. Then fix {OTHER}.")
    entry = [r for r in reply["refused"] if r.get("input") == "note"][0]
    assert entry["paths"] == [OTHER]


def test_unstaged_run_has_no_footprint_to_breach(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / OUTSIDE).parent.mkdir(parents=True)
    (tmp_path / OUTSIDE).write_text("x\n", encoding="utf-8")
    rl.start(tmp_path, RUN)
    reply = rl._handler({"run_id": RUN, "note": f"fix {OUTSIDE}"}, tmp_path)
    assert "note" in reply["applied"]


def test_halted_run_refuses_a_note_while_needs_paths_are_uncovered(repo):
    marker = {
        "status": halted_run.STATUS_HALTED,
        "self_correction": {"needs_paths": [OUTSIDE, INSIDE]},
    }
    (rl.run_dir(repo, RUN) / halted_run.HALTED_FILE).write_text(json.dumps(marker), encoding="utf-8")
    reply = _call(repo, note="the EM ruling: add the missing field")
    assert "note" not in reply["applied"]
    entry = [r for r in reply["refused"] if r.get("input") == "note"][0]
    assert entry["paths"] == [OUTSIDE] and "add-task" in entry["reason"]
    assert "sizing `writes`" not in entry["reason"]
    assert _notes(repo) == []


def test_halted_run_takes_a_note_with_no_edit_verb_clause(repo):
    marker = {"status": halted_run.STATUS_HALTED, "self_correction": {"needs_paths": [OUTSIDE]}}
    (rl.run_dir(repo, RUN) / halted_run.HALTED_FILE).write_text(json.dumps(marker), encoding="utf-8")
    assert "note" in _call(repo, note="the EM ruling: proceed, the sha is re-ruled")["applied"]
    assert len(_notes(repo)) == 1


def test_note_is_taken_once_the_path_joins_the_sizing_writes(repo):
    note = f"add a field to {OUTSIDE}"
    assert "note" not in _call(repo, note=note)["applied"]
    sizing_rel = "state/sizings/s.yaml"
    sizing = repo / sizing_rel
    sizing.parent.mkdir(parents=True)
    sizing.write_text(f"schema: sizing-object\nintent: x\nwrites:\n  - {OUTSIDE}\n", encoding="utf-8")
    rl.bind(repo, RUN, sizing_rel)
    reply = _call(repo, note=note)
    assert "note" in reply["applied"] and len(_notes(repo)) == 1


def test_held_run_queues_an_edit_verb_note_naming_an_outside_file(tmp_path):
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
    from coordinator_core.ops.dispatch_emit.ask_fanout_contract import ParentRecord, SiblingRun, parent_record_rel

    (tmp_path / ".git").mkdir()
    (tmp_path / OUTSIDE).parent.mkdir(parents=True)
    (tmp_path / OUTSIDE).write_text("x = 1\n", encoding="utf-8")
    parent = ParentRecord(parent_id="ask-parent", runs=(SiblingRun(run_id=RUN, label="a"),))
    path = tmp_path / parent_record_rel("ask-parent")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(parent.to_json()), encoding="utf-8")

    reply = rl._handler({"run_id": RUN, "note": f"add a field to {OUTSIDE}"}, tmp_path)
    assert reply["ok"] is True and reply["held"] is True
    assert "note queued for the run's start" in reply["applied"] and reply["refused"] == []
    assert len(rl.read_notes(tmp_path / RECORD_DIR_ROOT / RUN)) == 1

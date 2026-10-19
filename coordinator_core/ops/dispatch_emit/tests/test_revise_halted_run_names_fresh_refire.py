"""workflow.revise on a run holding a `halted` marker: not live, stamps the marker, names the fresh refire."""

from __future__ import annotations

import yaml

from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.warp_fire import fire_command

RUN = "ask-20261015T000000-abc123"
REL = "state/sizings/2026-10-15-revise.yaml"


def _repo(tmp_path, *, emit=True):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    sizing = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8")
    if emit:
        rl.start(tmp_path, RUN)
        rl.bind(tmp_path, RUN, REL, ask_text="a raw ask")
    return tmp_path


def _revise(root):
    return rl._handler({"run_id": RUN, "verbatim": "also cover Windows"}, repo_root=root)


def test_halted_marker_makes_the_run_not_live_and_names_the_fresh_refire(tmp_path):
    root = _repo(tmp_path)
    dir_ = record_dir(root, RUN)
    halted_run.write_halt_marker(dir_, halted="stalled", sizing_rel=REL)
    out = _revise(root)
    assert out["run_live"] is False
    assert out["next"] == {"kind": "refire", "call": fire_command(REL)}
    assert "resumeFromRunId" in out["note"] and "never reads this revision" in out["note"]
    assert halted_run.revised_halt(dir_)


def test_a_resumed_marker_is_left_alone(tmp_path):
    root = _repo(tmp_path)
    dir_ = record_dir(root, RUN)
    halted_run.write_halt_marker(dir_, halted="stalled", sizing_rel=REL)
    halted_run.mark_resumed(dir_)
    _revise(root)
    assert halted_run.revised_halt(dir_) is None
    assert halted_run.REVISED_AT_FIELD not in halted_run._read(dir_)


def test_no_marker_keeps_the_unchanged_reply(tmp_path):
    root = _repo(tmp_path)
    out = _revise(root)
    assert "resumeFromRunId" not in out.get("note", "")
    assert not (record_dir(root, RUN) / halted_run.HALTED_FILE).exists()


def test_a_held_run_writes_no_marker(tmp_path):
    root = _repo(tmp_path, emit=False)
    out = _revise(root)
    assert "error" in out or out.get("held") is True
    assert not (record_dir(root, RUN) / halted_run.HALTED_FILE).exists()

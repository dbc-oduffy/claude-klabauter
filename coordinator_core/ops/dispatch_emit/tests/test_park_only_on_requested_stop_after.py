"""A plain relayed ask whose lineage reaches a stale parked predecessor stages instead of parking; a real park
names its marker source. Drives `dispatch.ask_stage`'s handler over a tmp_path record tree."""
from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import ask_stage
from coordinator_core.ops.dispatch_emit.ask_contract import (
    PARK_SOURCE_REPLY_KEY, PARKED_REPLY_KEY, STOP_AFTER_FILE, record_dir,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import PLAN, PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts

_NEW = "ask-new"
_OLD = "ask-old"


def _seed(root) -> None:
    (root / ".git").mkdir(exist_ok=True)
    write_run_scripts(root, _NEW)
    (root / PLAN_REL).parent.mkdir(parents=True, exist_ok=True)
    (root / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")


def _old_parked_run(root, *, edge: str) -> None:
    """The example-game-repo shape: an earlier run for the same sizing parked on a revise; its marker was never
    consumed, and the new plain ask links to it."""
    old = record_dir(root, _OLD)
    old.mkdir(parents=True, exist_ok=True)
    (old / STOP_AFTER_FILE).write_text(
        json.dumps({"stop_after": "plan", "at": "2026-11-01T00:00:00Z", "from": "claude-em-old"}), encoding="utf-8"
    )
    (old / "halted.json").write_text(json.dumps({"halted": "parked"}), encoding="utf-8")
    new = record_dir(root, _NEW)
    new.mkdir(parents=True, exist_ok=True)
    (new / "run.json").write_text(json.dumps({edge: _OLD}), encoding="utf-8")


def _stage(root) -> dict:
    return ask_stage._handler({"run_id": _NEW, "plan_path": PLAN_REL, "writes": []}, repo_root=root)


def test_a_plain_ask_resuming_a_parked_predecessor_stages(tmp_path):
    for edge in ("resumed_from", "resumes_ask", "reserved_parent"):
        _seed(tmp_path)
        _old_parked_run(tmp_path, edge=edge)
        reply = _stage(tmp_path)
        assert PARKED_REPLY_KEY not in reply and "error" not in reply, (edge, reply)
        assert (record_dir(tmp_path, _NEW) / "manifest.json").is_file()
        (record_dir(tmp_path, _NEW) / "manifest.json").unlink()


def test_a_chain_child_of_a_live_marker_still_parks_and_names_the_source(tmp_path):
    _seed(tmp_path)
    old = record_dir(tmp_path, _OLD)
    old.mkdir(parents=True, exist_ok=True)
    (old / STOP_AFTER_FILE).write_text(
        json.dumps({"stop_after": "plan", "from": "claude-em-old"}), encoding="utf-8"
    )
    (record_dir(tmp_path, _NEW) / "run.json").parent.mkdir(parents=True, exist_ok=True)
    (record_dir(tmp_path, _NEW) / "run.json").write_text(json.dumps({"chain_parent": _OLD}), encoding="utf-8")

    reply = _stage(tmp_path)

    assert reply[PARKED_REPLY_KEY] == "plan"
    assert reply[PARK_SOURCE_REPLY_KEY] == {"run_id": _OLD, "from": "claude-em-old", "inherited": True}


def test_a_plain_relayed_emit_and_its_unmarked_chain_child_write_no_stop_after_marker(tmp_path, monkeypatch):
    """The emit-side pin: no stop_after requested and none in the lineage leaves no marker, single or chained."""
    import yaml

    from coordinator_core.ops.dispatch_emit import ask_compose, fan_launcher as fl, halted_run
    from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
    from coordinator_core.ops.dispatch_emit.tests import test_stop_after_survives_chained_runs_e2e as e2e

    rel = e2e.REL
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / rel).write_text(yaml.safe_dump(e2e._SIZING_DOC, sort_keys=False), encoding="utf-8", newline="\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: e2e._BLITZ)
    monkeypatch.setattr(halted_run, "_RESUME_SPAWNER", lambda root, run_id: None)

    plain = _dispatch_emit({"ask": "add a thing", "target_root": str(tmp_path)})
    assert "error" not in plain, plain
    assert not (record_dir(tmp_path, plain["run_id"]) / STOP_AFTER_FILE).exists()

    child_params = fl.child_emit_params(tmp_path, plain["run_id"], {"ask": True, "sizing_path": rel, "target_root": str(tmp_path)})
    assert "stop_after" not in child_params
    child = _dispatch_emit({**child_params, "chain_parent": plain["run_id"]})
    assert "error" not in child, child
    assert not (record_dir(tmp_path, child["run_id"]) / STOP_AFTER_FILE).exists()

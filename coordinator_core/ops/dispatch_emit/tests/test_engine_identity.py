"""Engine-identity receipts: one line per stage launch, and `run.engine_identity` reads them back."""
from __future__ import annotations

import json
import os

from coordinator_core.ops.dispatch_emit import engine_identity as ei

RUN = "warp-2026-11-20-identity-aaaaaa-20261120T000000-bbbbbb"


def _identity(head: str, stamp: str) -> dict:
    return {"engine_root": "R", "engine_head": head, "source_stamp": stamp}


def test_launch_writes_one_line_with_every_field(tmp_path, monkeypatch):
    monkeypatch.setattr(ei, "process_identity", lambda: _identity("a538eb621d", "2e31713580"))
    spawned = []
    on_spawn = ei.spawn_recorder(tmp_path, RUN, "execute", spawned.append)
    on_spawn(4242)
    path = ei._receipt_path(tmp_path, RUN)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and spawned == [4242]
    row = json.loads(lines[0])
    assert tuple(row) == ei.FIELDS
    assert (row["run_id"], row["stage_id"], row["worker_pid"], row["engine_pid"]) == (RUN, "execute", 4242, os.getpid())
    assert (row["engine_head"], row["source_stamp"]) == ("a538eb621d", "2e31713580")
    assert row["ts"].endswith("Z")


def test_process_identity_names_this_checkout():
    got = ei.process_identity()
    assert set(got) == {"engine_root", "engine_head", "source_stamp"}
    assert got["engine_root"] and os.path.isdir(got["engine_root"])


def test_different_stamps_read_back_inconsistent(tmp_path, monkeypatch):
    monkeypatch.setattr(ei, "process_identity", lambda: _identity("a538eb621d", "2e31713580"))
    ei.append_receipt(tmp_path, RUN, "plan", 1)
    assert ei.read_latest(tmp_path, RUN)["consistent"] is True
    monkeypatch.setattr(ei, "process_identity", lambda: _identity("a538eb621d", "ffffffffff"))
    ei.append_receipt(tmp_path, RUN, "execute", 2)
    got = ei.read_latest(tmp_path, RUN)
    assert got["consistent"] is False and got["count"] == 2 and got["receipt"]["stage_id"] == "execute"


def test_no_receipt_is_not_consistent(tmp_path):
    got = ei.read_latest(tmp_path, RUN)
    assert got == {"run_id": RUN, "receipt": None, "count": 0, "consistent": False}


def test_op_is_registered_compute_only_and_returns_the_receipt(tmp_path, monkeypatch):
    from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
    from coordinator_core.ipc import _REGISTRY
    from coordinator_core.ops._registry_map import OP_MODULE_MAP

    assert ei.OP in _REGISTRY and OP_MODULE_MAP[ei.OP] == ei.__name__
    assert OP_CLASSIFICATION[ei.OP] is OpClass.COMPUTE_ONLY
    monkeypatch.setattr(ei, "process_identity", lambda: _identity("h", "s"))
    ei.append_receipt(tmp_path, RUN, "execute", 7)
    reply = _REGISTRY[ei.OP]({"run_id": RUN}, tmp_path)
    assert reply["consistent"] is True and reply["receipt"]["worker_pid"] == 7
    assert "error" in _REGISTRY[ei.OP]({}, tmp_path)


def test_run_agent_records_receipt_and_briefs_prompt(tmp_path, monkeypatch):
    from coordinator_core.codex_runner import workflow

    monkeypatch.setattr(ei, "process_identity", lambda: _identity("h", "s"))
    seen = {}

    def spawn(argv, prompt, *, on_spawn, **_kw):
        seen["prompt"] = prompt
        on_spawn(99)
        raise OSError("stubbed spawn")

    monkeypatch.setattr(workflow, "_run_admitted", spawn)
    monkeypatch.setattr(workflow, "resolve_codex", lambda **_kw: ["codex"])
    monkeypatch.setattr(workflow, "preflight_auth", lambda **_kw: None)
    monkeypatch.setattr(workflow, "shape_codex_child", lambda *a, **k: None)
    try:
        workflow.run_agent("do it", {"label": "execute"}, repo_root=tmp_path, artifact_dir=tmp_path / "art",
                           parent_session_id="sid", run_id=RUN)
    except workflow.AgentFailed:
        pass
    assert f"run.engine_identity '{{\"run_id\":\"{RUN}\"}}'" in seen["prompt"]
    got = ei.read_latest(tmp_path, RUN)
    assert got["count"] == 1 and got["receipt"]["stage_id"] == "execute" and got["receipt"]["worker_pid"] == 99

"""A multi-ask submission's child emits resolve each sizing under the root the fan-out wrote it to,
and only after that write returned."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core import warp_fire
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
from coordinator_core.ops.dispatch_emit.sizing_fire import load_sizing

PARENT = "ask-parent-20261015T000000-aaaaaa"


def _repo(base: Path, name: str) -> Path:
    root = base / name
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(mod, "_is_first_turn", lambda _payload: True)


class _Fanout:
    """Writes each child sizing under `write_root`, logging each write as it returns."""

    def __init__(self, write_root: Path, events: list):
        self.write_root, self.events = write_root, events

    def __call__(self, params: dict, root: Path) -> dict:
        records = []
        for i in (1, 2):
            run_id = f"r{i}"
            rel = f"state/sizings/{run_id}.yaml"
            path = self.write_root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"em_analysis:\n  ask_parent: {PARENT} {run_id}\n", encoding="utf-8")
            self.events.append(("write", run_id))
            records.append({"run_id": run_id, "label": run_id, "after": [], "slice_sizing": rel, "status": "ready"})
        parent = self.write_root / contract.parent_record_rel(PARENT)
        parent.parent.mkdir(parents=True, exist_ok=True)
        parent.write_text(
            json.dumps({"schema": "ask-parent", "version": 1, "parent_id": PARENT, "runs": records}),
            encoding="utf-8",
        )
        return {
            "parent_id": PARENT,
            "runs": [{"run_id": r["run_id"], "label": r["label"]} for r in records],
            "writer_root": str(self.write_root),
        }


class _Launch:
    """Stands in for the launcher: each child emit leg loads its sizing the way `dispatch.emit` does."""

    def __init__(self, root: Path, events: list):
        self.root, self.events, self.loaded = root, events, {}

    def __call__(self, root, parent_id, children, session_id):
        for child in children:
            self.events.append(("emit", child.run_id))
            params = child.emit_params
            self.loaded[child.run_id] = load_sizing(Path(params["target_root"]), params["sizing_path"])
        return self.root / "scratch" / "warp" / "launcher.workflow.mjs"


def _run(submitting: Path, writer: Path, fanout_cls=_Fanout):
    events: list = []
    launch = _Launch(submitting, events)
    payload = {
        "tool_name": "Workflow",
        "tool_input": {
            "name": warp_fire.SUBMIT_WORKFLOW_NAME,
            "args": {"asks": [{"ask": "ask a", "label": "a"}, {"ask": "ask b", "label": "b"}]},
        },
        "session_id": "sess-1",
        "transcript_path": str(submitting / "transcript.jsonl"),
        "cwd": str(submitting),
    }
    out = mod.evaluate(payload, emit=lambda *_a: {}, fanout=fanout_cls(writer, events), launch=launch)
    return out, events, launch


def test_child_emits_load_sizings_from_the_writer_root_after_each_write(tmp_path):
    submitting, writer = _repo(tmp_path, "submitting"), _repo(tmp_path, "mirror")
    out, events, launch = _run(submitting, writer)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert set(launch.loaded) == {"r1", "r2"}
    for run_id in ("r1", "r2"):
        assert events.index(("write", run_id)) < events.index(("emit", run_id))


def test_a_reported_sizing_missing_under_the_writer_root_is_refused_before_any_emit(tmp_path):
    submitting, writer = _repo(tmp_path, "submitting"), _repo(tmp_path, "mirror")

    class _Dropping(_Fanout):
        def __call__(self, params, root):
            reply = super().__call__(params, root)
            (self.write_root / "state/sizings/r2.yaml").unlink()
            return reply

    out, events, _launch = _run(submitting, writer, _Dropping)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert not any(kind == "emit" for kind, _ in events)


def test_a_reply_without_writer_root_falls_back_to_the_submitting_root(tmp_path):
    submitting = _repo(tmp_path, "submitting")

    class _NoRoot(_Fanout):
        def __call__(self, params, root):
            reply = super().__call__(params, root)
            del reply["writer_root"]
            return reply

    out, _events, launch = _run(submitting, submitting, _NoRoot)
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert set(launch.loaded) == {"r1", "r2"}

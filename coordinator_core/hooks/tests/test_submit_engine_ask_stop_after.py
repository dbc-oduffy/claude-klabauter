"""`stop_after` on the engine-ask submit hook: accepted at top level and per `asks` entry, value `plan` only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core import warp_fire
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
from coordinator_core.ops.dispatch_emit import run_token

PARENT = "ask-parent-20261015T000000-aaaaaa"


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


def _payload(root: Path, args) -> dict:
    return {
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": args},
        "session_id": "sess-1",
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    }


class _Emit:
    def __init__(self):
        self.calls: list = []

    def __call__(self, params: dict, root: Path) -> dict:
        self.calls.append((params, root))
        run_id = "ask-20261013T000000-abcdef"
        script = root / "scratch" / "warp" / f"{run_id}.workflow.mjs"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("export const meta = {}\n", encoding="utf-8")
        run_token.mint(root, run_id, str(script), session_id=params.get("session_id"),
                       sizing_rel=params.get("sizing_path"))
        return {"ok": True, "path": str(script), "run_id": run_id, "fire_args": {"repoRoot": "/r"}}


class _Fanout:
    def __init__(self, names):
        self.names = names
        self.calls: list = []

    def __call__(self, params: dict, root: Path) -> dict:
        self.calls.append((params, root))
        records = []
        for n in self.names:
            rel = f"state/sizings/r-{n}.yaml"
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(f"em_analysis:\n  ask_parent: {PARENT} r-{n}\n", encoding="utf-8")
            records.append({"run_id": f"r-{n}", "label": n, "after": [], "slice_sizing": rel, "status": "ready"})
        parent = root / contract.parent_record_rel(PARENT)
        parent.parent.mkdir(parents=True, exist_ok=True)
        parent.write_text(
            json.dumps({"schema": "ask-parent", "version": 1, "parent_id": PARENT, "runs": records}),
            encoding="utf-8",
        )
        return {"parent_id": PARENT, "runs": [{"run_id": r["run_id"], "label": r["label"]} for r in records]}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(mod, "_is_first_turn", lambda _payload: True)


def _decision(env: dict) -> str:
    return (env.get("hookSpecificOutput") or {}).get("permissionDecision")


def _reason(env: dict) -> str:
    return (env.get("hookSpecificOutput") or {}).get("permissionDecisionReason", "")


def test_ask_with_stop_after_plan_reaches_the_emit_op(tmp_path):
    emit = _Emit()
    out = mod.evaluate(_payload(_repo(tmp_path), {"ask": "a", "stop_after": "plan"}), emit=emit)
    assert _decision(out) == "allow"
    assert emit.calls[0][0]["stop_after"] == "plan"


def test_ask_without_stop_after_passes_none_to_the_emit_op(tmp_path):
    emit = _Emit()
    mod.evaluate(_payload(_repo(tmp_path), {"ask": "a"}), emit=emit)
    assert "stop_after" not in emit.calls[0][0]


def test_sizing_with_stop_after_plan_reaches_the_emit_op(tmp_path):
    emit = _Emit()
    out = mod.evaluate(_payload(_repo(tmp_path), {"sizing": "state/sizings/x.yaml", "stop_after": "plan"}), emit=emit)
    assert _decision(out) == "allow"
    assert emit.calls[0][0]["stop_after"] == "plan"


def test_one_element_asks_collapse_carries_the_entry_value(tmp_path):
    emit = _Emit()
    out = mod.evaluate(_payload(_repo(tmp_path), {"asks": [{"ask": "a", "stop_after": "plan"}]}), emit=emit)
    assert _decision(out) == "allow"
    assert emit.calls[0][0]["stop_after"] == "plan"


def test_one_element_asks_collapse_takes_the_top_level_default(tmp_path):
    emit = _Emit()
    mod.evaluate(_payload(_repo(tmp_path), {"asks": [{"ask": "a"}], "stop_after": "plan"}), emit=emit)
    assert emit.calls[0][0]["stop_after"] == "plan"


def test_multi_asks_apply_the_top_level_value_to_entries_lacking_their_own(tmp_path):
    fan = _Fanout(["a", "b", "c"])
    args = {"asks": [{"ask": "a"}, {"ask": "b", "stop_after": "plan"}, {"ask": "c"}], "stop_after": "plan"}
    out = mod.evaluate(_payload(_repo(tmp_path), args), emit=_Emit(), fanout=fan)
    assert _decision(out) == "allow"
    assert [item.get("stop_after") for item in fan.calls[0][0]["asks"]] == ["plan", "plan", "plan"]


def test_multi_asks_without_top_level_keep_only_the_entry_that_set_it(tmp_path):
    fan = _Fanout(["a", "b"])
    args = {"asks": [{"ask": "a", "stop_after": "plan"}, {"ask": "b"}]}
    mod.evaluate(_payload(_repo(tmp_path), args), emit=_Emit(), fanout=fan)
    assert [item.get("stop_after") for item in fan.calls[0][0]["asks"]] == ["plan", None]


@pytest.mark.parametrize(
    "args",
    [
        {"ask": "a", "stop_after": "execute"},
        {"sizing": "state/sizings/x.yaml", "stop_after": "stage"},
        {"asks": [{"ask": "a"}, {"ask": "b"}], "stop_after": "execute"},
        {"asks": [{"ask": "a"}, {"ask": "b", "stop_after": "execute"}]},
        {"asks": [{"ask": "a", "stop_after": "execute"}]},
    ],
)
def test_a_non_plan_value_is_refused_naming_the_key_and_the_accepted_value(tmp_path, args):
    emit, fan = _Emit(), _Fanout(["a", "b"])
    out = mod.evaluate(_payload(_repo(tmp_path), args), emit=emit, fanout=fan)
    assert _decision(out) == "deny"
    assert "stop_after" in _reason(out)
    assert '"plan"' in _reason(out)
    assert not emit.calls and not fan.calls

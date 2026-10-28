"""Post-fire record of `nudge_em_code_dispatch.op`: only a path a live run owns produces the
advisory; a halted or landed run owns nothing, and no site returns a permissionDecision deny."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import coordinator_core.machine_profile as mp
from coordinator_core.hooks import _em_write_budget as leaf
from coordinator_core.hooks import flag_em_poll_in_flight as fl
from coordinator_core.hooks import nudge_em_code_dispatch as mod
from coordinator_core.ops.dispatch_emit import halted_run

SID = "sess-postfire"
OWNED = "pkg/mod.py"


@pytest.fixture
def root(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "state" / "handoffs").mkdir(parents=True)
    (repo / "scratch").mkdir()
    (repo / "docs").mkdir()
    for target in (mod, leaf):
        monkeypatch.setattr(target, "_is_bootstrap_or_out_of_repo", lambda file_path: False)
    monkeypatch.setattr(mp, "guard_level", lambda name: "warn")
    monkeypatch.setattr(leaf, "_is_scratch_or_tmp", lambda p: "scratch" in Path(p).parts)
    return repo


def _fire(monkeypatch, runs):
    monkeypatch.setattr(fl, "session_workflow_run_ids", lambda sid: runs)


def _declare(root: Path, run_id: str = "run-1", writes=(OWNED,), marker: str | None = None) -> None:
    run_dir = root / "state" / "warp" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": list(writes)}], "doc_writes": []}), encoding="utf-8")
    if marker:
        (run_dir / halted_run.HALTED_FILE).write_text(json.dumps({"status": marker}), encoding="utf-8")


def _payload(root: Path, tool: str, rel: str, **extra) -> dict:
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    tool_input = {key: str(root / rel), "old_string": "a = 1\n", "new_string": "a = f(1)\n"}
    tool_input.update(extra)
    return {"tool_name": tool, "session_id": SID, "cwd": str(root), "tool_input": tool_input}


def _is_deny(result) -> bool:
    return bool(result) and result["hookSpecificOutput"].get("permissionDecision") == "deny"


def _advised(result) -> bool:
    return bool(result) and "workflow.revise" in result["hookSpecificOutput"].get("additionalContext", "")


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
def test_live_run_owned_path_is_advised_never_denied(root, monkeypatch, tool):
    _fire(monkeypatch, ["run-1"])
    _declare(root)
    result = mod.op(_payload(root, tool, OWNED))
    assert _advised(result) and not _is_deny(result)


@pytest.mark.parametrize("rel", ["CLAUDE.md", "sub/CLAUDE.md", ".claude/CLAUDE.md"])
def test_fired_claude_md_write_names_the_grant_route(root, monkeypatch, rel):
    from coordinator_core.session.claude_md_grant import GRANT_CLI_NAME

    _fire(monkeypatch, ["run-1"])
    _declare(root, writes=(rel,))
    result = mod.op(_payload(root, "Edit", rel))
    assert _advised(result) and not _is_deny(result)
    text = result["hookSpecificOutput"]["additionalContext"]
    assert GRANT_CLI_NAME in text and "workflow.revise" in text
    assert "from the repo that holds the CLAUDE.md" in text


def test_multiedit_edits_path_of_a_live_run_is_advised(root, monkeypatch):
    _fire(monkeypatch, ["run-1"])
    _declare(root)
    payload = {
        "tool_name": "MultiEdit",
        "session_id": SID,
        "cwd": str(root),
        "tool_input": {"edits": [{"file_path": str(root / OWNED), "old_string": "a", "new_string": "b"}]},
    }
    result = mod.op(payload)
    assert _advised(result) and not _is_deny(result)


def test_live_runs_plan_writes_are_advised(root, monkeypatch):
    _fire(monkeypatch, ["run-2"])
    run = root / "state" / "warp" / "run-2"
    run.mkdir(parents=True)
    (run / "run.json").write_text('{"sizing_rel": "state/sizings/s.yaml"}')
    (root / "state" / "sizings").mkdir()
    (root / "state" / "sizings" / "s.yaml").write_text('plan: "docs/plans/p.md"\n')
    (root / "docs" / "plans").mkdir()
    (root / "docs" / "plans" / "p.md").write_text("- id: C1\n  writes:\n    - pkg/live.py\n")
    assert _advised(mod.op(_payload(root, "Edit", "pkg/live.py")))
    assert not _advised(mod.op(_payload(root, "Edit", "pkg/other.py")))


@pytest.mark.parametrize("marker", ["halted", "landed"])
def test_halted_or_landed_run_owns_nothing(root, monkeypatch, marker):
    _fire(monkeypatch, ["run-1"])
    _declare(root, marker=marker)
    result = mod.op(_payload(root, "Edit", OWNED))
    assert not _advised(result) and not _is_deny(result)


def test_live_run_does_not_own_an_undeclared_path(root, monkeypatch):
    _fire(monkeypatch, ["run-1"])
    _declare(root)
    result = mod.op(_payload(root, "Edit", "pkg/other.py"))
    assert not _advised(result) and not _is_deny(result)


@pytest.mark.parametrize("rel", ["state/handoffs/h.md", "scratch/probe.txt", ".coordinator-local/x/y.md"])
def test_fired_non_product_paths_are_not_advised(root, monkeypatch, rel):
    _fire(monkeypatch, ["run-1"])
    _declare(root, writes=(rel,))
    result = mod.op(_payload(root, "Write", rel, content="x"))
    assert not _advised(result) and not _is_deny(result)


def test_path_outside_the_repo_is_not_advised(root, monkeypatch, tmp_path):
    _fire(monkeypatch, ["run-1"])
    _declare(root)
    payload = _payload(root, "Edit", OWNED)
    payload["tool_input"]["file_path"] = str(tmp_path / "elsewhere" / "mod.py")
    assert not _advised(mod.op(payload))


def test_subagent_is_unaffected(root, monkeypatch):
    _fire(monkeypatch, ["run-1"])
    _declare(root)
    payload = _payload(root, "Edit", OWNED)
    payload["agent_id"] = "agent-1"
    assert mod.op(payload) is None


def test_unfired_session_gets_no_post_fire_record(root, monkeypatch):
    _fire(monkeypatch, [])
    monkeypatch.setattr(mod, "_session_sizing_state", lambda payload, sid: (True, False))
    monkeypatch.setattr(leaf, "_session_sizing_state", lambda payload, sid: (True, False))
    result = mod.op(_payload(root, "Edit", OWNED))
    assert not _advised(result)


def test_post_fire_record_decides_on_liveness(root, monkeypatch):
    _fire(monkeypatch, ["run-1"])
    assert mod._post_fire_deny(SID, (), str(root)) is not None
    _declare(root, marker="landed")
    assert mod._post_fire_deny(SID, (), str(root)) is None
    _fire(monkeypatch, [])
    assert mod._post_fire_deny(SID, (), str(root)) is None

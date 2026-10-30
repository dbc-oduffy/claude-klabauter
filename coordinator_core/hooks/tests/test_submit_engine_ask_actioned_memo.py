"""A submission whose context names an actioned or closed cross-repo memo proceeds with an advisory; it is never denied."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod


def _memo(root: Path, folder: str, name: str, status: str, extra: str = "") -> str:
    d = root / "state" / "cross-repo" / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(f'---\ntitle: "t"\nstatus: {status}\n{extra}---\n\nbody\n', encoding="utf-8")
    return f"state/cross-repo/{folder}/{name}"


def _payload(root: Path, args: dict) -> dict:
    return {
        "tool_name": "Workflow",
        "cwd": str(root),
        "session_id": "s1",
        "tool_input": {"name": mod.SUBMIT_WORKFLOW_NAME, "args": args},
    }


def _emit(params: dict, root: Path) -> dict:
    return {"path": str(root / "run.js"), "run_id": "r1", "fire_args": {}}


def _text(env: dict) -> str:
    hso = env["hookSpecificOutput"]
    return f"{hso.get('permissionDecisionReason', '')} {hso.get('additionalContext', '')} {env}"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


@pytest.mark.parametrize("status", ["actioned", "closed"])
def test_resolved_memo_is_advised_not_denied(repo, status):
    rel = _memo(repo, "inbox", "m1.md", status, "actioned_commit: abc1234def\n")
    env = mod.evaluate(_payload(repo, {"ask": "do it", "source": "relayed", "context": rel}), emit=_emit)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert f"m1.md is {status} (commit abc1234def)" in _text(env)


def test_commit_is_read_from_the_actioned_note(repo):
    rel = _memo(repo, "inbox", "n.md", "actioned", "actioned_note: 'landed at commit 05b60b15b; done'\n")
    env = mod.evaluate(_payload(repo, {"ask": "x", "source": "relayed", "context": rel}), emit=_emit)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "n.md is actioned (commit 05b60b15b)" in _text(env)


def test_resolved_memo_without_a_commit_says_so(repo):
    rel = _memo(repo, "inbox", "bare.md", "closed")
    env = mod.evaluate(_payload(repo, {"ask": "x", "source": "relayed", "context": rel}), emit=_emit)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "bare.md is closed (no commit recorded)" in _text(env)


def test_one_element_asks_item_is_advised_not_denied(repo):
    done = _memo(repo, "inbox", "done.md", "actioned")
    env = mod.evaluate(_payload(repo, {"asks": [{"ask": "b", "context": done}], "source": "relayed"}), emit=_emit)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "done.md is actioned" in _text(env)


def test_archived_memo_is_advised_not_denied(repo):
    rel = _memo(repo, "archive", "old.md", "closed")
    env = mod.evaluate(_payload(repo, {"ask": "x", "source": "relayed", "context": [rel]}), emit=_emit)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "old.md is closed" in _text(env)


def test_open_memo_carries_no_advisory(repo):
    rel = _memo(repo, "inbox", "live.md", "open")
    env = mod.evaluate(_payload(repo, {"ask": "x", "source": "relayed", "context": rel}), emit=_emit)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "Advisory: context memo" not in _text(env)


def test_actioned_status_outside_a_memo_folder_carries_no_advisory(repo):
    (repo / "docs").mkdir()
    (repo / "docs" / "n.md").write_text("---\nstatus: actioned\n---\n", encoding="utf-8")
    env = mod.evaluate(_payload(repo, {"ask": "x", "source": "relayed", "context": "docs/n.md"}), emit=_emit)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "Advisory: context memo" not in _text(env)

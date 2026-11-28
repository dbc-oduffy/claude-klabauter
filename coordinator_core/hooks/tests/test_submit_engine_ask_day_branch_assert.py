"""A cross-repo `repo:` target on a published `main` is refused at submit, before emit, naming day-branch-assert."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import warp_fire
from coordinator_core.git.main_commit_policy import REMEDY
from coordinator_core.hooks import submit_engine_ask as mod


def _repo(base: Path, name: str, head: str = "main", remote: bool = False) -> Path:
    root = base / name
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text(f"ref: refs/heads/{head}\n", encoding="utf-8")
    if remote:
        (git / "config").write_text(
            '[remote "origin"]\n\turl = https://example.com/o/r.git\n', encoding="utf-8"
        )
    return root


class _Recorder:
    def __init__(self):
        self.calls: list = []

    def __call__(self, params, root):
        self.calls.append((params, root))
        raise RuntimeError("recorder reached")


def _payload(own: Path, args) -> dict:
    return {
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": args},
        "session_id": "sess-daybranch",
        "transcript_path": str(own / "transcript.jsonl"),
        "cwd": str(own),
        "env": {"CLAUDE_PROJECT_DIR": str(own)},
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(mod, "_is_first_turn", lambda _p: True)


def _reason(out: dict) -> str:
    hso = out.get("hookSpecificOutput") or {}
    assert hso.get("permissionDecision") == "deny", out
    return hso["permissionDecisionReason"]


def test_target_on_main_with_a_remote_is_refused_before_emit(tmp_path):
    own = _repo(tmp_path, "own")
    other = _repo(tmp_path, "other", head="main", remote=True)
    emit = _Recorder()
    out = mod.evaluate(
        _payload(own, {"ask": "a", "repo": str(other), "cross_repo_approved": True}), emit=emit
    )
    reason = _reason(out)
    assert "day-branch-assert" in reason and REMEDY in reason and str(other) in reason
    assert emit.calls == []


def test_target_on_a_day_branch_submits_unchanged(tmp_path):
    own = _repo(tmp_path, "own")
    other = _repo(tmp_path, "other", head="example-operator/machine-a/2026-11-24", remote=True)
    emit = _Recorder()
    mod.evaluate(_payload(own, {"ask": "a", "repo": str(other), "cross_repo_approved": True}), emit=emit)
    assert emit.calls


def test_target_on_main_without_a_remote_submits_unchanged(tmp_path):
    own = _repo(tmp_path, "own")
    other = _repo(tmp_path, "other", head="main", remote=False)
    emit = _Recorder()
    mod.evaluate(_payload(own, {"ask": "a", "repo": str(other), "cross_repo_approved": True}), emit=emit)
    assert emit.calls


def test_own_repo_on_main_is_not_this_checks_business(tmp_path):
    own = _repo(tmp_path, "own", head="main", remote=True)
    assert mod._main_branch_refusal(own, own) is None


def test_branch_check_spawns_no_process(tmp_path, monkeypatch):
    import subprocess

    own = _repo(tmp_path, "own")
    other = _repo(tmp_path, "other", head="main", remote=True)

    def _boom(*a, **k):
        raise AssertionError("the branch read must not spawn")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    assert "day-branch-assert" in (mod._main_branch_refusal(other, own) or "")

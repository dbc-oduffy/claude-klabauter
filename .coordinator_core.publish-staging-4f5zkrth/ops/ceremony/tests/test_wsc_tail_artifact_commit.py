"""
coordinator_core.ops.ceremony.tests.test_wsc_tail_artifact_commit

C7 (docs/plans/2026-08-20-the-close-ceremony-commits-what-the-session-wrote.md
§ C7): the `/workstream-complete` half of AC5/AC7/AC9 -- `ceremony.wsc_tail`
calling C4's hardened `auto_commit_session_async` in-process, within its own
sub-2s invoke budget (`/quick-wrap`'s half landed in C5).

Coverage:
  (a) the call threads `_handler`'s own resolved `worktree_root` as `cwd`
      explicitly -- never the `cwd=None` default that resolves to the
      ambient process cwd (the defect that blocked C5's first dispatch,
      3.386s against this repo's real ~26k-file tree).
  (b) a failure inside the call never prevents the rest of the close
      (receipt emit, exit-code computation) from completing.
  (c) AC9: the outcome is rendered into the ceremony's own result as a named
      `artifact_commit` fact, not merely a log line.
  (d) a peer-claimed artifact beside this session's own survives the close
      uncommitted -- real end-to-end run, no mock, real git + real session
      claims (`core.init`/`scope.touch`).

Spec backlink: coordinator_core/ops/ceremony/wsc_tail.py
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

import coordinator_core.ops.ceremony.wsc_tail as wsc_tail_mod
from coordinator_core.session import core, scope

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _make_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "wsc-tail-artifact-commit@makima.test"], root)
    _git(["config", "user.name", "WSC Tail Artifact Commit Test"], root)
    _git(["config", "commit.gpgsign", "false"], root)
    (root / "state" / "handoffs").mkdir(parents=True)
    (root / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "chore: initial skeleton"], root)
    return root


def _porcelain(root: Path) -> list[str]:
    return [ln for ln in _git(["status", "--porcelain"], root).stdout.splitlines() if ln]


_UID = 0


def _sid() -> str:
    global _UID
    _UID += 1
    return f"wsc-tail-artifact-commit-{_UID}"


def _handler_params(sid: str) -> dict:
    return {
        "sid": sid,
        "subject": "workstream-complete: artifact-commit test",
        "stage_paths": ["tasks/feature/todo.md"],
        "caller_paths": ["tasks/feature/todo.md"],
    }


def _seed_stage_path(root: Path) -> None:
    (root / "tasks" / "feature").mkdir(parents=True, exist_ok=True)
    (root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")


def _canned_report(session_id: str, committed_paths: list[str]) -> dict:
    return {
        "session_id": session_id,
        "groups": [],
        "excluded": [],
        "failed_groups": [],
        "dropped_groups": [],
        "residue": {},
        "outcome": {
            "status": "committed" if committed_paths else "empty",
            "detail": "test-canned",
            "committed_paths": committed_paths,
            "conflicted_paths": [],
        },
    }


def test_artifact_commit_uses_explicit_worktree_root_never_defaults(tmp_path, monkeypatch):
    """(a) -- `_handler`'s own resolved `worktree_root` is threaded through as
    `cwd`, explicitly, on every call -- never left to `auto_commit_session_async`'s
    `cwd=None` default (which resolves to the ambient PROCESS cwd, the defect
    this chunk's brief names as the actual cause of C5's first-dispatch
    3.386s KPI blow)."""
    repo = _make_repo(tmp_path)
    _seed_stage_path(repo)
    sid = _sid()

    calls: list[dict] = []

    async def _fake_auto_commit(session_id, cwd=None, groups=None, invoker=None):
        calls.append({"session_id": session_id, "cwd": cwd, "invoker": invoker})
        return _canned_report(session_id, [])

    monkeypatch.setattr(wsc_tail_mod, "auto_commit_session_async", _fake_auto_commit)
    monkeypatch.setattr(wsc_tail_mod, "_spawn_deferred_push_skip_loud", lambda wt: None)

    result = _run(
        wsc_tail_mod._handler(_handler_params(sid), repo_root=(repo / ".git").resolve())
    )

    assert result["exit_code"] in (0, 2), result
    assert len(calls) == 1, calls
    assert calls[0]["cwd"] is not None, "cwd must never be left at its None default"
    assert calls[0]["cwd"] == str(repo.resolve()), calls[0]
    assert calls[0]["session_id"] == sid


def test_artifact_commit_failure_does_not_prevent_close(tmp_path, monkeypatch):
    """(b) -- a failure inside the call never prevents the rest of the
    ceremony (receipt emit, exit-code computation, sentinel clear) from
    completing. Standing § Anti-scope constraint: a failure inside the
    commit reports and proceeds rather than refusing the close."""
    repo = _make_repo(tmp_path)
    _seed_stage_path(repo)
    sid = _sid()

    async def _raising_auto_commit(session_id, cwd=None, groups=None, invoker=None):
        raise RuntimeError("boom -- artifact commit exploded")

    monkeypatch.setattr(wsc_tail_mod, "auto_commit_session_async", _raising_auto_commit)
    monkeypatch.setattr(wsc_tail_mod, "_spawn_deferred_push_skip_loud", lambda wt: None)

    result = _run(
        wsc_tail_mod._handler(_handler_params(sid), repo_root=(repo / ".git").resolve())
    )

    # The ceremony's own commit still landed -- a failed artifact commit is
    # soft-failed, not a refusal.
    assert result["committed_sha"] is not None, result
    assert result["exit_code"] in (0, 2), result
    assert result["receipt_path"], "receipt emit must still complete"

    artifact_result = result["tail_results"][wsc_tail_mod.OP_SESSION_ARTIFACT_COMMIT]
    assert any("boom" in f for f in artifact_result["failed"]), artifact_result
    assert result["artifact_commit"]["status"] == "error"
    assert "boom" in result["artifact_commit"]["detail"]


def test_artifact_commit_outcome_rendered_in_result(tmp_path, monkeypatch):
    """(c) AC9 -- the outcome is rendered into the ceremony's own result as a
    named fact (`result["artifact_commit"]`), not a log line/stderr, and the
    same outcome feeds a `tail_results` D-node so it also lands in the
    persisted receipt."""
    repo = _make_repo(tmp_path)
    _seed_stage_path(repo)
    sid = _sid()

    async def _fake_auto_commit(session_id, cwd=None, groups=None, invoker=None):
        return _canned_report(session_id, ["state/some/artifact.md"])

    monkeypatch.setattr(wsc_tail_mod, "auto_commit_session_async", _fake_auto_commit)
    monkeypatch.setattr(wsc_tail_mod, "_spawn_deferred_push_skip_loud", lambda wt: None)

    result = _run(
        wsc_tail_mod._handler(_handler_params(sid), repo_root=(repo / ".git").resolve())
    )

    assert result["artifact_commit"]["status"] == "committed"
    assert result["artifact_commit"]["committed_paths"] == ["state/some/artifact.md"]
    assert result["artifact_commit"]["residue"] == {}

    artifact_result = result["tail_results"][wsc_tail_mod.OP_SESSION_ARTIFACT_COMMIT]
    assert artifact_result["acted"] == ["state/some/artifact.md"]
    assert artifact_result["failed"] == []


# designed_red: blocked on the `ceremony.scoped_git_commit` op SUSPENSION
# (coordinator_core/op_budget_suspension.py, PM ruling 2026-08-21: measured
# max 150021ms against a 2000ms bar). Reached here through the deliberately
# un-mocked `auto_commit_session_async` -> `safe_commit_offer._commit_group`,
# which resolves that op by key via `ipc.get_op_handler` and so takes
# `OpSuspendedError`. `ceremony.wsc_tail`'s own suspension is NOT the cause --
# this test calls `_handler` directly and never crosses dispatch. NOT the
# attribution kill either: that was rebuilt and `_MECHANISM_DISABLED` is gone.
# Re-greens when the op is proven under 2s and leaves the roster, and not
# before; nothing in this module can lift it.
@pytest.mark.designed_red
def test_peer_claimed_artifact_survives_uncommitted(tmp_path, monkeypatch):
    """(d) -- real end-to-end run (no mock of `auto_commit_session_async`):
    a peer session's claimed dirty artifact beside this session's own
    remains untracked/dirty after the close -- the artifact commit's own
    peer-isolation (C4) survives being called from inside `ceremony.wsc_tail`."""
    repo = _make_repo(tmp_path)
    _seed_stage_path(repo)
    sid = _sid()
    peer_sid = f"{sid}-peer"

    core.init(sid, cwd=str(repo))
    core.init(peer_sid, cwd=str(repo))

    (repo / "state" / "artifacts").mkdir(parents=True, exist_ok=True)
    (repo / "state" / "artifacts" / "mine.md").write_text("mine", encoding="utf-8")
    (repo / "state" / "artifacts" / "peer.md").write_text("peer", encoding="utf-8")
    scope.touch(sid, "state/artifacts/mine.md", cwd=str(repo))
    scope.touch(peer_sid, "state/artifacts/peer.md", cwd=str(repo))

    monkeypatch.setattr(wsc_tail_mod, "_spawn_deferred_push_skip_loud", lambda wt: None)

    result = _run(
        wsc_tail_mod._handler(_handler_params(sid), repo_root=(repo / ".git").resolve())
    )

    assert result["committed_sha"] is not None, result
    assert result["artifact_commit"]["status"] == "committed", result["artifact_commit"]
    assert "state/artifacts/mine.md" in result["artifact_commit"]["committed_paths"]
    assert "state/artifacts/peer.md" not in result["artifact_commit"]["committed_paths"]

    porcelain = _porcelain(repo)
    assert any("peer.md" in ln for ln in porcelain), (
        "peer-claimed artifact must survive uncommitted -- residue stays dirty "
        "on disk for the next session's own workstream-start dirty-tree read"
    )
    assert not any("mine.md" in ln for ln in porcelain), porcelain

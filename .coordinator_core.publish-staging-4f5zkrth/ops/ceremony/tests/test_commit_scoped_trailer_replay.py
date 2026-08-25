"""
coordinator_core.ops.ceremony.tests.test_commit_scoped_trailer_replay

Tests AC18 (docs/plans/2026-07-27-computed-commit-mechanism-selection.md
chunk C10-remainder): `git_native.commit_scoped()`'s diverged (private-
index) branch lands via `git commit-tree`, which runs no git hooks -- so
without an explicit replay, a commit landed through that branch would
silently omit the Session-Id/Deliverable-Id trailers the
`prepare-commit-msg` hook stamps on every ordinary `git commit` (the agree
branch). This suite asserts trailer PARITY between the two branches inside
one session, using real git (mocked git has no index, so the agree/diverged
distinction this selector exists to compute cannot be exhibited).

Coverage:
  - parity: an agree-branch commit and a diverged-branch commit in the same
    session carry equal Session-Id and Deliverable-Id trailers.
  - red-proof: with the replay call removed, the diverged-branch commit
    loses both trailers while the agree-branch commit keeps them -- proving
    the parity assertion actually depends on the fix under test.

Spec backlink: docs/plans/2026-07-27-computed-commit-mechanism-selection.md
chunk C10-remainder (AC18).
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import git_native
from .fixtures.real_git import make_agree_path, make_diverged_path, real_git_repo

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_SESSION_ID = "abcdef12-3456-7890-abcd-ef1234567890"
_DELIVERABLE_ID = "deliverable-c10-remainder"

# coordinator_core/ops/ceremony/tests/<this file> -> repo root is 4 parents up.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_REAL_HOOK_SCRIPT = _REPO_ROOT / "coordinator" / "bin" / "coordinator-prepare-commit-msg"


def _install_real_prepare_commit_msg_hook(repo: Path) -> None:
    """Install the ACTUAL production hook
    (`coordinator/bin/coordinator-prepare-commit-msg`) into this throwaway
    repo's `.git/hooks/`, so the AGREE branch (plain `git commit`, which
    fires hooks normally) stamps trailers the same way it does on a real
    coordinator working tree. `real_git_repo()` inits a bare-bones throwaway
    repo with no hooks installed at all -- without this, NEITHER branch
    would carry trailers and the parity assertion below would pass
    vacuously."""
    assert _REAL_HOOK_SCRIPT.is_file(), f"hook script not found: {_REAL_HOOK_SCRIPT}"
    hooks_dir = Path(
        subprocess.run(
            ["git", "rev-parse", "--absolute-git-dir"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip()
    ) / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "prepare-commit-msg"
    # Both paths go through `as_posix()` and are quoted: git runs hooks under
    # its bundled `sh` on Windows too, where a native `C:\...\python.exe`
    # spelling has its backslashes eaten as shell escapes (the hook then dies
    # with `exec: C:UsersoduffyAppData...: not found`). Forward slashes with a
    # drive letter are understood by that shell, and are unchanged on POSIX.
    hook_path.write_text(
        '#!/bin/sh\nexec "%s" "%s" "$@"\n'
        % (Path(sys.executable).as_posix(), _REAL_HOOK_SCRIPT.as_posix()),
        encoding="utf-8",
    )
    hook_path.chmod(hook_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _write_msg(tmp_path: Path, name: str, text: str) -> Path:
    msg_file = tmp_path / name
    msg_file.write_text(text, encoding="utf-8")
    return msg_file


def _seed_session_shape(repo: Path, session_id: str, deliverable_id: str) -> None:
    """Write `<git-dir>/coordinator-sessions/<session_id>/session-shape.json`
    so `_resolve_deliverable_id` (mirrored from the hook) has a
    `pickup.deliverable_id` to resolve -- verbatim shape the hook expects."""
    git_dir = Path(
        _git(["rev-parse", "--absolute-git-dir"], repo).stdout.strip()
    )
    shape_dir = git_dir / "coordinator-sessions" / session_id
    shape_dir.mkdir(parents=True, exist_ok=True)
    (shape_dir / "session-shape.json").write_text(
        json.dumps({"pickup": {"deliverable_id": deliverable_id}}),
        encoding="utf-8",
    )


def _trailer_value(commit_message: str, prefix: str) -> str | None:
    for line in commit_message.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def _log_message(repo: Path, sha: str) -> str:
    return _git(["log", "-1", "--format=%B", sha], repo).stdout


@pytest.fixture()
def session_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SESSION_ID)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    return _SESSION_ID


def test_agree_and_diverged_branch_trailers_match(tmp_path, session_env):
    repo = real_git_repo(tmp_path)
    _install_real_prepare_commit_msg_hook(repo)
    _seed_session_shape(repo, session_env, _DELIVERABLE_ID)

    make_agree_path(repo, "agree.txt", "agree content\n")
    agree_msg = _write_msg(tmp_path, "agree_msg.txt", "agree commit\n")
    agree_result = git_native.commit_scoped(["agree.txt"], agree_msg, repo)
    assert agree_result.ok, agree_result.stderr
    agree_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    agree_message = _log_message(repo, agree_sha)

    make_diverged_path(
        repo, "diverged.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n"
    )
    diverged_msg = _write_msg(tmp_path, "diverged_msg.txt", "diverged commit\n")
    diverged_result = git_native.commit_scoped(["diverged.txt"], diverged_msg, repo)
    assert diverged_result.ok, diverged_result.stderr
    diverged_sha = diverged_result.stdout.strip()
    diverged_message = _log_message(repo, diverged_sha)

    agree_session_id = _trailer_value(agree_message, "Session-Id:")
    diverged_session_id = _trailer_value(diverged_message, "Session-Id:")
    assert agree_session_id == session_env, agree_message
    assert diverged_session_id == session_env, diverged_message
    assert agree_session_id == diverged_session_id

    agree_deliverable_id = _trailer_value(agree_message, "Deliverable-Id:")
    diverged_deliverable_id = _trailer_value(diverged_message, "Deliverable-Id:")
    assert agree_deliverable_id == _DELIVERABLE_ID, agree_message
    assert diverged_deliverable_id == _DELIVERABLE_ID, diverged_message
    assert agree_deliverable_id == diverged_deliverable_id


def test_diverged_branch_resolves_artifact_tier0_deliverable_id(tmp_path, session_env):
    """2026-08-04 threading fix: `_commit_scoped_private_index` passes its
    own committed pathspec through to `compute_missing_trailer_args(...,
    paths=...)`, so the diverged (private-index) branch resolves
    Deliverable-Id from a COMMITTED ARTIFACT's own `deliverable_id`
    frontmatter (tier 0) ahead of the session's pickup record whenever the
    two disagree -- prior to this fix `paths=` was never threaded through,
    so tier 0 was permanently inert on this branch. `docs/diverged.md`
    supplies the divergence that routes into the private-index branch at
    all; `state/handoffs/baton.md` is deliberately NOT diverged (staged ==
    worktree) so its on-disk read at trailer-computation time is the same
    content actually being committed -- isolates this test to the paths-
    threading fix, not the separate (out-of-scope) question of resolving a
    DIVERGED artifact's own staged-vs-worktree frontmatter."""
    repo = real_git_repo(tmp_path)
    _install_real_prepare_commit_msg_hook(repo)
    _seed_session_shape(repo, session_env, _DELIVERABLE_ID)

    artifact_deliverable_id = "deliverable-artifact-own-value"
    make_agree_path(
        repo,
        "state/handoffs/baton.md",
        f'---\ntitle: baton\ndeliverable_id: "{artifact_deliverable_id}"\n---\n\nbody\n',
    )
    make_diverged_path(
        repo, "docs/diverged.md", staged_content="STAGED\n", worktree_content="WORKTREE\n"
    )
    msg = _write_msg(tmp_path, "diverged_tier0_msg.txt", "archive baton + hunk\n")

    result = git_native.commit_scoped(
        ["state/handoffs/baton.md", "docs/diverged.md"], msg, repo
    )
    assert result.ok, result.stderr
    sha = result.stdout.strip()
    message = _log_message(repo, sha)

    assert artifact_deliverable_id != _DELIVERABLE_ID
    assert _trailer_value(message, "Deliverable-Id:") == artifact_deliverable_id, message


def test_diverged_branch_omits_trailers_without_replay(tmp_path, session_env, monkeypatch):
    """Red-proof: with the replay call neutered, the diverged branch's
    commit-tree loses both trailers even though the agree branch (hooks
    fire normally) still carries them -- proving the parity test above
    actually exercises the AC18 fix rather than passing vacuously."""
    monkeypatch.setattr(
        git_native, "compute_missing_trailer_args", lambda *args, **kwargs: []
    )

    repo = real_git_repo(tmp_path)
    _install_real_prepare_commit_msg_hook(repo)
    _seed_session_shape(repo, session_env, _DELIVERABLE_ID)

    make_agree_path(repo, "agree.txt", "agree content\n")
    agree_msg = _write_msg(tmp_path, "agree_msg.txt", "agree commit\n")
    agree_result = git_native.commit_scoped(["agree.txt"], agree_msg, repo)
    assert agree_result.ok, agree_result.stderr
    agree_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    agree_message = _log_message(repo, agree_sha)

    make_diverged_path(
        repo, "diverged.txt", staged_content="STAGED\n", worktree_content="WORKTREE\n"
    )
    diverged_msg = _write_msg(tmp_path, "diverged_msg.txt", "diverged commit\n")
    diverged_result = git_native.commit_scoped(["diverged.txt"], diverged_msg, repo)
    assert diverged_result.ok, diverged_result.stderr
    diverged_sha = diverged_result.stdout.strip()
    diverged_message = _log_message(repo, diverged_sha)

    assert _trailer_value(diverged_message, "Session-Id:") is None, diverged_message
    assert _trailer_value(diverged_message, "Deliverable-Id:") is None, diverged_message

"""Tests for coordinator_core.ops.push_failure_verdict.

All git exercise runs against throwaway repos created fresh under
`tmp_path` per test — NEVER against this working repo. See module
docstring for the op-key/contract: `git.push_failure_verdict`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.push_failure_verdict import (
    _handler,
    classify,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(*args: str, cwd: Path, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
        timeout=30,
        stdin=subprocess.DEVNULL,
        env=env,
    )


@pytest.fixture(autouse=True)
def _isolate_global_git_config(tmp_path, monkeypatch):
    """Isolate from the ambient dev machine's global git config, mirroring
    the same fixture in test_merge_quiet_activity_gate.py."""
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def _init_repo(root: Path, *, bare: bool = False) -> None:
    root.mkdir(parents=True, exist_ok=True)
    args = ["init", "-q", "-b", "main"]
    if bare:
        args.append("--bare")
    _git(*args, cwd=root)
    if not bare:
        _git("config", "user.email", "test@example.com", cwd=root)
        _git("config", "user.name", "Test", cwd=root)


def _commit_file(root: Path, rel_path: str, content: str, msg: str | None = None) -> None:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git("add", rel_path, cwd=root)
    _git("commit", "-q", "-m", msg or f"add {rel_path}", cwd=root)


def _clone_with_upstream(tmp_path: Path) -> tuple[Path, Path]:
    """Bare origin + a clone with an initial commit pushed, tracking main."""
    origin = tmp_path / "origin.git"
    _init_repo(origin, bare=True)

    seed = tmp_path / "seed"
    _init_repo(seed)
    _commit_file(seed, "README.md", "seed\n")
    _git("remote", "add", "origin", str(origin), cwd=seed)
    _git("push", "-u", "origin", "main", cwd=seed)

    clone = tmp_path / "clone"
    _git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=clone)
    _git("config", "user.name", "Test", cwd=clone)
    _git("checkout", "-q", "-B", "main", "origin/main", cwd=clone)
    _git("branch", "-q", "--set-upstream-to=origin/main", "main", cwd=clone)
    return origin, clone


def _push_extra_commits_and_fetch(origin: Path, clone: Path, files: list[str]) -> None:
    """Land `files` as new commits on origin (via a throwaway pusher clone),
    then `git fetch` inside `clone` so `@{u}` reflects them WITHOUT touching
    clone's own working tree/index (produces a clean 'behind' state)."""
    pusher = origin.parent / "pusher"
    _git("clone", "-q", str(origin), str(pusher), cwd=origin.parent)
    _git("config", "user.email", "test@example.com", cwd=pusher)
    _git("config", "user.name", "Test", cwd=pusher)
    for rel_path in files:
        _commit_file(pusher, rel_path, f"content for {rel_path}\n", msg=f"add {rel_path}")
    _git("push", "-q", cwd=pusher)
    _git("fetch", "-q", cwd=clone)


def _write_push_failures_log(clone: Path, lines: list[str]) -> None:
    common_dir_result = _git("rev-parse", "--git-common-dir", cwd=clone)
    common_dir = Path(common_dir_result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = clone / common_dir
    log_path = common_dir / "push-failures.log"
    log_path.write_text("\n".join(lines) + "\n")


def test_no_upstream_is_indeterminate(tmp_path):
    root = tmp_path / "solo"
    _init_repo(root)
    _commit_file(root, "a.txt", "hello\n")

    result = classify(root)

    assert result["verdict"] == "indeterminate"
    assert result["evidence"]["upstream_resolved"] is False


def test_not_a_git_repo_is_indeterminate(tmp_path):
    root = tmp_path / "not-a-repo"
    root.mkdir()

    result = classify(root)

    assert result["verdict"] == "indeterminate"


def test_simple_lag_when_behind_and_clean(tmp_path):
    origin, clone = _clone_with_upstream(tmp_path)
    _push_extra_commits_and_fetch(origin, clone, ["new1.txt", "new2.txt"])

    result = classify(clone)

    assert result["verdict"] == "simple_lag"
    assert result["evidence"]["behind"] == 2
    assert result["evidence"]["ahead"] == 0
    assert result["evidence"]["staged_count"] == 0


def test_resolved_since_when_log_stale_but_tree_in_sync(tmp_path):
    origin, clone = _clone_with_upstream(tmp_path)
    _push_extra_commits_and_fetch(origin, clone, ["new1.txt"])
    # Fast-forward clone's local main to match origin -- fully in sync.
    _git("merge", "-q", "--ff-only", "origin/main", cwd=clone)
    _write_push_failures_log(
        clone,
        ["[2026-08-06T14:17:00Z] PUSH FAILED on main (rebase/non-ff after 3) :: rejected"],
    )

    result = classify(clone)

    assert result["verdict"] == "resolved_since"
    assert result["evidence"]["push_failures_log_count"] == 1
    assert result["evidence"]["push_failures_log_newest"] == "2026-08-06T14:17:00Z"
    assert result["evidence"]["ahead"] == 0
    assert result["evidence"]["behind"] == 0


def test_indeterminate_when_clean_and_no_log(tmp_path):
    origin, clone = _clone_with_upstream(tmp_path)

    result = classify(clone)

    assert result["verdict"] == "indeterminate"
    assert result["evidence"]["staged_count"] == 0
    assert result["evidence"]["push_failures_log_count"] == 0


def test_peer_staged_when_staged_content_unrelated_to_incoming(tmp_path):
    origin, clone = _clone_with_upstream(tmp_path)
    _push_extra_commits_and_fetch(origin, clone, ["incoming1.txt", "incoming2.txt"])

    (clone / "mine.txt").write_text("my own unrelated staged file\n")
    _git("add", "mine.txt", cwd=clone)

    result = classify(clone)

    assert result["verdict"] == "peer_staged"
    assert result["evidence"]["staged_count"] == 1


def test_half_applied_merge_when_staged_mirrors_incoming(tmp_path):
    origin, clone = _clone_with_upstream(tmp_path)
    _push_extra_commits_and_fetch(
        origin, clone, ["inc1.txt", "inc2.txt", "inc3.txt", "inc4.txt"]
    )

    # Simulate a failed merge's partial index: stage near-copies of the
    # incoming files, touch nothing else (zero overlap with any unstaged
    # local modification).
    for rel_path in ["inc1.txt", "inc2.txt", "inc3.txt"]:
        (clone / rel_path).write_text(f"content for {rel_path}\n")
        _git("add", rel_path, cwd=clone)

    result = classify(clone)

    assert result["verdict"] == "half_applied_merge"
    assert result["evidence"]["staged_count"] == 3
    assert result["evidence"]["staged_incoming_overlap"] == 3
    assert result["evidence"]["staged_unstaged_overlap"] == 0


def test_half_applied_merge_not_confused_by_unrelated_unstaged_edit(tmp_path):
    origin, clone = _clone_with_upstream(tmp_path)
    _push_extra_commits_and_fetch(origin, clone, ["inc1.txt", "inc2.txt"])

    for rel_path in ["inc1.txt", "inc2.txt"]:
        (clone / rel_path).write_text(f"content for {rel_path}\n")
        _git("add", rel_path, cwd=clone)

    # An unrelated unstaged edit to a tracked file (README.md, seeded).
    (clone / "README.md").write_text("locally edited, unstaged\n")

    result = classify(clone)

    assert result["verdict"] == "half_applied_merge"
    assert result["evidence"]["staged_unstaged_overlap"] == 0


def test_handler_uses_dispatch_repo_root(tmp_path):
    origin, clone = _clone_with_upstream(tmp_path)
    _push_extra_commits_and_fetch(origin, clone, ["new1.txt"])

    result = _handler({}, repo_root=clone)

    assert result["verdict"] == "simple_lag"


def test_double_invocation_is_idempotent(tmp_path):
    origin, clone = _clone_with_upstream(tmp_path)
    _push_extra_commits_and_fetch(origin, clone, ["new1.txt"])

    first = classify(clone)
    second = classify(clone)

    assert first == second

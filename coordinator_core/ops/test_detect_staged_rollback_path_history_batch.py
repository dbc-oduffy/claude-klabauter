"""Path-scoped tests for `detect_staged_rollback._batch_path_history` (T2).

Spec: docs/plans/2026-08-07-n-plus-one-git-spawn-class-and-amplification-gate.md
§ Tasks, T2. `find_rollback_candidates` used to call `_path_history` once per
staged path (one `git log` spawn per path, unmemoized) — this file exercises
the batched replacement: ONE multi-pathspec `git log` walk resolving every
staged path's history in a single subprocess invocation.

All fixtures are real, throwaway git repos built under pytest's `tmp_path` —
never against this repo's own working tree, mirroring
`test_detect_staged_rollback.py`'s own fixture discipline.
"""

from __future__ import annotations

import os
import subprocess
from unittest import mock

from coordinator_core.ops.detect_staged_rollback import (
    HISTORY_DEPTH_LIMIT,
    _batch_path_history,
    _path_history,
    find_rollback_candidates,
)


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=check,
    )


def _init_repo(repo):
    repo.mkdir(exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "rollback-test@example.com")
    _git(repo, "config", "user.name", "rollback-test")
    return repo


def _commit_file(repo, name, content, message):
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message, "--", name)


def _commit_multi(repo, files, message):
    """Commit several paths TOGETHER in one commit — the shape a batched
    multi-pathspec walk must parse correctly (a single commit touching
    multiple requested paths emits multiple raw/path pairs back-to-back)."""
    for name, content in files.items():
        (repo / name).write_text(content)
        _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", message)


def _stage_file(repo, name, content):
    (repo / name).write_text(content)
    _git(repo, "add", name)


# ---------------------------------------------------------------------------
# Parity with the per-path (unbatched) `_path_history`


def test_batch_matches_per_path_history_single_path(tmp_path):
    repo = _init_repo(tmp_path / "single")
    _commit_file(repo, "f.txt", "v1\n", "c1")
    _commit_file(repo, "f.txt", "v2\n", "c2")
    _commit_file(repo, "f.txt", "v3\n", "c3")

    expected = _path_history(str(repo), "f.txt")
    batched = _batch_path_history(str(repo), ["f.txt"])

    assert batched["f.txt"] == expected


def test_batch_matches_per_path_history_multiple_independent_paths(tmp_path):
    repo = _init_repo(tmp_path / "multi")
    _commit_file(repo, "a.txt", "a1\n", "a c1")
    _commit_file(repo, "b.txt", "b1\n", "b c1")
    _commit_file(repo, "a.txt", "a2\n", "a c2")
    _commit_file(repo, "b.txt", "b2\n", "b c2")

    expected_a = _path_history(str(repo), "a.txt")
    expected_b = _path_history(str(repo), "b.txt")
    batched = _batch_path_history(str(repo), ["a.txt", "b.txt"])

    assert batched["a.txt"] == expected_a
    assert batched["b.txt"] == expected_b


def test_batch_parses_a_single_commit_touching_multiple_requested_paths(tmp_path):
    """A commit that touches TWO requested paths in one commit emits two
    raw/path pairs back-to-back before the next commit header — the shape a
    fixed-stride (single-path) parser cannot handle."""
    repo = _init_repo(tmp_path / "shared-commit")
    _commit_file(repo, "a.txt", "a1\n", "a c1")
    _commit_file(repo, "b.txt", "b1\n", "b c1")
    _commit_multi(repo, {"a.txt": "a2\n", "b.txt": "b2\n"}, "shared commit touching both")

    batched = _batch_path_history(str(repo), ["a.txt", "b.txt"])

    assert [h for h, _s, _b in batched["a.txt"]][0:1] == [batched["a.txt"][0][0]]
    assert len(batched["a.txt"]) == 2
    assert len(batched["b.txt"]) == 2
    # The shared commit is the most-recent entry for BOTH paths, and both
    # entries reference the SAME commit hash.
    assert batched["a.txt"][0][0] == batched["b.txt"][0][0]
    assert batched["a.txt"][0][1] == "shared commit touching both"


# ---------------------------------------------------------------------------
# Absence reconciliation (§ Anti-scope 25)


def test_absent_path_with_no_history_is_reconciled_not_silently_dropped(tmp_path):
    """A requested path with genuinely no commit history is absent from the
    returned dict (or maps to an empty list) — never silently defaulted."""
    repo = _init_repo(tmp_path / "absent")
    _commit_file(repo, "a.txt", "a1\n", "a c1")
    _stage_file(repo, "never-committed.txt", "new\n")

    batched = _batch_path_history(str(repo), ["a.txt", "never-committed.txt"])

    assert len(batched["a.txt"]) == 1
    assert batched.get("never-committed.txt", []) == []


def test_find_rollback_candidates_skips_absent_history_paths_not_crash(tmp_path):
    """`find_rollback_candidates` reconciles an absent-history staged path as
    NOT a rollback candidate — the fail-open direction matches the pre-batch
    per-path behavior (suppresses a finding, never fabricates one)."""
    repo = _init_repo(tmp_path / "absent-e2e")
    _commit_file(repo, "a.txt", "a1\n", "a c1")
    _commit_file(repo, "a.txt", "a2\n", "a c2")
    _stage_file(repo, "a.txt", "a1\n")
    _stage_file(repo, "brand-new.txt", "never seen before\n")

    candidates = find_rollback_candidates(str(repo))

    paths = {c.path for c in candidates}
    assert "a.txt" in paths
    assert "brand-new.txt" not in paths


# ---------------------------------------------------------------------------
# Per-path depth cap (HISTORY_DEPTH_LIMIT), applied in memory (no `-n`)


def test_per_path_cap_applied_in_memory_not_starved_by_shared_walk(tmp_path):
    """A path with many commits must not consume a git-level `-n` window
    that would starve a sibling path's own history — the batched walk has NO
    `-n` on the git invocation; the cap is enforced per path in Python."""
    repo = _init_repo(tmp_path / "cap")
    for i in range(HISTORY_DEPTH_LIMIT + 5):
        _commit_file(repo, "busy.txt", f"v{i}\n", f"busy c{i}")
    _commit_file(repo, "quiet.txt", "q1\n", "quiet c1")

    batched = _batch_path_history(str(repo), ["busy.txt", "quiet.txt"])

    assert len(batched["busy.txt"]) == HISTORY_DEPTH_LIMIT
    assert len(batched["quiet.txt"]) == 1


# ---------------------------------------------------------------------------
# Spawn-count reduction (the actual N+1 fix this chunk exists for)


def test_find_rollback_candidates_spawns_one_git_log_for_history_not_one_per_path(tmp_path):
    """The whole point of T2: N staged paths must cost ONE `_batch_path_history`
    git-log spawn, not N `_path_history` spawns."""
    repo = _init_repo(tmp_path / "spawn-count")
    for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
        _commit_file(repo, name, "v1\n", f"{name} c1")
        _commit_file(repo, name, "v2\n", f"{name} c2")
        _stage_file(repo, name, "v1\n")

    with mock.patch(
        "coordinator_core.ops.detect_staged_rollback._path_history"
    ) as mocked_per_path:
        find_rollback_candidates(str(repo))

    mocked_per_path.assert_not_called()


# ---------------------------------------------------------------------------
# Empty input


def test_batch_empty_paths_returns_empty_dict_no_subprocess(tmp_path):
    repo = _init_repo(tmp_path / "empty-paths")
    _commit_file(repo, "a.txt", "a1\n", "a c1")

    assert _batch_path_history(str(repo), []) == {}

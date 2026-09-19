"""
coordinator_core.ops.test_review_freeze_diff — op-level coverage for
`review.freeze_diff` (coordinator_core/ops/review_freeze_diff.py).

Purpose: verifies both layers of the module — the pure `freeze_diff()` core
function (called directly, real tmp_path git repo, no daemon) and the
`@register_op("review.freeze_diff")` handler (params-dict-in, repo_root-kwarg
shape, matching the sibling `review.snapshot_diff_and_head` test convention).
End-to-end CLI coverage over `coordinator/bin/freeze-review-diff.py` (which
imports and delegates to `freeze_diff()` — see that module's "Composing
algorithm" docstring section) lives separately in
`coordinator/tests/test_freeze_review_diff.py`; this file does not duplicate
those CLI-argv-shaped cases.

Spec backlink: cross-repo/inbox/2026-07-23-claude-central-em-review-diff-freeze-op-wanted.md
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.review_freeze_diff as review_freeze_diff  # fires @register_op
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.review_freeze_diff import (
    _handler,
    _validate_slice_id,
    freeze_diff,
    freeze_diffs_batch,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_OP_NAME = "review.freeze_diff"

DIFFS_SUBDIR = ("state", "review-trail", "diffs")


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _init_repo(repo: Path) -> None:
    _git(["init", "-q"], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test Author"], cwd=repo)


def _commit(repo: Path, path: str, content: str, message: str) -> str:
    (repo / path).write_text(content)
    _git(["add", path], cwd=repo)
    cp = _git(["commit", "-q", "-m", message], cwd=repo)
    assert cp.returncode == 0, f"setup commit failed: {cp.stderr!r}"
    return _git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()


def _diffs_dir(repo: Path) -> Path:
    d = repo
    for part in DIFFS_SUBDIR:
        d = d / part
    return d


def test_op_registered() -> None:
    assert _OP_NAME in _REGISTRY, (
        f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
        "@register_op did not fire on import"
    )


# ---------------------------------------------------------------------------
# Core function (freeze_diff) — direct calls over a real tmp_path git repo.
# ---------------------------------------------------------------------------


def test_range_omitted_fails_loud(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "a.txt", "line one\n", "add a.txt")

    result = freeze_diff(tmp_path, "", "no-range")

    assert result["error"] is not None
    assert result["diff_path"] is None
    assert not _diffs_dir(tmp_path).exists()


def test_slice_id_omitted_fails_loud(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "")

    assert result["error"] is not None
    assert not _diffs_dir(tmp_path).exists()


def test_diff_written_to_expected_path(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "wave-1")

    assert result["error"] is None
    expected_diff = _diffs_dir(tmp_path) / "wave-1.diff"
    assert result["diff_path"] == str(expected_diff)
    assert expected_diff.is_file()
    assert "line two" in expected_diff.read_text()
    assert result["empty"] is False


def test_colliding_slice_id_refuses_rather_than_overwriting(tmp_path: Path) -> None:
    # A slice id is a filename: a generic one collides with whatever peer
    # froze it first, and on a shared worktree that file is often tracked.
    # An unconditional write destroyed their evidence silently.
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")
    sha3 = _commit(tmp_path, "b.txt", "peer content\n", "add b.txt")

    first = freeze_diff(tmp_path, f"{sha1}..{sha2}", "wave-1")
    assert first["error"] is None
    frozen = _diffs_dir(tmp_path) / "wave-1.diff"
    original = frozen.read_text()

    second = freeze_diff(tmp_path, f"{sha2}..{sha3}", "wave-1")

    assert second["error"] is not None
    assert "wave-1" in second["error"]
    assert second["diff_path"] is None
    assert frozen.read_text() == original


def test_identical_refreeze_under_same_slice_id_is_idempotent(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    first = freeze_diff(tmp_path, f"{sha1}..{sha2}", "wave-1")
    second = freeze_diff(tmp_path, f"{sha1}..{sha2}", "wave-1")

    assert first["error"] is None
    assert second["error"] is None
    assert second["diff_path"] == first["diff_path"]


def test_head_sha_matches_freeze_time_head(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "wave-2")

    expected_sha_path = _diffs_dir(tmp_path) / "wave-2.head.sha"
    assert result["head_sha_path"] == str(expected_sha_path)
    assert result["head_sha"] == sha2
    assert expected_sha_path.read_text().strip() == sha2

    # HEAD advances after the freeze — the recorded sha must stay pinned to
    # the freeze-time HEAD, not silently track the moved tip.
    _commit(tmp_path, "a.txt", "line one\nline two\nline three\n", "extend again")
    assert expected_sha_path.read_text().strip() == sha2


def test_pathspec_narrows_output(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "a v1\n", "add a.txt")
    (tmp_path / "b.txt").write_text("b v1\n")
    _git(["add", "b.txt"], cwd=tmp_path)
    _git(["commit", "-q", "-m", "add b.txt"], cwd=tmp_path)
    (tmp_path / "a.txt").write_text("a v2\n")
    (tmp_path / "b.txt").write_text("b v2\n")
    _git(["add", "a.txt", "b.txt"], cwd=tmp_path)
    _git(["commit", "-q", "-m", "modify both"], cwd=tmp_path)
    sha2 = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "paths-restrict", paths=["a.txt"])

    assert result["error"] is None
    diff_text = Path(result["diff_path"]).read_text()
    assert "a.txt" in diff_text
    assert "b.txt" not in diff_text


def test_empty_diff_is_valid_outcome(tmp_path: Path) -> None:
    # An empty diff over a range that DOES name commits stays a valid
    # outcome. This used to assert it via "HEAD..HEAD", which
    # `_zero_commit_range_error` now refuses outright (2c510a2857) — a
    # zero-commit range is a malformed request, not an empty result, and
    # the two must not be asserted by the same case.
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "empty-diff", paths=["b.txt"])

    assert result["error"] is None
    assert result["empty"] is True
    assert Path(result["diff_path"]).read_text() == ""


def test_zero_commit_range_refuses(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "a.txt", "line one\n", "add a.txt")

    result = freeze_diff(tmp_path, "HEAD..HEAD", "zero-commit")

    assert result["error"] is not None
    assert "ZERO commits" in result["error"]
    assert result["diff_path"] is None


@pytest.mark.parametrize("bad_slice_id", ["../escape", "sub/dir", "back\\slash"])
def test_slice_id_traversal_rejected(tmp_path: Path, bad_slice_id: str) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", bad_slice_id)

    assert result["error"] is not None
    assert not _diffs_dir(tmp_path).exists()


def test_validate_slice_id_accepts_bare_filename_component() -> None:
    assert _validate_slice_id("weekly-2026-07-26") is None


# ---------------------------------------------------------------------------
# JSON-RPC handler shape.
# ---------------------------------------------------------------------------


def test_handler_requires_repo_root() -> None:
    result = _handler({"range": "a..b", "slice_id": "x"}, repo_root=None)
    assert result["error"] is not None


def test_handler_happy_path(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    result = _handler(
        {"range": f"{sha1}..{sha2}", "slice_id": "handler-wave"}, repo_root=tmp_path
    )

    assert result["error"] is None
    assert Path(result["diff_path"]).is_file()
    assert result["head_sha"] == sha2


def test_handler_paths_must_be_a_list(tmp_path: Path) -> None:
    result = _handler(
        {"range": "a..b", "slice_id": "x", "paths": "not-a-list"}, repo_root=tmp_path
    )
    assert result["error"] is not None


# ---------------------------------------------------------------------------
# freeze_diffs_batch — batch/single-path parity, one git spawn per phase.
# ---------------------------------------------------------------------------


def test_batch_of_two_ranges_equals_two_single_freezes_via_one_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")
    sha3 = _commit(tmp_path, "b.txt", "peer content\n", "add b.txt")

    diff_tree_calls = []
    real_git = review_freeze_diff._git

    def _counting_git(args, **kwargs):
        if args and args[0] == "diff-tree":
            diff_tree_calls.append(args)
        return real_git(args, **kwargs)

    monkeypatch.setattr(review_freeze_diff, "_git", _counting_git)

    batch_results = freeze_diffs_batch(
        tmp_path,
        [
            {"slice_id": "batch-a", "range": f"{sha1}..{sha2}"},
            {"slice_id": "batch-b", "range": f"{sha2}..{sha3}"},
        ],
    )

    assert len(diff_tree_calls) == 1, (
        f"expected exactly one git diff-tree spawn for a two-slice batch, got "
        f"{len(diff_tree_calls)}: {diff_tree_calls!r}"
    )

    single_a = freeze_diff(tmp_path, f"{sha1}..{sha2}", "single-a")
    single_b = freeze_diff(tmp_path, f"{sha2}..{sha3}", "single-b")

    assert batch_results[0]["error"] is None
    assert batch_results[1]["error"] is None
    assert Path(batch_results[0]["diff_path"]).read_text() == Path(
        single_a["diff_path"]
    ).read_text()
    assert Path(batch_results[1]["diff_path"]).read_text() == Path(
        single_b["diff_path"]
    ).read_text()
    assert batch_results[0]["head_sha"] == single_a["head_sha"]
    assert batch_results[1]["head_sha"] == single_b["head_sha"]
    assert batch_results[0]["empty"] == single_a["empty"] is False
    assert batch_results[1]["empty"] == single_b["empty"] is False


def test_batch_shares_one_head_sha_across_requests(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    results = freeze_diffs_batch(
        tmp_path,
        [
            {"slice_id": "shared-1", "range": f"{sha1}..{sha2}"},
            {"slice_id": "shared-2", "range": f"{sha1}..{sha2}"},
        ],
    )
    assert results[0]["head_sha"] == results[1]["head_sha"] == sha2


def test_batch_zero_commit_range_refuses_that_request_only(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    results = freeze_diffs_batch(
        tmp_path,
        [
            {"slice_id": "zero-commit", "range": "HEAD..HEAD"},
            {"slice_id": "real-diff", "range": f"{sha1}..{sha2}"},
        ],
    )

    assert results[0]["error"] is not None
    assert "ZERO commits" in results[0]["error"]
    assert results[1]["error"] is None
    assert Path(results[1]["diff_path"]).is_file()


def test_batch_three_dot_range_matches_two_dot_merge_base_equivalent(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    two_dot = freeze_diffs_batch(
        tmp_path, [{"slice_id": "three-dot-check", "range": f"{sha1}...{sha2}"}]
    )[0]

    assert two_dot["error"] is None
    assert Path(two_dot["diff_path"]).read_text() != ""


def test_batch_empty_returns_empty_list(tmp_path: Path) -> None:
    assert freeze_diffs_batch(tmp_path, []) == []


def test_batch_mismatched_paths_raises(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    with pytest.raises(ValueError):
        freeze_diffs_batch(
            tmp_path,
            [
                {"slice_id": "p1", "range": f"{sha1}..{sha2}", "paths": ["a.txt"]},
                {"slice_id": "p2", "range": f"{sha1}..{sha2}", "paths": ["b.txt"]},
            ],
        )


def test_frozen_diff_is_byte_identical_to_git_diff_of_the_range(tmp_path: Path) -> None:
    """Direction pin: the freeze must equal `git diff A B`, not its inverse.
    `diff-tree --stdin` reads `<commit> <parent>`, so feeding the pair in
    range order printed every addition as a deletion -- and the batch/single
    parity tests above could not see it, because both paths share it."""
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    _commit(tmp_path, "b.txt", "new file\n", "add b.txt")
    sha3 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    results = freeze_diffs_batch(
        tmp_path,
        [
            {"slice_id": "dir-1", "range": f"{sha1}..{sha3}", "paths": None},
            {"slice_id": "dir-2", "range": f"{sha1}...{sha3}", "paths": None},
        ],
    )

    expected = _git(["diff", sha1, sha3], cwd=tmp_path).stdout
    assert "+line two" in expected and "+new file" in expected
    for result in results:
        assert result["error"] is None
        assert Path(result["diff_path"]).read_text() == expected

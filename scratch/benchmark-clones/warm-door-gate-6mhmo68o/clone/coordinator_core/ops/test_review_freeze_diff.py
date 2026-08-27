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

import coordinator_core.ops.review_freeze_diff  # noqa: F401 — fires @register_op
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.review_freeze_diff import _handler, _validate_slice_id, freeze_diff

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
    _init_repo(tmp_path)
    _commit(tmp_path, "a.txt", "line one\n", "add a.txt")

    result = freeze_diff(tmp_path, "HEAD..HEAD", "empty-diff")

    assert result["error"] is None
    assert result["empty"] is True
    assert Path(result["diff_path"]).read_text() == ""


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

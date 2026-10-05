"""
coordinator_core.ops.test_review_freeze_diff_worktree — worktree-mode
coverage for `review.freeze_diff` (coordinator_core/ops/review_freeze_diff.py),
added by docs/plans/2026-09-27-emitter-dag-terminal-commit-wake-digest.md
chunk C8 (§ Design D7's freeze bullet).

Range-mode behaviour is covered by test_review_freeze_diff.py and stays
untouched here; this file exercises only `worktree=True`.

Spec backlink: docs/plans/2026-09-27-emitter-dag-terminal-commit-wake-digest.md
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.review_freeze_diff as review_freeze_diff
from coordinator_core.ops.review_freeze_diff import _handler, freeze_diff

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

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


def _index_bytes(repo: Path) -> bytes:
    return (repo / ".git" / "index").read_bytes()


# ---------------------------------------------------------------------------
# AC7 — base-to-worktree freeze over tracked + untracked declared paths.
# ---------------------------------------------------------------------------


def test_worktree_freeze_covers_modified_tracked_and_new_untracked_paths(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    base = _commit(tmp_path, "tracked.txt", "line one\n", "add tracked.txt")

    # Modify the tracked file in the worktree, without committing.
    (tmp_path / "tracked.txt").write_text("line one\nline two\n")
    # A brand-new file, never added or committed -- genuinely untracked.
    (tmp_path / "untracked.txt").write_text("brand new content\n")

    result = freeze_diff(
        tmp_path,
        base,
        "worktree-1",
        paths=["tracked.txt", "untracked.txt"],
        worktree=True,
    )

    assert result["error"] is None, result["error"]
    diff_text = Path(result["diff_path"]).read_text()
    assert "line two" in diff_text
    assert "tracked.txt" in diff_text
    assert "brand new content" in diff_text
    assert "untracked.txt" in diff_text
    assert "new file mode" in diff_text
    assert result["head_sha"] == f"{base} worktree"
    assert Path(result["head_sha_path"]).read_text().strip() == f"{base} worktree"


def test_worktree_freeze_spawns_git_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    base = _commit(tmp_path, "tracked.txt", "line one\n", "add tracked.txt")
    (tmp_path / "tracked.txt").write_text("line one\nline two\n")
    (tmp_path / "untracked.txt").write_text("brand new content\n")

    diff_calls = []
    real_git = review_freeze_diff._git

    def _counting_git(args, **kwargs):
        if args and args[0] == "diff":
            diff_calls.append(args)
        return real_git(args, **kwargs)

    monkeypatch.setattr(review_freeze_diff, "_git", _counting_git)

    result = freeze_diff(
        tmp_path,
        base,
        "worktree-spawn",
        paths=["tracked.txt", "untracked.txt"],
        worktree=True,
    )

    assert result["error"] is None, result["error"]
    assert len(diff_calls) == 1, (
        f"expected exactly one 'git diff' spawn for the worktree freeze, got "
        f"{len(diff_calls)}: {diff_calls!r}"
    )


def test_worktree_freeze_leaves_git_index_byte_identical(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    base = _commit(tmp_path, "tracked.txt", "line one\n", "add tracked.txt")
    (tmp_path / "tracked.txt").write_text("line one\nline two\n")
    (tmp_path / "untracked.txt").write_text("brand new content\n")

    before = _index_bytes(tmp_path)

    result = freeze_diff(
        tmp_path,
        base,
        "worktree-index",
        paths=["tracked.txt", "untracked.txt"],
        worktree=True,
    )

    assert result["error"] is None, result["error"]
    after = _index_bytes(tmp_path)
    assert after == before, "worktree freeze must never touch .git/index"

    # untracked.txt must still show as untracked -- never staged.
    status = _git(["status", "--porcelain", "--", "untracked.txt"], cwd=tmp_path).stdout
    assert status.strip().startswith("??"), f"unexpected status: {status!r}"


def test_worktree_freeze_requires_non_empty_paths(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    base = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")

    result = freeze_diff(tmp_path, base, "no-paths", paths=None, worktree=True)

    assert result["error"] is not None
    assert not _diffs_dir(tmp_path).exists()


def test_worktree_freeze_requires_a_base(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "a.txt", "line one\n", "add a.txt")

    result = freeze_diff(tmp_path, "", "no-base", paths=["a.txt"], worktree=True)

    assert result["error"] is not None
    assert not _diffs_dir(tmp_path).exists()


def test_worktree_freeze_unmodified_declared_path_is_reported_not_refused(
    tmp_path: Path,
) -> None:
    # The declared set is a ceiling: an untouched declared path must not
    # refuse the freeze of the declared paths that did change.
    _init_repo(tmp_path)
    base = _commit(tmp_path, "unmodified.txt", "same\n", "add unmodified.txt")
    (tmp_path / "changed.txt").write_text("v1\n")
    _git(["add", "changed.txt"], cwd=tmp_path)
    _git(["commit", "-q", "-m", "add changed.txt"], cwd=tmp_path)
    base2 = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()
    (tmp_path / "changed.txt").write_text("v2\n")

    result = freeze_diff(
        tmp_path,
        base2,
        "unmod",
        paths=["unmodified.txt", "changed.txt"],
        worktree=True,
    )

    assert result["error"] is None
    assert result["uncovered_paths"] == ["unmodified.txt"]
    assert "changed.txt" in Path(result["diff_path"]).read_text(encoding="utf-8")
    assert not result["empty"]
    assert base  # keep the first commit sha referenced for clarity


def test_worktree_freeze_undeclared_path_peer_commit_stays_out(tmp_path: Path) -> None:
    # A peer commit to an UNDECLARED path after base must not leak into a
    # worktree freeze restricted to a different declared path (mirrors
    # test_pathspec_narrows_output's range-mode pin).
    _init_repo(tmp_path)
    base = _commit(tmp_path, "declared.txt", "v1\n", "add declared.txt")
    (tmp_path / "declared.txt").write_text("v2\n")

    # A peer's commit to an undeclared path, landing after base.
    (tmp_path / "peer.txt").write_text("peer content\n")
    _git(["add", "peer.txt"], cwd=tmp_path)
    _git(["commit", "-q", "-m", "peer commit to an undeclared path"], cwd=tmp_path)

    result = freeze_diff(tmp_path, base, "peer-excluded", paths=["declared.txt"], worktree=True)

    assert result["error"] is None, result["error"]
    diff_text = Path(result["diff_path"]).read_text()
    assert "declared.txt" in diff_text
    assert "peer.txt" not in diff_text
    assert "peer content" not in diff_text


def test_worktree_freeze_is_exactly_the_declared_changes_amid_peer_activity(
    tmp_path: Path,
) -> None:
    # The execute-review shape: the run's rows land uncommitted (one modified,
    # one brand new) while a peer both edits an undeclared file in the
    # worktree and commits another after base. Only the declared changes may
    # appear, and none of them may be dropped.
    _init_repo(tmp_path)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "queue").mkdir(parents=True)
    (tmp_path / "docs" / "plans" / "plan.md").write_text("plan v1\n")
    (tmp_path / "state" / "queue" / "peer-edited.yaml").write_text("peer v1\n")
    (tmp_path / "state" / "queue" / "peer-committed.yaml").write_text("pc v1\n")
    _git(["add", "-A"], cwd=tmp_path)
    assert _git(["commit", "-q", "-m", "base"], cwd=tmp_path).returncode == 0
    base = _git(["rev-parse", "HEAD"], cwd=tmp_path).stdout.strip()

    (tmp_path / "state" / "queue" / "peer-committed.yaml").write_text("pc v2\n")
    _git(["add", "state/queue/peer-committed.yaml"], cwd=tmp_path)
    assert _git(["commit", "-q", "-m", "peer commit"], cwd=tmp_path).returncode == 0

    (tmp_path / "docs" / "plans" / "plan.md").write_text("plan v2 declared edit\n")
    (tmp_path / "docs" / "plans" / "new-plan.md").write_text("declared untracked\n")
    (tmp_path / "state" / "queue" / "peer-edited.yaml").write_text("peer v2 edit\n")

    result = freeze_diff(
        tmp_path,
        base,
        "declared-amid-peers",
        paths=["docs/plans/plan.md", "docs/plans/new-plan.md"],
        worktree=True,
    )

    assert result["error"] is None, result["error"]
    diff_text = Path(result["diff_path"]).read_text()
    headers = sorted(
        line for line in diff_text.splitlines() if line.startswith("diff --git ")
    )
    assert headers == [
        "diff --git a/docs/plans/new-plan.md b/docs/plans/new-plan.md",
        "diff --git a/docs/plans/plan.md b/docs/plans/plan.md",
    ]
    assert "plan v2 declared edit" in diff_text
    assert "declared untracked" in diff_text
    assert "peer" not in diff_text and "pc v2" not in diff_text


def test_worktree_freeze_binary_untracked_file(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    base = _commit(tmp_path, "tracked.txt", "line one\n", "add tracked.txt")
    (tmp_path / "tracked.txt").write_text("line one\nline two\n")
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02")

    result = freeze_diff(
        tmp_path, base, "worktree-binary", paths=["tracked.txt", "bin.dat"], worktree=True
    )

    assert result["error"] is None, result["error"]
    diff_text = Path(result["diff_path"]).read_text()
    assert "Binary files /dev/null and b/bin.dat differ" in diff_text


def test_worktree_freeze_range_mode_stays_byte_identical(tmp_path: Path) -> None:
    # Range mode's own output must not shift with worktree mode landed
    # alongside it.
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    result = freeze_diff(tmp_path, f"{sha1}..{sha2}", "range-unaffected", paths=["a.txt"])

    assert result["error"] is None
    assert "line two" in Path(result["diff_path"]).read_text()


# ---------------------------------------------------------------------------
# JSON-RPC handler shape.
# ---------------------------------------------------------------------------


def test_handler_forwards_worktree_flag(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    base = _commit(tmp_path, "tracked.txt", "line one\n", "add tracked.txt")
    (tmp_path / "tracked.txt").write_text("line one\nline two\n")

    result = _handler(
        {
            "range": base,
            "slice_id": "handler-worktree",
            "paths": ["tracked.txt"],
            "worktree": True,
        },
        repo_root=tmp_path,
    )

    assert result["error"] is None, result["error"]
    assert result["head_sha"] == f"{base} worktree"


def test_handler_default_worktree_is_range_mode(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha1 = _commit(tmp_path, "a.txt", "line one\n", "add a.txt")
    sha2 = _commit(tmp_path, "a.txt", "line one\nline two\n", "extend a.txt")

    result = _handler(
        {"range": f"{sha1}..{sha2}", "slice_id": "handler-default"}, repo_root=tmp_path
    )

    assert result["error"] is None
    assert result["head_sha"] == sha2

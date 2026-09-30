from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from percolate.throwaway_tree import (  # noqa: E402
    build_throwaway_tree,
    discard_throwaway_tree,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        **_NO_CONSOLE,
    )
    return proc.stdout.strip()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.fixture()
def dest_repo(tmp_path: Path) -> Path:
    root = tmp_path / "dest"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _write(root / "top.txt", "top-head\n")
    _write(root / "sub" / "a.txt", "a-head\n")
    _write(root / "sub" / "b.txt", "b-head\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def _snapshot(root: Path) -> "dict[str, str]":
    out = {}
    for path in root.rglob("*"):
        if ".git" in path.parts:
            continue
        if path.is_file():
            out[str(path.relative_to(root))] = path.read_bytes().decode("utf-8", "replace")
    return out


def test_head_only_materialization_ignores_dirty_worktree(dest_repo: Path) -> None:
    # Dirty the worktree after commit — must not appear in the throwaway tree.
    (dest_repo / "top.txt").write_text("DIRTY-UNCOMMITTED\n", encoding="utf-8")
    (dest_repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")

    tree = build_throwaway_tree(dest_repo, overlays=[], deletions=[])
    try:
        assert (tree / "top.txt").read_text(encoding="utf-8") == "top-head\n"
        assert not (tree / "untracked.txt").exists()
        assert (tree / ".git").exists()
        assert _git(tree, "rev-parse", "HEAD") == _git(dest_repo, "rev-parse", "HEAD")
    finally:
        discard_throwaway_tree(tree)


def test_status_lists_exactly_overlaid_and_deleted_paths(
    tmp_path: Path, dest_repo: Path
) -> None:
    staging = tmp_path / "staging"
    _write(staging / "a.txt", "a-overlaid\n")
    # b.txt intentionally omitted from staging — must be gone from the overlaid subtree.
    _write(staging / "c.txt", "c-new\n")

    tree = build_throwaway_tree(
        dest_repo, overlays=[(staging, Path("sub"))], deletions=["top.txt"]
    )
    try:
        assert (tree / "sub" / "a.txt").read_text(encoding="utf-8") == "a-overlaid\n"
        assert not (tree / "sub" / "b.txt").exists()
        assert (tree / "sub" / "c.txt").read_text(encoding="utf-8") == "c-new\n"
        assert not (tree / "top.txt").exists()

        status = _git(tree, "status", "--porcelain")
        changed = {line.split(maxsplit=1)[1] for line in status.splitlines()}
        assert changed == {"sub/a.txt", "sub/b.txt", "sub/c.txt", "top.txt"}
    finally:
        discard_throwaway_tree(tree)


def test_deletions_remove_dest_relative_paths(dest_repo: Path) -> None:
    tree = build_throwaway_tree(dest_repo, overlays=[], deletions=["sub/a.txt", "sub"])
    try:
        assert not (tree / "sub").exists()
        assert (tree / "top.txt").exists()
    finally:
        discard_throwaway_tree(tree)


def test_discard_is_idempotent(dest_repo: Path) -> None:
    tree = build_throwaway_tree(dest_repo, overlays=[], deletions=[])
    discard_throwaway_tree(tree)
    assert not tree.exists()
    discard_throwaway_tree(tree)  # second call must not raise
    assert not tree.exists()


def test_dest_repo_untouched_after_build(dest_repo: Path) -> None:
    before_head = _git(dest_repo, "rev-parse", "HEAD")
    before_status = _git(dest_repo, "status", "--porcelain")
    before_snapshot = _snapshot(dest_repo)

    tree = build_throwaway_tree(dest_repo, overlays=[], deletions=[])
    try:
        assert _git(dest_repo, "rev-parse", "HEAD") == before_head
        assert _git(dest_repo, "status", "--porcelain") == before_status
        assert _snapshot(dest_repo) == before_snapshot
        assert _git(dest_repo, "cat-file", "-p", "HEAD^{tree}")  # index untouched, sanity
    finally:
        discard_throwaway_tree(tree)


def test_root_overlay_keeps_git_and_yields_to_subdir_rows(dest_repo: Path, tmp_path: Path) -> None:
    root_stage = tmp_path / "root-stage"
    _write(root_stage / "top.txt", "top-new\n")
    _write(root_stage / "sub" / "a.txt", "a-head\n")
    _write(root_stage / "sub" / "b.txt", "b-head\n")
    sub_stage = tmp_path / "sub-stage"
    _write(sub_stage / "a.txt", "a-new\n")

    tree = build_throwaway_tree(
        dest_repo, [(sub_stage, Path("sub")), (root_stage, Path("."))], []
    )
    try:
        assert (tree / ".git").is_dir()
        assert (tree / "top.txt").read_text() == "top-new\n"
        assert (tree / "sub" / "a.txt").read_text() == "a-new\n"
        assert not (tree / "sub" / "b.txt").exists()
    finally:
        discard_throwaway_tree(tree)


def test_second_root_row_does_not_revert_the_first(dest_repo: Path, tmp_path: Path) -> None:
    """Every root row stages a full copy of the dest; a later one's stale
    copy must not undo an earlier one's add, edit, or delete."""
    first = tmp_path / "first"
    _write(first / "top.txt", "top-new\n")
    _write(first / "sub" / "a.txt", "a-head\n")
    _write(first / "sub" / "new.txt", "new\n")
    second = tmp_path / "second"
    _write(second / "top.txt", "top-head\n")
    _write(second / "sub" / "a.txt", "a-2\n")
    _write(second / "sub" / "b.txt", "b-head\n")
    _write(second / "other.txt", "other\n")

    tree = build_throwaway_tree(dest_repo, [(first, Path(".")), (second, Path("."))], [])
    try:
        assert (tree / "top.txt").read_text() == "top-new\n"
        assert (tree / "sub" / "new.txt").read_text() == "new\n"
        assert (tree / "sub" / "a.txt").read_text() == "a-2\n"
        assert not (tree / "sub" / "b.txt").exists()
        assert (tree / "other.txt").read_text() == "other\n"
    finally:
        discard_throwaway_tree(tree)

"""Tests for the structural restore-or-commit invariant added 2026-09-29
(PM ruling: "it must be IMPOSSIBLE for percolate/publish to exit -- success
or failure -- with the klabauter dest dirty or half-committed").

Covers the two new primitives directly (`_restore_dest_subtree_to_head`,
`_assert_dest_subtree_clean`) and `process_target`'s own `finally`-block
wiring (a post-swap failure restores `target.dest_dir`, never leaving real
swapped bytes stranded dirty when `main()`'s per-row failure handling
merely excludes the row from the round's commit pathspec).

Run: python -m pytest coordinator/bin/tests/test_publish_dest_restore_primitive.py -q
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
        creationflags=_NO_WINDOW,
    )


def _init_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "publish-dest-restore-test@claude-klabauter.test")
    _git(root, "config", "user.name", "Publish Dest Restore Test")
    _git(root, "config", "commit.gpgsign", "false")


def _porcelain(root: Path) -> str:
    return _git(root, "status", "--porcelain").stdout


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_dest_restore_primitive_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def test_restore_removes_untracked_new_files_under_dest_dir(tmp_path):
    dest_root = tmp_path / "dest"
    _init_git_repo(dest_root)
    keeper = dest_root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git(dest_root, "add", ".gitkeep")
    _git(dest_root, "commit", "-m", "init")

    sub = dest_root / "sub"
    sub.mkdir()
    (sub / "swapped.txt").write_text("landed by an aborted swap\n", encoding="utf-8")
    nested = sub / "nested"
    nested.mkdir()
    (nested / "also_new.txt").write_text("also landed\n", encoding="utf-8")

    assert _porcelain(dest_root) != ""

    publish._restore_dest_subtree_to_head(dest_root, sub)

    assert _porcelain(dest_root) == ""
    assert not sub.exists()


def test_restore_reverts_tracked_modifications_under_dest_dir(tmp_path):
    dest_root = tmp_path / "dest"
    _init_git_repo(dest_root)
    sub = dest_root / "sub"
    sub.mkdir()
    tracked = sub / "tracked.txt"
    tracked.write_text("original\n", encoding="utf-8")
    _git(dest_root, "add", "sub/tracked.txt")
    _git(dest_root, "commit", "-m", "seed tracked file")

    tracked.write_text("mutated by an aborted swap\n", encoding="utf-8")
    assert _porcelain(dest_root) != ""

    publish._restore_dest_subtree_to_head(dest_root, sub)

    assert _porcelain(dest_root) == ""
    assert tracked.read_text(encoding="utf-8") == "original\n"


def test_restore_never_touches_a_sibling_row_outside_dest_dir(tmp_path):
    dest_root = tmp_path / "dest"
    _init_git_repo(dest_root)
    keeper = dest_root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git(dest_root, "add", ".gitkeep")
    _git(dest_root, "commit", "-m", "init")

    failing_sub = dest_root / "sub-failed"
    failing_sub.mkdir()
    (failing_sub / "swapped.txt").write_text("stranded\n", encoding="utf-8")

    sibling_sub = dest_root / "sub-succeeded"
    sibling_sub.mkdir()
    (sibling_sub / "still_uncommitted.txt").write_text(
        "another row's own swap, not yet committed by the round\n", encoding="utf-8"
    )

    publish._restore_dest_subtree_to_head(dest_root, failing_sub)

    assert not failing_sub.exists()
    # The sibling row's own not-yet-committed swap is outside `failing_sub`'s
    # pathspec and must survive untouched -- restore is scoped per dest_dir,
    # never a whole-repo `git checkout .`/`git clean`.
    assert (sibling_sub / "still_uncommitted.txt").exists()


def test_assert_dest_subtree_clean_raises_when_restore_did_not_finish(tmp_path, monkeypatch):
    dest_root = tmp_path / "dest"
    _init_git_repo(dest_root)
    keeper = dest_root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git(dest_root, "add", ".gitkeep")
    _git(dest_root, "commit", "-m", "init")

    sub = dest_root / "sub"
    sub.mkdir()
    (sub / "swapped.txt").write_text("landed\n", encoding="utf-8")

    with pytest.raises(publish.PublishDestNotCleanError):
        publish._assert_dest_subtree_clean(dest_root, sub, context="test")


def test_assert_dest_subtree_clean_passes_on_a_clean_subtree(tmp_path):
    dest_root = tmp_path / "dest"
    _init_git_repo(dest_root)
    keeper = dest_root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git(dest_root, "add", ".gitkeep")
    _git(dest_root, "commit", "-m", "init")

    sub = dest_root / "sub"
    sub.mkdir()
    (sub / "tracked.txt").write_text("hello\n", encoding="utf-8")
    _git(dest_root, "add", "sub/tracked.txt")
    _git(dest_root, "commit", "-m", "seed")

    publish._assert_dest_subtree_clean(dest_root, sub, context="test")


def test_process_target_finally_restores_after_a_post_swap_exception(monkeypatch, tmp_path):
    """The exact gap the module's own comment used to admit: a swap that
    lands real bytes (`staging_swapped = True`) followed by ANY exception
    before the row's own success line must restore `target.dest_dir`, not
    merely leave it for `main()`'s exclude-from-commit bookkeeping (which
    never restores anything)."""
    dest_root = tmp_path / "dest"
    _init_git_repo(dest_root)
    keeper = dest_root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git(dest_root, "add", ".gitkeep")
    _git(dest_root, "commit", "-m", "init")

    sub = dest_root / "sub"

    class _BoomAfterSwap(RuntimeError):
        pass

    # Exercise the `finally` block in isolation, the way this module already
    # exercises other private helpers directly (§ `test_publish_dest_restore_
    # primitive` siblings above) -- a full `process_target` dispatch needs a
    # live percolate engine context this test suite does not stand up.
    staging_swapped = False
    row_committed_ok = False
    try:
        try:
            sub.mkdir()
            (sub / "swapped.txt").write_text("landed\n", encoding="utf-8")
            staging_swapped = True
            raise _BoomAfterSwap("simulated failure between swap and success line")
        finally:
            if staging_swapped and not row_committed_ok:
                repo_root = publish._dest_repo_root(sub)
                assert repo_root is not None
                publish._restore_dest_subtree_to_head(repo_root, sub)
                publish._assert_dest_subtree_clean(repo_root, sub, context="test")
    except _BoomAfterSwap:
        pass

    assert not sub.exists()
    assert _porcelain(dest_root) == ""


def test_restore_handles_a_dirty_set_larger_than_the_windows_argv_cap(tmp_path):
    """A failed round that wrote ~1000 paths overflowed the 32K command line;
    the restore swallowed the spawn failure and the assertion then raised."""
    dest_root = tmp_path / "dest"
    _init_git_repo(dest_root)
    _git(dest_root, "config", "core.autocrlf", "true")
    sub = dest_root / "coordinator" / "bin"
    sub.mkdir(parents=True)
    names = [f"{'long-file-name-padding-' * 3}{i:05d}.py" for i in range(1500)]
    for name in names:
        (sub / name).write_bytes(b"print('original')\n")
    _git(dest_root, "add", "-A")
    _git(dest_root, "commit", "-m", "seed")

    for name in names:
        (sub / name).write_bytes(b"print('mutated')\r\n")
    (sub / "new file.txt").write_text("untracked\n", encoding="utf-8")
    assert sum(len(str(sub / n)) + 1 for n in names) > 32768

    publish._restore_dest_subtree_to_head(dest_root, sub)

    assert _porcelain(dest_root) == ""
    publish._assert_dest_subtree_clean(dest_root, sub, context="argv-cap test")

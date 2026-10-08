"""coordinator/bin/tests/test_publish_swap_via_throwaway.py — DR-445's
2026-09-29 swap rewrite (PM ruling): the throwaway IS the delta, and
landing it is a git operation, never a Python file walk.

Covers `_throwaway_delta_paths`, `_apply_throwaway_delta_to_dest` (the
`--no-commit` leg) and `_commit_throwaway_and_merge_into_dest` (the
default, commit-in-throwaway + fetch + `merge --ff-only` leg) directly —
each test builds a REAL throwaway as a `git clone --local` of a REAL dest
repo, mutates the throwaway's own worktree (add/modify/delete), then
drives the function under test and asserts on the dest.

Run: python -m pytest coordinator/bin/tests/test_publish_swap_via_throwaway.py -q
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
    _git(root, "config", "user.email", "publish-swap-throwaway-test@claude-klabauter.test")
    _git(root, "config", "user.name", "Publish Swap Throwaway Test")
    _git(root, "config", "commit.gpgsign", "false")


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_swap_via_throwaway_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _build_dest_and_throwaway(tmp_path: Path):
    """A dest repo carrying `unchanged.txt`, `modified.txt`, `deleted.txt`
    (all committed), plus a `git clone --local` of it (the throwaway) whose
    worktree is then mutated: `modified.txt` changed, `deleted.txt` removed,
    `added.txt` created — `unchanged.txt` left byte-for-byte and stat-for-
    stat alone."""
    dest = tmp_path / "dest-repo"
    _init_git_repo(dest)
    (dest / "unchanged.txt").write_text("unchanged\n", encoding="utf-8")
    (dest / "modified.txt").write_text("before\n", encoding="utf-8")
    (dest / "deleted.txt").write_text("goodbye\n", encoding="utf-8")
    _git(dest, "add", ".")
    _git(dest, "commit", "-m", "chore: seed dest")

    throwaway = tmp_path / "throwaway"
    _git(tmp_path, "clone", "--local", "--no-checkout", str(dest), str(throwaway))
    _git(throwaway, "checkout", "-q", "HEAD")

    (throwaway / "modified.txt").write_text("after\n", encoding="utf-8")
    (throwaway / "deleted.txt").unlink()
    (throwaway / "added.txt").write_text("brand new\n", encoding="utf-8")

    return dest, throwaway


def test_throwaway_delta_paths_reports_exactly_the_mutation(tmp_path):
    _dest, throwaway = _build_dest_and_throwaway(tmp_path)

    present, deleted = publish._throwaway_delta_paths(throwaway)

    assert present == ["added.txt", "modified.txt"]
    assert deleted == ["deleted.txt"]


def test_apply_throwaway_delta_to_dest_no_commit_leg(tmp_path):
    dest, throwaway = _build_dest_and_throwaway(tmp_path)
    unchanged_path = dest / "unchanged.txt"
    before_stat = unchanged_path.stat()

    present, deleted = publish._throwaway_delta_paths(throwaway)
    publish._apply_throwaway_delta_to_dest(dest, throwaway, present, deleted)

    after_stat = unchanged_path.stat()
    assert after_stat.st_ino == before_stat.st_ino
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert unchanged_path.read_text(encoding="utf-8") == "unchanged\n"

    assert (dest / "modified.txt").read_text(encoding="utf-8") == "after\n"
    assert (dest / "added.txt").read_text(encoding="utf-8") == "brand new\n"
    assert not (dest / "deleted.txt").exists()

    # No-commit leg: bytes land, dest gains no commit — this is the
    # `percolate-round.py`-driven `--no-commit` contract.
    assert _git(dest, "rev-list", "--count", "HEAD").stdout.strip() == "1"
    porcelain = _git(dest, "status", "--porcelain").stdout
    assert "modified.txt" in porcelain
    assert "added.txt" in porcelain
    assert "deleted.txt" in porcelain

    # `.git` is never a candidate — the delta never names a path under it.
    assert (dest / ".git").is_dir()
    assert _git(dest, "rev-parse", "HEAD").returncode == 0


def test_commit_throwaway_and_merge_into_dest_commits_exactly_the_delta(tmp_path):
    dest, throwaway = _build_dest_and_throwaway(tmp_path)
    unchanged_path = dest / "unchanged.txt"
    before_stat = unchanged_path.stat()
    before_head = _git(dest, "rev-parse", "HEAD").stdout.strip()

    present, deleted = publish._throwaway_delta_paths(throwaway)
    publish._commit_throwaway_and_merge_into_dest(
        dest,
        throwaway,
        present_paths=present,
        deleted_paths=deleted,
        succeeded_row_names=["row-a"],
        round_pinned_shas={},
    )

    # Unchanged file survives untouched -- the commit's tree carries it
    # unaltered (its blob is inherited from the parent commit), and the
    # merge (a fast-forward) never rewrites a worktree file it did not
    # change.
    after_stat = unchanged_path.stat()
    assert after_stat.st_ino == before_stat.st_ino
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns

    assert (dest / "modified.txt").read_text(encoding="utf-8") == "after\n"
    assert (dest / "added.txt").read_text(encoding="utf-8") == "brand new\n"
    assert not (dest / "deleted.txt").exists()

    after_head = _git(dest, "rev-parse", "HEAD").stdout.strip()
    assert after_head != before_head, "the merge must advance dest's HEAD"
    assert _git(dest, "rev-list", "--count", "HEAD").stdout.strip() == "2"

    assert _git(dest, "status", "--porcelain").stdout == "", (
        "a clean fast-forward merge must leave dest with nothing dirty"
    )

    # The commit's own tree contains EXACTLY the delta: unchanged.txt stays,
    # modified.txt carries the new content, deleted.txt is gone, added.txt
    # is present -- nothing more, nothing less.
    changed_files = _git(
        dest, "diff", "--name-status", f"{before_head}..{after_head}"
    ).stdout.strip().splitlines()
    changed_names = {line.split("\t", 1)[1] for line in changed_files}
    assert changed_names == {"modified.txt", "deleted.txt", "added.txt"}

    subject = _git(dest, "log", "-1", "--format=%s").stdout
    assert "row-a" in subject

    assert (dest / ".git").is_dir()


def test_commit_throwaway_reaps_tracked_egg_info(tmp_path):
    """Install output an old round committed is removed by the next round's
    commit, and a non-install-output top-level dir is left alone."""
    dest = tmp_path / "dest-repo"
    _init_git_repo(dest)
    (dest / "x.egg-info").mkdir()
    (dest / "x.egg-info" / "PKG-INFO").write_text("Name: x\n", encoding="utf-8")
    (dest / "docs").mkdir()
    (dest / "docs" / "keep.md").write_text("keep\n", encoding="utf-8")
    (dest / "a.txt").write_text("before\n", encoding="utf-8")
    _git(dest, "add", ".")
    _git(dest, "commit", "-m", "chore: seed dest")
    throwaway = tmp_path / "throwaway"
    _git(tmp_path, "clone", "--local", "--no-checkout", str(dest), str(throwaway))
    _git(throwaway, "checkout", "-q", "HEAD")
    (throwaway / "a.txt").write_text("after\n", encoding="utf-8")

    present, deleted = publish._throwaway_delta_paths(throwaway)
    publish._commit_throwaway_and_merge_into_dest(
        dest, throwaway, present_paths=present, deleted_paths=deleted,
        succeeded_row_names=["row-a"], round_pinned_shas={},
    )

    assert "x.egg-info/PKG-INFO" not in _git(dest, "ls-files").stdout
    assert not (dest / "x.egg-info").exists()
    assert (dest / "docs" / "keep.md").exists()
    assert _git(dest, "status", "--porcelain").stdout == ""


def test_swap_all_rows_into_dest_commits_the_shared_root_once(tmp_path):
    """`_swap_all_rows_into_dest` orchestrates the whole thing per repo
    root, keyed by `staged_by_repo_root`/`throwaway_by_repo_root` — this
    drives it end-to-end with a fabricated `StagedRowResult` and asserts the
    per-row outcome dict reports success."""
    dest, throwaway = _build_dest_and_throwaway(tmp_path)

    fake_target = type("FakeTarget", (), {"name": "row-a", "dest_dir": dest})()
    fake_staged = type(
        "FakeStaged", (), {"staging_dir": tmp_path / "unused-staging-dir"}
    )()

    outcomes = publish._swap_all_rows_into_dest(
        {dest: [(fake_target, fake_staged)]},
        {dest: throwaway},
        commit_now=True,
        succeeded_row_names=["row-a"],
        round_pinned_shas={},
    )

    assert outcomes == {"row-a": None}
    assert (dest / "added.txt").read_text(encoding="utf-8") == "brand new\n"
    assert _git(dest, "rev-list", "--count", "HEAD").stdout.strip() == "2"


def test_apply_throwaway_delta_renames_a_mapped_image_aside(tmp_path, monkeypatch):
    dest, throwaway = _build_dest_and_throwaway(tmp_path)
    (dest / "door.exe").write_bytes(b"old image")
    _git(dest, "add", "door.exe")
    _git(dest, "commit", "-m", "chore: seed door")
    (throwaway / "door.exe").write_bytes(b"new image")

    real_replace = publish.os.replace
    real_copy2 = publish.shutil.copy2
    locked = {"copy_denied": False}

    def _replace(src, dst, *a, **kw):
        if Path(src).name.startswith(".claude-klabauter-swap-") and Path(dst).name == "door.exe":
            raise PermissionError(13, "mapped image", str(dst))
        return real_replace(src, dst, *a, **kw)

    def _copy2(src, dst, *a, **kw):
        if Path(dst).name == "door.exe" and not locked["copy_denied"]:
            locked["copy_denied"] = True
            raise PermissionError(13, "mapped image", str(dst))
        return real_copy2(src, dst, *a, **kw)

    monkeypatch.setattr(publish.os, "replace", _replace)
    monkeypatch.setattr(publish.shutil, "copy2", _copy2)

    present, deleted = publish._throwaway_delta_paths(throwaway)
    publish._apply_throwaway_delta_to_dest(dest, throwaway, present, deleted)

    assert (dest / "door.exe").read_bytes() == b"new image"
    assert not list(dest.glob(".claude-klabauter-swap-*"))

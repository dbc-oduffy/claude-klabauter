"""Staging directories `publish.py` mints are discarded on success and on failure.

Run: python -m pytest coordinator/bin/tests/test_publish_staging_residue_sweep.py -q
"""

from __future__ import annotations

import importlib.util
import io
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location("publish_staging_residue_sweep_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


@pytest.fixture(autouse=True)
def _clean_minted():
    publish._MINTED_STAGING_DIRS.clear()
    yield
    publish._MINTED_STAGING_DIRS.clear()


def _dest(tmp_path: Path) -> Path:
    dest = tmp_path / "mirror" / "coordinator_core"
    dest.mkdir(parents=True)
    return dest


def _git(cwd: Path, *args: str) -> None:
    # popup-safe-env-suppressed
    subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _toplevel_reference_row(tmp_path: Path, *, allowlist: str):
    """A mirror repo whose `docs/reference` row publishes `a.md` while its source also holds
    `op-catalog.md`; the destination carries `op-catalog.md` untracked."""
    source = tmp_path / "src" / "docs" / "reference"
    source.mkdir(parents=True)
    for name in ("a.md", "op-catalog.md"):
        (source / name).write_text(name, encoding="utf-8")
    repo = tmp_path / "mirror"
    dest = repo / "docs" / "reference"
    dest.mkdir(parents=True)
    _git(repo, "init", "-q")
    (dest / "a.md").write_text("a.md", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    (dest / "op-catalog.md").write_text("generated", encoding="utf-8")
    return publish.ResolvedTarget(
        name="toplevel-reference", mode="flat-mirror", source_dir=source, dest_dir=dest, allowlist=allowlist
    )


def test_withheld_untracked_file_is_found_and_swept(tmp_path):
    target = _toplevel_reference_row(tmp_path, allowlist="a.md")
    leftover = target.dest_dir / "op-catalog.md"

    assert publish._withheld_untracked_top_level_files(target) == [leftover]
    assert publish._sweep_withheld_untracked_top_level_files(target) == [leftover]

    assert not leftover.exists()
    assert (target.dest_dir / "a.md").is_file()


def test_a_published_untracked_or_tracked_file_is_never_swept(tmp_path):
    published = _toplevel_reference_row(tmp_path / "p", allowlist="a.md,op-catalog.md")
    assert publish._withheld_untracked_top_level_files(published) == []

    tracked = _toplevel_reference_row(tmp_path / "t", allowlist="a.md")
    _git(tracked.dest_dir.parent.parent, "add", "-f", "docs/reference/op-catalog.md")
    assert publish._withheld_untracked_top_level_files(tracked) == []

    foreign = _toplevel_reference_row(tmp_path / "f", allowlist="a.md")
    (foreign.dest_dir / "theirs.md").write_text("another publisher's", encoding="utf-8")
    assert [p.name for p in publish._withheld_untracked_top_level_files(foreign)] == ["op-catalog.md"]


def test_no_allowlist_inclusion_sweeps_nothing(tmp_path):
    target = _toplevel_reference_row(tmp_path, allowlist="")

    assert publish._withheld_untracked_top_level_files(target) == []


def _dead_pid() -> int:
    # popup-safe-env-suppressed
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    proc.wait()
    return proc.pid


def _owned_dir(dest: Path, pid: int, *, age_seconds: float = 0.0) -> Path:
    path = dest.parent / f".{dest.name}.publish-staging-p{pid}-abcd1234"
    path.mkdir()
    if age_seconds:
        old = path.stat().st_mtime - age_seconds
        os.utime(path, (old, old))
    return path


def test_dead_owner_young_dir_is_removed(tmp_path):
    dest = _dest(tmp_path)
    dead = _owned_dir(dest, _dead_pid())

    publish._sweep_stale_publish_staging_dirs(dest, publish.RunTotals())

    assert not dead.exists()


def test_dry_run_removes_no_dead_owner_dir(tmp_path):
    dest = _dest(tmp_path)
    dead = _owned_dir(dest, _dead_pid())

    publish._sweep_stale_publish_staging_dirs(dest, publish.RunTotals(), dry_run=True, out=io.StringIO())

    assert dead.is_dir()


def test_a_dir_this_process_minted_is_never_swept(tmp_path):
    dest = _dest(tmp_path)
    dead = _owned_dir(dest, _dead_pid())
    publish._MINTED_STAGING_DIRS.add(dead.resolve())

    publish._sweep_stale_publish_staging_dirs(dest, publish.RunTotals())

    assert dead.is_dir()

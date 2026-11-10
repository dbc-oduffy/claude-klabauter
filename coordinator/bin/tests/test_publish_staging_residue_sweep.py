"""Staging directories `publish.py` mints are discarded on success and on failure.

Run: python -m pytest coordinator/bin/tests/test_publish_staging_residue_sweep.py -q
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

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


def _staging_dirs(dest: Path) -> "list[Path]":
    return sorted(dest.parent.glob(f".{dest.name}.publish-staging-*"))


def _fill_readonly(staging: Path) -> None:
    nested = staging / "sub"
    nested.mkdir()
    victim = nested / "_engine_stamp"
    victim.write_text("stamp\n", encoding="utf-8")
    os.chmod(victim, stat.S_IREAD)


def test_undiscarded_minted_dirs_are_removed_including_readonly_files(tmp_path):
    dest = _dest(tmp_path)
    first = publish._create_publish_staging_dir(dest)
    second = publish._create_publish_staging_dir(dest)
    _fill_readonly(first)
    bystander = dest.parent / ".other.publish-staging-keepme"
    bystander.mkdir()

    publish._remove_minted_staging_dirs()

    assert not first.exists() and not second.exists()
    assert _staging_dirs(dest) == []
    assert bystander.is_dir(), "a directory this process did not mint must survive"
    assert publish._MINTED_STAGING_DIRS == set()


def test_discard_retries_a_partial_removal(tmp_path, monkeypatch):
    dest = _dest(tmp_path)
    staging = publish._create_publish_staging_dir(dest)
    (staging / "f").write_text("x", encoding="utf-8")
    real_rmtree = shutil.rmtree
    calls = []

    def flaky_rmtree(path, *args, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            return None
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(publish.shutil, "rmtree", flaky_rmtree)
    publish._discard_publish_staging_dir(staging)

    assert len(calls) == 2
    assert not staging.exists()


def test_main_leaves_no_staging_after_a_successful_run(tmp_path, monkeypatch):
    dest = _dest(tmp_path)
    monkeypatch.setattr(publish, "lower_process_priority_self", lambda: None)

    def fake_main(_argv):
        publish._create_publish_staging_dir(dest)
        return 0

    monkeypatch.setattr(publish, "_main", fake_main)

    assert publish.main([]) == 0
    assert _staging_dirs(dest) == []


def test_main_leaves_no_staging_after_a_failed_run(tmp_path, monkeypatch):
    dest = _dest(tmp_path)
    monkeypatch.setattr(publish, "lower_process_priority_self", lambda: None)

    def fake_main(_argv):
        _fill_readonly(publish._create_publish_staging_dir(dest))
        raise RuntimeError("row aborted after minting")

    monkeypatch.setattr(publish, "_main", fake_main)

    with pytest.raises(RuntimeError):
        publish.main([])
    assert _staging_dirs(dest) == []


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


def test_stale_sweep_still_spares_a_fresh_unminted_dir(tmp_path):
    dest = _dest(tmp_path)
    fresh = dest.parent / f".{dest.name}.publish-staging-abcd1234"
    fresh.mkdir()

    publish._sweep_stale_publish_staging_dirs(dest, publish.RunTotals())

    assert fresh.is_dir()

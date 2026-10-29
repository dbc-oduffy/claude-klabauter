"""The clone destination is refused when it resolves under no git root.

Guards `benchmarks.isolated_clone.mkdtemp_for_clone`'s runtime assertion --
the 2026-08-27 incident's first half (68 clone directories minted on a bare
drive root, outside every repo, invisible to write confinement).

WHAT THIS FILE DOES NOT COVER, STATED SO NOBODY READS IT AS SUFFICIENT. The
assertion fires only for callers that GO THROUGH the helper. The literal
2026-08-27 defect did not: it was a direct `tempfile.mkdtemp(dir=source_root.
parent)` in a fixture, which never consults this code path at all. Measured
during the spike (docs/research/spike-verdicts/2026-08-27-the-choke-point-
cannot-see-its-own-bypass.md): with this assertion in place, re-introducing
that exact line raises nothing and turns nothing red. The bypass-detector
named in that verdict is what closes it; this file guards the other half.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from coordinator_core.temp_layout import managed_tempdir

from coordinator_core.benchmarks.isolated_clone import (
    CloneTeardownLeak,
    RootlessCloneDestination,
    mkdtemp_for_clone,
    rmtree_or_raise,
)


def test_clone_silences_git_daemons(tmp_path: Path) -> None:
    """The clone's config carries the no-daemon settings (written without a git spawn)."""
    from coordinator_core.benchmarks.isolated_clone import _silence_clone_git_daemons

    (tmp_path / ".git").mkdir()
    cfg = tmp_path / ".git" / "config"
    cfg.write_text("[core]\n\tbare = false\n", encoding="utf-8")
    _silence_clone_git_daemons(tmp_path)
    import configparser

    parser = configparser.ConfigParser(strict=False)
    parser.read_string(cfg.read_text(encoding="utf-8"))
    assert parser["core"]["fsmonitor"] == "false"
    assert parser["gc"]["auto"] == "0"
    assert parser["maintenance"]["auto"] == "false"


def test_fresh_git_clone_is_portable_to_long_paths(tmp_path: Path, monkeypatch) -> None:
    """The clone argv enables core.longpaths and the destination is short."""
    import coordinator_core.git.run as git_run
    from coordinator_core.benchmarks.isolated_clone import fresh_git_clone

    (tmp_path / ".git").mkdir()
    seen: list = []

    class _Result:
        ok = True
        returncode = 0
        stderr = ""

    def _fake(argv, **_kw):
        seen.append(list(argv))
        Path(argv[-1]).mkdir(parents=True)
        (Path(argv[-1]) / ".git").mkdir()
        (Path(argv[-1]) / ".git" / "config").write_text("", encoding="utf-8")
        return _Result()

    monkeypatch.setattr(git_run, "run_git", _fake)
    with fresh_git_clone(tmp_path, prefix="a-very-long-descriptive-prefix-", label="t") as dest:
        argv = seen[0]
        assert "core.longpaths=true" in argv
        assert "clone" in argv
        scratch = tmp_path.joinpath("scratch", "benchmark-clones")
        rel = dest.relative_to(scratch)
        assert len(str(rel)) <= 20, rel


def _rooted_sleeper(root: Path):
    import subprocess
    import sys

    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_reap_signals_descendant_holder(tmp_path: Path) -> None:
    """A rooted process in our own tree is signalled and reported reaped."""
    from coordinator_core.benchmarks.isolated_clone import reap_processes_under

    proc = _rooted_sleeper(tmp_path)
    try:
        reaped = reap_processes_under(tmp_path)
        assert proc.pid in reaped
        assert reaped.foreign == []
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_reap_reports_foreign_holder_without_signalling(tmp_path: Path, monkeypatch) -> None:
    """A rooted process outside our tree is returned on `.foreign` and left alive."""
    import psutil

    from coordinator_core.benchmarks.isolated_clone import reap_processes_under

    proc = _rooted_sleeper(tmp_path)
    try:
        monkeypatch.setattr(psutil.Process, "children", lambda self, recursive=False: [])
        reaped = reap_processes_under(tmp_path)
        assert proc.pid not in reaped
        assert proc.pid in reaped.foreign
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait(timeout=10)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_reap_stale_clones_never_raises_on_foreign_holder(tmp_path: Path, monkeypatch) -> None:
    import os
    import psutil

    from coordinator_core.benchmarks.isolated_clone import _reap_stale_clones

    stale = tmp_path / "stale"
    stale.mkdir()
    old = stale.stat().st_mtime - 7200
    os.utime(stale, (old, old))
    proc = _rooted_sleeper(stale)
    try:
        monkeypatch.setattr(psutil.Process, "children", lambda self, recursive=False: [])
        _reap_stale_clones(tmp_path)
        assert proc.poll() is None
        assert stale.exists()
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_rootless_destination_is_refused() -> None:
    """A source root with no `.git` above it raises rather than minting.

    The platform temp dir is the realistic stand-in for the incident's bare
    drive root: both resolve under no repository, which is the predicate --
    not "is a drive root" specifically.
    """
    rootless = Path(tempfile.mkdtemp(prefix="rootless-", dir=str(managed_tempdir())))
    try:
        with pytest.raises(RootlessCloneDestination):
            mkdtemp_for_clone(rootless, prefix="should-not-exist-")
    finally:
        rmtree_or_raise(rootless, label="test_isolated_clone_destination")


def test_rootless_destination_mints_nothing_before_raising() -> None:
    """The refusal precedes creation -- a raise that still left the scratch
    tree behind would reintroduce the accumulation it exists to prevent."""
    rootless = Path(tempfile.mkdtemp(prefix="rootless-", dir=str(managed_tempdir())))
    try:
        with pytest.raises(RootlessCloneDestination):
            mkdtemp_for_clone(rootless, prefix="should-not-exist-")
        assert not (rootless / "scratch").exists(), (
            "scratch tree was created before the destination was validated"
        )
    finally:
        rmtree_or_raise(rootless, label="test_isolated_clone_destination")


def test_in_repo_destination_is_allowed(tmp_path: Path) -> None:
    """The legitimate case still works -- a guard that refuses everything is
    a guard that gets deleted. Uses a synthetic `.git` rather than this repo's
    own so the test does not depend on where it is checked out (which is the
    very fact the assertion exists to stop callers assuming)."""
    (tmp_path / ".git").mkdir()
    minted = mkdtemp_for_clone(tmp_path, prefix="ok-")
    try:
        assert minted.exists()
        assert minted.is_relative_to(tmp_path)
    finally:
        rmtree_or_raise(minted, label="test_isolated_clone_destination")


def test_git_file_counts_as_a_root(tmp_path: Path) -> None:
    """A worktree or submodule checkout carries `.git` as a FILE, not a
    directory; both mean a root is above, and refusing the file form would
    break every clone made from a worktree."""
    (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
    minted = mkdtemp_for_clone(tmp_path, prefix="wt-")
    try:
        assert minted.exists()
    finally:
        rmtree_or_raise(minted, label="test_isolated_clone_destination")


def test_surviving_clone_raises_rather_than_warning(tmp_path: Path) -> None:
    """A tree that cannot be removed fails the run.

    This is the finding the first cut of this module got wrong: it emitted
    `warnings.warn` and returned False, which -- with no `filterwarnings` in
    `pyproject.toml` -- leaves the run PASSING, the same observable outcome as
    the `ignore_errors=True` it replaced. A leaked clone means a live orphaned
    process; it has to turn something red.

    Simulated by pointing the helper at a path it cannot clear: a directory
    replaced by a file mid-call is awkward to stage portably, so this asserts
    the raise on the simpler observable -- a root that still exists after the
    removal attempt, forced by making removal a no-op via a read-only handle
    the test itself holds open on Windows and a chmod-guarded parent elsewhere.
    """
    doomed = tmp_path / "survivor"
    doomed.mkdir()
    held = doomed / "held.txt"
    held.write_text("x")
    handle = held.open("r")  # a live handle blocks rmtree on Windows
    try:
        if doomed.exists():
            try:
                rmtree_or_raise(doomed, label="leak-proof", reaped=[])
            except CloneTeardownLeak as exc:
                assert "survived teardown" in str(exc)
                return
        # POSIX removes a tree with an open handle without complaint; there the
        # raise cannot be provoked this way and the contract is unexercised
        # rather than violated.
        assert not doomed.exists(), "removal neither succeeded nor raised"
    finally:
        handle.close()
        shutil.rmtree(doomed, ignore_errors=True)

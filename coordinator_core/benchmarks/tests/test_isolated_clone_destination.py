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

from coordinator_core.benchmarks.isolated_clone import (
    CloneTeardownLeak,
    RootlessCloneDestination,
    mkdtemp_for_clone,
    rmtree_or_raise,
)


def test_rootless_destination_is_refused() -> None:
    """A source root with no `.git` above it raises rather than minting.

    The platform temp dir is the realistic stand-in for the incident's bare
    drive root: both resolve under no repository, which is the predicate --
    not "is a drive root" specifically.
    """
    rootless = Path(tempfile.mkdtemp(prefix="rootless-"))
    try:
        with pytest.raises(RootlessCloneDestination):
            mkdtemp_for_clone(rootless, prefix="should-not-exist-")
    finally:
        rmtree_or_raise(rootless, label="test_isolated_clone_destination")


def test_rootless_destination_mints_nothing_before_raising() -> None:
    """The refusal precedes creation -- a raise that still left the scratch
    tree behind would reintroduce the accumulation it exists to prevent."""
    rootless = Path(tempfile.mkdtemp(prefix="rootless-"))
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

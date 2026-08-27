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
    RootlessCloneDestination,
    mkdtemp_for_clone,
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
        shutil.rmtree(rootless, ignore_errors=True)


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
        shutil.rmtree(rootless, ignore_errors=True)


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
        shutil.rmtree(minted, ignore_errors=True)


def test_git_file_counts_as_a_root(tmp_path: Path) -> None:
    """A worktree or submodule checkout carries `.git` as a FILE, not a
    directory; both mean a root is above, and refusing the file form would
    break every clone made from a worktree."""
    (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
    minted = mkdtemp_for_clone(tmp_path, prefix="wt-")
    try:
        assert minted.exists()
    finally:
        shutil.rmtree(minted, ignore_errors=True)

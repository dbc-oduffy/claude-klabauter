"""Tests for `resolve_repo_root_arg` (`coordinator_core/group_em/repo_root_arg.py`).

Regression for coordinator:code-reviewer.a89481390696514f7 (P1): the drive
requirement added to catch a Windows drive-relative mangling (`X:name`) was
applied unconditionally, so `ntpath`-free platforms (POSIX, where
`os.path.splitdrive` always returns an empty drive) refused every legitimate
absolute repo root. Pins both directions so this cannot regress either way.
"""

from __future__ import annotations

import posixpath
from unittest import mock

import pytest

from coordinator_core.group_em import repo_root_arg


def test_posix_absolute_root_accepted_when_not_windows():
    """A driveless POSIX-style absolute root must not be refused off-Windows.

    `os.name`/`os.path` are bound at interpreter startup and can't be swapped
    to a real POSIX runtime from a Windows test host, so this patches
    `repo_root_arg.os.path` to `posixpath` directly -- the same substitution
    that actually happens when this module runs on a POSIX box -- and
    confirms the drive-gate no longer fires. Uses a path that does not exist
    on this host: the point is to confirm it clears the drive-gate, surfacing
    as an "existing directory" refusal rather than a "not absolute,
    drive-anchored" one -- proof the POSIX shape was accepted by the gate
    this finding is about, not that the whole call succeeds on a foreign
    filesystem.
    """
    with mock.patch.object(repo_root_arg.os, "name", "posix"), \
            mock.patch.object(repo_root_arg.os, "path", posixpath):
        with pytest.raises(repo_root_arg.RepoRootArgError) as exc_info:
            repo_root_arg.resolve_repo_root_arg("/home/user/repo")  # abs-path-ok: synthetic POSIX fixture, not a machine-local path
    assert "not an existing directory" in str(exc_info.value)


def test_drive_relative_mangling_still_refused_on_windows():
    """The original incident shape (`X:name`, no separator) stays refused on Windows."""
    with mock.patch.object(repo_root_arg.os, "name", "nt"):
        with pytest.raises(repo_root_arg.RepoRootArgError, match="drive-anchored"):
            repo_root_arg.resolve_repo_root_arg("X:example-game-workbench-repo")


def test_driveless_rooted_path_still_refused_on_windows(tmp_path):
    """`/foo/bar` under ntpath semantics binds to the process's current drive -- refuse it."""
    with mock.patch.object(repo_root_arg.os, "name", "nt"):
        with pytest.raises(repo_root_arg.RepoRootArgError, match="drive-anchored"):
            repo_root_arg.resolve_repo_root_arg("/foo/bar")

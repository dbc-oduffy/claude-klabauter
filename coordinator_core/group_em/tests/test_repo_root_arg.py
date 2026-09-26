
from __future__ import annotations

import posixpath
from unittest import mock

import pytest

from coordinator_core.group_em import repo_root_arg


def test_posix_absolute_root_accepted_when_not_windows():
    with mock.patch.object(repo_root_arg.os, "name", "posix"), \
            mock.patch.object(repo_root_arg.os, "path", posixpath):
        with pytest.raises(repo_root_arg.RepoRootArgError) as exc_info:
            repo_root_arg.resolve_repo_root_arg("/home/user/repo")
    assert "not an existing directory" in str(exc_info.value)


def test_drive_relative_mangling_still_refused_on_windows():
    with mock.patch.object(repo_root_arg.os, "name", "nt"):
        with pytest.raises(repo_root_arg.RepoRootArgError, match="drive-anchored"):
            repo_root_arg.resolve_repo_root_arg("X:example-game-workbench-repo")


def test_driveless_rooted_path_still_refused_on_windows(tmp_path):
    with mock.patch.object(repo_root_arg.os, "name", "nt"):
        with pytest.raises(repo_root_arg.RepoRootArgError, match="drive-anchored"):
            repo_root_arg.resolve_repo_root_arg("/foo/bar")

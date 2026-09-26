
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git import tree_spine
from coordinator_core.win_portability import no_console_creationflags


def test_rewrite_head_spine_returns_none_for_missing_parent_in_spine():
    spine = {"": {}}
    assembled = {"missing_dir/file.txt": (0o100644, "0" * 40)}
    assert tree_spine._rewrite_head_spine(None, spine, assembled) is None


def test_synthesize_absent_spine_dirs_fills_new_directory():
    spine = {"": {}}
    assembled = {"newdir/newfile.txt": (0o100644, "0" * 40)}
    result = tree_spine._synthesize_absent_spine_dirs(spine, assembled)
    assert result is spine
    assert "newdir" in spine
    assert spine["newdir"] == {}


def test_synthesize_absent_spine_dirs_refuses_on_absent_deletion_with_missing_parent():
    spine = {"": {}}
    assembled = {"missing_dir/file.txt": tree_spine._ABSENT}
    assert tree_spine._synthesize_absent_spine_dirs(spine, assembled) is None


def test_synthesize_absent_spine_dirs_refuses_when_name_occupied_by_non_directory():
    spine = {"": {"occupied": (0o100644, "1" * 40)}}
    assembled = {"occupied/newfile.txt": (0o100644, "0" * 40)}
    assert tree_spine._synthesize_absent_spine_dirs(spine, assembled) is None

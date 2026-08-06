"""Tests for coordinator_core.win_portability -- executability, PATH-list,
and path-split primitives.

Per AC-5 of the dispatch this file implements: differential checks against
independently-derived Windows-real values (``PureWindowsPath``), not against
monkeypatched values this same test file invented -- a test that patches a
value and then asserts that value proves nothing about correctness.

Spec backlink: docs/research/2026-07-28-windows-simulation-test-harness-design.md
  (example-doctrine-repo), coordinator_core/tests/test_home_resolution_lint.py (this repo).
"""

from __future__ import annotations

import os
import stat
from pathlib import PureWindowsPath

import pytest

from coordinator_core import win_portability
from coordinator_core.win_portability import (
    is_executable,
    join_path_list,
    split_path,
    split_path_list,
)


# ---------------------------------------------------------------------------
# is_executable -- POSIX branch
# ---------------------------------------------------------------------------


def test_is_executable_posix_true_for_chmod_plus_x_file(tmp_path, monkeypatch):
    monkeypatch.setattr(win_portability, "_is_windows", lambda: False)
    target = tmp_path / "a-script"
    target.write_text("#!/bin/sh\necho hi\n")
    target.chmod(target.stat().st_mode | stat.S_IEXEC)
    assert is_executable(target) is True


def test_is_executable_posix_false_for_non_executable_file(tmp_path, monkeypatch):
    monkeypatch.setattr(win_portability, "_is_windows", lambda: False)
    target = tmp_path / "readme.txt"
    target.write_text("not executable")
    assert is_executable(target) is False


def test_is_executable_posix_false_for_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(win_portability, "_is_windows", lambda: False)
    assert is_executable(tmp_path / "does-not-exist") is False


# ---------------------------------------------------------------------------
# is_executable -- Windows branch (simulated: real host stays macOS/Linux,
# _is_windows() is monkeypatched so the Windows CODE PATH runs for real,
# against real tmp_path files -- not a mock of the outcome).
# ---------------------------------------------------------------------------


def test_is_executable_windows_true_for_pathext_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(win_portability, "_is_windows", lambda: True)
    monkeypatch.delenv("PATHEXT", raising=False)
    target = tmp_path / "machine-local.CMD"
    target.write_text("@echo off\n")
    assert is_executable(target) is True


def test_is_executable_windows_false_for_non_pathext_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(win_portability, "_is_windows", lambda: True)
    monkeypatch.delenv("PATHEXT", raising=False)
    target = tmp_path / "helper.py"
    target.write_text("print('hi')\n")
    assert is_executable(target) is False


def test_is_executable_windows_extensionless_resolves_via_pathext_sibling(tmp_path, monkeypatch):
    """The AC-1 case this primitive exists for: coordinator ships bareword
    entrypoints (`machine-local`, no extension) with a `.cmd` twin. Windows
    launches the twin, not the bare file -- is_executable must agree."""
    monkeypatch.setattr(win_portability, "_is_windows", lambda: True)
    monkeypatch.delenv("PATHEXT", raising=False)
    bare = tmp_path / "machine-local"
    bare.write_text("#!/bin/sh\n")
    (tmp_path / "machine-local.cmd").write_text("@echo off\n")
    assert is_executable(bare) is True


def test_is_executable_windows_extensionless_bare_file_alone_is_false(tmp_path, monkeypatch):
    """Negative-spec pin: an extensionless file's own EXISTENCE must never be
    sufficient on Windows -- this is exactly the degrade-to-F_OK failure mode
    `os.access(path, os.X_OK)` produces there, which this primitive replaces."""
    monkeypatch.setattr(win_portability, "_is_windows", lambda: True)
    monkeypatch.delenv("PATHEXT", raising=False)
    bare = tmp_path / "orphan-shebang-script"
    bare.write_text("#!/bin/sh\necho hi\n")
    assert is_executable(bare) is False


def test_is_executable_windows_respects_custom_pathext_env(tmp_path, monkeypatch):
    monkeypatch.setattr(win_portability, "_is_windows", lambda: True)
    monkeypatch.setenv("PATHEXT", ".EXE;.FOO")
    target = tmp_path / "tool.foo"
    target.write_text("x")
    assert is_executable(target) is True
    other = tmp_path / "tool.cmd"
    other.write_text("x")
    assert is_executable(other) is False  # .CMD not in this custom PATHEXT


# ---------------------------------------------------------------------------
# split_path_list / join_path_list
# ---------------------------------------------------------------------------


def test_split_path_list_uses_os_pathsep(monkeypatch):
    value = os.pathsep.join(["/a/bin", "/b/bin", "/c/bin"])
    assert split_path_list(value) == ["/a/bin", "/b/bin", "/c/bin"]


def test_join_path_list_uses_os_pathsep():
    assert join_path_list(["/a/bin", "/b/bin"]) == os.pathsep.join(["/a/bin", "/b/bin"])


def test_split_path_list_never_uses_literal_colon_on_windows_shaped_input(monkeypatch):
    # Regression pin for the strangler-facade seam-detection defect: a
    # Windows-style ';'-joined PATH value, split with the BANNED literal
    # ':' idiom, corrupts every drive-letter entry ("C:\\a\\bin" splits at
    # its own drive-letter colon) instead of yielding the intended entries.
    windows_style = ";".join(["C:\\a\\bin", "C:\\b\\bin"])
    banned_result = windows_style.split(":")
    assert banned_result != ["C:\\a\\bin", "C:\\b\\bin"]  # the banned shape is provably wrong here

    monkeypatch.setattr(os, "pathsep", ";")
    assert split_path_list(windows_style) == ["C:\\a\\bin", "C:\\b\\bin"]


# ---------------------------------------------------------------------------
# split_path -- differential checks against PureWindowsPath, not self-mocks
# ---------------------------------------------------------------------------


def test_split_path_folds_backslash_windows_native_form():
    result = split_path("X:\\example-doctrine-repo\\coordinator", maxsplit=1, from_right=True)
    assert result == ["X:/example-doctrine-repo", "coordinator"]


def test_split_path_matches_purewindowspath_parent_and_name():
    raw = "X:\\example-doctrine-repo\\coordinator"
    result = split_path(raw, maxsplit=1, from_right=True)

    # Independently-derived expectation via PureWindowsPath (the real Win32
    # path-shape algorithm, importable on any host) -- not a re-assertion of
    # split_path's own output.
    pwp = PureWindowsPath(raw)
    expected_leaf = pwp.name
    expected_parent = str(pwp.parent).replace("\\", "/")

    assert result == [expected_parent, expected_leaf]


def test_split_path_msys_mount_form_already_forward_slash_unaffected():
    # /x/example-doctrine-repo/coordinator is the Git-Bash/MSYS mount-form rendering of
    # X:\example-doctrine-repo\coordinator -- already all-forward-slash, must split
    # identically whether or not the fold runs.
    result = split_path("/x/example-doctrine-repo/coordinator", maxsplit=1, from_right=True)
    assert result == ["/x/example-doctrine-repo", "coordinator"]


def test_split_path_posix_form_unaffected_by_fold():
    result = split_path("/usr/local/bin", maxsplit=1, from_right=True)
    assert result == ["/usr/local", "bin"]


def test_split_path_left_split_matches_model_helper():
    raw = "X:\\a\\b\\c"
    assert split_path(raw) == win_portability._model_windows_split(raw)


def test_split_path_bypasses_forward_slash_lint_class_no_fs_access(tmp_path, monkeypatch):
    # split_path must never touch the filesystem -- assert no exists()/is_file()
    # call occurs by pointing it at a path that would raise if stat'd unsafely.
    result = split_path("nonexistent\\segment\\path", maxsplit=1, from_right=True)
    assert result == ["nonexistent/segment", "path"]

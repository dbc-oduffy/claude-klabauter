
from __future__ import annotations

import os
import sys

import pytest

from coordinator_core import launchable
from coordinator_core.launchable import resolve_launchable, which_path_ordered


@pytest.fixture
def as_nt(monkeypatch):
    monkeypatch.setattr(launchable, "_is_windows", lambda: True)


@pytest.fixture
def as_posix(monkeypatch):
    monkeypatch.setattr(launchable, "_is_windows", lambda: False)


@pytest.mark.parametrize(
    "script",
    ["/x/query-records.js", "/x/verify-no-console-flash.sh", "/x/machine-local", "/x/t.py"],
)
def test_posix_is_always_bare_path(as_posix, script):
    assert resolve_launchable(script) == [script]


def test_posix_ignores_a_cmd_twin(as_posix, tmp_path):
    script = tmp_path / "query-records.js"
    script.write_text("//\n")
    (tmp_path / "query-records.js.cmd").write_text("@echo off\n")
    assert resolve_launchable(str(script)) == [str(script)]


def test_nt_prefers_cmd_twin_over_interpreter_prefix(as_nt, tmp_path):
    script = tmp_path / "query-records.js"
    script.write_text("//\n")
    twin = tmp_path / "query-records.js.cmd"
    twin.write_text("@echo off\n")
    assert resolve_launchable(str(script)) == [str(twin)]


def test_nt_prefers_cmd_twin_for_extensionless_script(as_nt, tmp_path):
    script = tmp_path / "machine-local"
    script.write_text("#!/usr/bin/env node\n")
    twin = tmp_path / "machine-local.cmd"
    twin.write_text("@echo off\n")
    assert resolve_launchable(str(script)) == [str(twin)]


def test_nt_ignores_a_cmd_twin_that_is_a_directory(as_nt, tmp_path):
    script = tmp_path / "thing.sh"
    script.write_text("#!/usr/bin/env bash\n")
    (tmp_path / "thing.sh.cmd").mkdir()
    assert resolve_launchable(str(script))[-1] == str(script)
    assert len(resolve_launchable(str(script))) == 2


@pytest.mark.parametrize("suffix", [".js", ".cjs", ".mjs"])
def test_nt_js_family_gets_node_prefix(as_nt, suffix):
    vector = resolve_launchable(f"C:\\x\\query-records{suffix}")
    assert len(vector) == 2
    assert os.path.basename(vector[0]).lower().startswith("node")
    assert vector[1] == f"C:\\x\\query-records{suffix}"


@pytest.mark.parametrize("suffix", [".sh", ".bash"])
def test_nt_shell_family_gets_bash_prefix(as_nt, suffix):
    vector = resolve_launchable(f"C:\\x\\verify{suffix}")
    assert len(vector) == 2
    assert os.path.basename(vector[0]).lower().startswith("bash")


def test_nt_py_uses_this_interpreter_not_a_path_probe(as_nt):
    assert resolve_launchable("C:\\x\\t.py") == [sys.executable, "C:\\x\\t.py"]


def test_nt_extension_match_is_case_insensitive(as_nt):
    assert len(resolve_launchable("C:\\x\\QUERY-RECORDS.JS")) == 2


@pytest.mark.parametrize("script", ["C:\\x\\machine-local", "C:\\x\\thing.exe", "C:\\x\\a.pl"])
def test_nt_unknown_shape_falls_through_to_bare_path(as_nt, script):
    assert resolve_launchable(script) == [script]


@pytest.mark.parametrize("windows", [True, False])
def test_script_is_always_the_last_element(monkeypatch, windows):
    monkeypatch.setattr(launchable, "_is_windows", lambda: windows)
    for script in ("C:\\x\\a.js", "C:\\x\\b.sh", "C:\\x\\c", "C:\\x\\d.py"):
        assert resolve_launchable(script)[-1] == script


def test_accepts_pathlike(as_posix, tmp_path):
    script = tmp_path / "a.js"
    assert resolve_launchable(script) == [str(script)]


def _set_path(monkeypatch, *dirs):
    monkeypatch.setenv("PATH", os.pathsep.join(dirs))


def test_directory_major_extension_minor_ordering(monkeypatch, tmp_path):
    dir1 = tmp_path / "dir1"
    dir2 = tmp_path / "dir2"
    dir1.mkdir()
    dir2.mkdir()
    bat = dir1 / "tool.BAT"
    bat.write_text("@echo off\n")
    exe = dir2 / "tool.EXE"
    exe.write_text("binary\n")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT")
    _set_path(monkeypatch, str(dir1), str(dir2))
    monkeypatch.setattr(launchable, "_is_windows", lambda: True)

    result = which_path_ordered("tool")

    assert result == str(bat)


def test_pathext_order_honoured_within_a_directory(monkeypatch, tmp_path):
    d = tmp_path / "dir1"
    d.mkdir()
    (d / "tool.BAT").write_text("@echo off\n")
    exe = d / "tool.EXE"
    exe.write_text("binary\n")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT")
    _set_path(monkeypatch, str(d))
    monkeypatch.setattr(launchable, "_is_windows", lambda: True)

    assert which_path_ordered("tool") == str(exe)


def test_bare_extensionless_name_is_a_candidate(monkeypatch, tmp_path):
    d = tmp_path / "dir1"
    d.mkdir()
    bare = d / "tool"
    bare.write_text("#!/bin/sh\n")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT")
    _set_path(monkeypatch, str(d))
    monkeypatch.setattr(launchable, "_is_windows", lambda: True)

    assert which_path_ordered("tool") == str(bare)


def test_extensions_empty_list_matches_only_literal_filename(monkeypatch, tmp_path):
    d = tmp_path / "dir1"
    d.mkdir()
    shim = d / "tool.sh"
    shim.write_text("#!/bin/sh\n")
    # A decoy that a naive PATHEXT-appending search could wrongly prefer.
    (d / "tool.sh.EXE").write_text("binary\n")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT")
    _set_path(monkeypatch, str(d))
    monkeypatch.setattr(launchable, "_is_windows", lambda: True)

    assert which_path_ordered("tool.sh", extensions=[]) == str(shim)


def test_posix_default_is_bare_name_only_no_pathext(monkeypatch, tmp_path):
    """On POSIX (per the module's own `_is_windows` seam) `extensions`
    defaults to `[]`, matching shutil.which's own platform default -- a
    PATHEXT-suffixed sibling must NOT be picked up even if PATHEXT happens
    to be set in the environment (e.g. inherited from a cross-platform CI
    box)."""
    d = tmp_path / "dir1"
    d.mkdir()
    bare = d / "tool"
    bare.write_text("#!/bin/sh\n")
    (d / "tool.EXE").write_text("binary\n")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT")
    _set_path(monkeypatch, str(d))
    monkeypatch.setattr(launchable, "_is_windows", lambda: False)

    assert which_path_ordered("tool") == str(bare)


def test_not_found_returns_none(monkeypatch, tmp_path):
    d = tmp_path / "dir1"
    d.mkdir()
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT")
    _set_path(monkeypatch, str(d))
    monkeypatch.setattr(launchable, "_is_windows", lambda: True)

    assert which_path_ordered("nope") is None


def test_empty_path_entry_is_skipped(monkeypatch, tmp_path):
    d = tmp_path / "dir1"
    d.mkdir()
    target = d / "tool"
    target.write_text("#!/bin/sh\n")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT")
    _set_path(monkeypatch, "", str(d))
    monkeypatch.setattr(launchable, "_is_windows", lambda: True)

    assert which_path_ordered("tool") == str(target)


def test_path_entry_that_is_not_a_directory_is_skipped(monkeypatch, tmp_path):
    not_a_dir = tmp_path / "not-a-dir.txt"
    not_a_dir.write_text("x\n")
    d = tmp_path / "dir1"
    d.mkdir()
    target = d / "tool"
    target.write_text("#!/bin/sh\n")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT")
    _set_path(monkeypatch, str(not_a_dir), str(d))
    monkeypatch.setattr(launchable, "_is_windows", lambda: True)

    assert which_path_ordered("tool") == str(target)


def test_name_that_already_carries_an_extension_tries_pathext_twins_first(monkeypatch, tmp_path):
    """With the default `extensions` (PATHEXT), a name that already ends in
    its own extension still gets PATHEXT-suffixed candidates tried first
    (`tool.sh.EXE`, ...) before the bare `tool.sh` -- this is the case
    `which_path_ordered`'s docstring calls out as `shutil.which` getting
    wrong (it never tries the literal filename at all); here we confirm
    THIS function does still fall through to the bare literal name when no
    PATHEXT-suffixed twin exists."""
    d = tmp_path / "dir1"
    d.mkdir()
    shim = d / "tool.sh"
    shim.write_text("#!/bin/sh\n")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT")
    _set_path(monkeypatch, str(d))
    monkeypatch.setattr(launchable, "_is_windows", lambda: True)

    assert which_path_ordered("tool.sh") == str(shim)

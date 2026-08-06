"""Tests for coordinator_core.launchable.resolve_launchable.

Both platform branches are exercised on every host by patching the module's
``_is_windows`` seam -- the whole point of this module is a defect that only
manifests on Windows, so a suite that skipped the nt branch off-Windows would test
nothing that matters. (``os.name`` itself is deliberately NOT patched: ``pathlib``
keys its concrete-class selection on it and raises mid-test.)
"""

from __future__ import annotations

import os
import sys

import pytest

from coordinator_core import launchable
from coordinator_core.launchable import resolve_by_shebang, resolve_launchable


@pytest.fixture
def as_nt(monkeypatch):
    monkeypatch.setattr(launchable, "_is_windows", lambda: True)


@pytest.fixture
def as_posix(monkeypatch):
    monkeypatch.setattr(launchable, "_is_windows", lambda: False)


# ---------------------------------------------------------------------------
# POSIX -- bare path always (the shebang is authoritative there)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Windows -- tier 1: the .cmd twin wins when present
# ---------------------------------------------------------------------------


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
    """isfile, not exists -- a directory named ``<script>.cmd`` is not a launcher."""
    script = tmp_path / "thing.sh"
    script.write_text("#!/usr/bin/env bash\n")
    (tmp_path / "thing.sh.cmd").mkdir()
    assert resolve_launchable(str(script))[-1] == str(script)
    assert len(resolve_launchable(str(script))) == 2  # fell through to bash prefix


# ---------------------------------------------------------------------------
# Windows -- tier 2: interpreter prefix by extension
# ---------------------------------------------------------------------------


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
    """venv-correct by construction -- see _interpreter_for's docstring."""
    assert resolve_launchable("C:\\x\\t.py") == [sys.executable, "C:\\x\\t.py"]


def test_nt_extension_match_is_case_insensitive(as_nt):
    assert len(resolve_launchable("C:\\x\\QUERY-RECORDS.JS")) == 2


# ---------------------------------------------------------------------------
# Windows -- tier 3: bare path when nothing better is known
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", ["C:\\x\\machine-local", "C:\\x\\thing.exe", "C:\\x\\a.pl"])
def test_nt_unknown_shape_falls_through_to_bare_path(as_nt, script):
    assert resolve_launchable(script) == [script]


# ---------------------------------------------------------------------------
# Shape contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("windows", [True, False])
def test_script_is_always_the_last_element(monkeypatch, windows):
    """`[*resolve_launchable(p), *args]` is only correct if p stays adjacent to args."""
    monkeypatch.setattr(launchable, "_is_windows", lambda: windows)
    for script in ("C:\\x\\a.js", "C:\\x\\b.sh", "C:\\x\\c", "C:\\x\\d.py"):
        assert resolve_launchable(script)[-1] == script


def test_accepts_pathlike(as_posix, tmp_path):
    script = tmp_path / "a.js"
    assert resolve_launchable(script) == [str(script)]


# ---------------------------------------------------------------------------
# resolve_by_shebang -- explicit-interpreter resolution, exec-bit independent
# ---------------------------------------------------------------------------


def test_shebang_env_form_python_resolves_to_sys_executable(as_posix, tmp_path):
    script = tmp_path / "seed-skill-overrides.sh"
    script.write_text("#!/usr/bin/env python3\nprint('hi')\n", encoding="utf-8")
    assert resolve_by_shebang(str(script)) == [sys.executable, str(script)]


def test_shebang_env_form_python_bare_resolves_to_sys_executable(as_posix, tmp_path):
    script = tmp_path / "thing.sh"
    script.write_text("#!/usr/bin/env python\nprint('hi')\n", encoding="utf-8")
    assert resolve_by_shebang(str(script)) == [sys.executable, str(script)]


def _interpreter_stem(path: str) -> str:
    """Basename minus any executable extension, lowercased.

    `shutil.which("bash")` answers `bash` on POSIX and a full path ending
    `bash.EXE` on Windows, so a bare basename comparison is a platform
    assertion wearing an interpreter assertion's clothes. Splitting the
    extension keeps the check EXACT — unlike a `startswith("bash")` form,
    which also accepts a `bashfoo` on PATH.
    """
    return os.path.splitext(os.path.basename(path))[0].lower()


def test_shebang_direct_form_bash_resolves_through_which(as_posix, tmp_path):
    script = tmp_path / "thing.sh"
    script.write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
    vector = resolve_by_shebang(str(script))
    assert len(vector) == 2
    assert _interpreter_stem(vector[0]) == "bash"
    assert vector[1] == str(script)


def test_shebang_direct_form_sh_resolves_through_which(as_posix, tmp_path):
    script = tmp_path / "thing.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    vector = resolve_by_shebang(str(script))
    assert len(vector) == 2
    assert _interpreter_stem(vector[0]) == "sh"
    assert vector[1] == str(script)


def test_no_shebang_falls_back_to_bash(as_posix, tmp_path):
    script = tmp_path / "thing.sh"
    script.write_text("echo hi\n", encoding="utf-8")
    assert resolve_by_shebang(str(script)) == ["bash", str(script)]


def test_empty_file_falls_back_to_bash(as_posix, tmp_path):
    script = tmp_path / "thing.sh"
    script.write_text("", encoding="utf-8")
    assert resolve_by_shebang(str(script)) == ["bash", str(script)]


def test_unreadable_binary_first_line_falls_back_to_bash_without_raising(as_posix, tmp_path):
    script = tmp_path / "thing.sh"
    script.write_bytes(b"\xff\xfe\x00\x01binary garbage\n")
    assert resolve_by_shebang(str(script)) == ["bash", str(script)]


def test_missing_file_falls_back_to_bash_without_raising(as_posix, tmp_path):
    script = tmp_path / "does-not-exist.sh"
    assert resolve_by_shebang(str(script)) == ["bash", str(script)]


def test_result_always_ends_with_the_script_path_itself(as_posix, tmp_path):
    """resolve_by_shebang folds the script into the returned vector,
    matching resolve_launchable's convention -- no caller-side branching."""
    script = tmp_path / "thing.sh"
    script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    assert resolve_by_shebang(str(script))[-1] == str(script)


def test_nt_prefers_cmd_twin_for_shebang_resolution(as_nt, tmp_path):
    script = tmp_path / "seed-skill-overrides.sh"
    script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    twin = tmp_path / "seed-skill-overrides.sh.cmd"
    twin.write_text("@echo off\n", encoding="utf-8")
    assert resolve_by_shebang(str(script)) == [str(twin)]


def test_shebang_env_dash_s_form_resolves_interpreter_not_the_flag(as_posix, tmp_path):
    """Regression for Finding 2: `env -S python3 -u` must resolve to python,
    not mis-parse `-S` itself as the interpreter name."""
    script = tmp_path / "thing.sh"
    script.write_text("#!/usr/bin/env -S python3 -u\nprint('hi')\n", encoding="utf-8")
    assert resolve_by_shebang(str(script)) == [sys.executable, str(script)]


def test_shebang_env_dash_s_only_flags_falls_back_to_bash(as_posix, tmp_path):
    """`env` with only flag tokens (no interpreter name at all) has nothing
    to resolve -- fail closed to the bash fallback rather than mis-picking
    a flag as the interpreter."""
    script = tmp_path / "thing.sh"
    script.write_text("#!/usr/bin/env -S\necho hi\n", encoding="utf-8")
    assert resolve_by_shebang(str(script)) == ["bash", str(script)]


def test_shebang_with_leading_utf8_bom_still_resolves(as_posix, tmp_path):
    """Regression for Finding 3: a UTF-8 BOM before `#!` must not make the
    shebang line invisible and silently fall back to bash."""
    script = tmp_path / "thing.sh"
    script.write_bytes(b"\xef\xbb\xbf#!/usr/bin/env python3\n" + b"print('hi')\n")
    assert resolve_by_shebang(str(script)) == [sys.executable, str(script)]

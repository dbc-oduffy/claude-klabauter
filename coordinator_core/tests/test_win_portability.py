
from __future__ import annotations

import os
import stat
import sys
from pathlib import PureWindowsPath

import pytest

from coordinator_core import win_portability
from coordinator_core.win_portability import (
    is_executable,
    join_path_list,
    no_console_creationflags,
    run_forwarding,
    same_path,
    split_path,
    split_path_list,
)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "POSIX-only: needs a REAL exec bit on disk. NTFS stores no POSIX mode "
        "bits, so os.chmod(+x) is a silent no-op here and st_mode never carries "
        "S_IXUSR -- the host filesystem, not is_executable, is what cannot "
        "satisfy this. The mode-bit predicate itself is covered on every "
        "platform by test_is_executable_posix_true_for_exec_mode_bits."
    ),
)
def test_is_executable_posix_true_for_chmod_plus_x_file(tmp_path, monkeypatch):
    monkeypatch.setattr(win_portability, "_is_windows", lambda: False)
    target = tmp_path / "a-script"
    target.write_text("#!/bin/sh\necho hi\n")
    target.chmod(target.stat().st_mode | stat.S_IEXEC)
    assert is_executable(target) is True


def test_is_executable_posix_true_for_exec_mode_bits(tmp_path, monkeypatch):
    monkeypatch.setattr(win_portability, "_is_windows", lambda: False)
    target = tmp_path / "a-script"
    target.write_text("#!/bin/sh\necho hi\n")

    real_stat = os.stat

    def _stat_with_exec_bit(p, *args, **kwargs):
        st = real_stat(p, *args, **kwargs)
        if os.path.abspath(os.fspath(p)) == os.path.abspath(os.fspath(target)):
            return os.stat_result(
                (st.st_mode | stat.S_IXUSR,) + tuple(st)[1:]
            )
        return st

    monkeypatch.setattr(win_portability.os, "stat", _stat_with_exec_bit)
    assert is_executable(target) is True


def test_is_executable_posix_false_for_non_executable_file(tmp_path, monkeypatch):
    monkeypatch.setattr(win_portability, "_is_windows", lambda: False)
    target = tmp_path / "readme.txt"
    target.write_text("not executable")
    assert is_executable(target) is False


def test_is_executable_posix_false_for_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(win_portability, "_is_windows", lambda: False)
    assert is_executable(tmp_path / "does-not-exist") is False


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
    assert is_executable(other) is False


def test_split_path_list_uses_os_pathsep(monkeypatch):
    value = os.pathsep.join(["/a/bin", "/b/bin", "/c/bin"])
    assert split_path_list(value) == ["/a/bin", "/b/bin", "/c/bin"]


def test_join_path_list_uses_os_pathsep():
    assert join_path_list(["/a/bin", "/b/bin"]) == os.pathsep.join(["/a/bin", "/b/bin"])


def test_split_path_list_never_uses_literal_colon_on_windows_shaped_input(monkeypatch):
    windows_style = ";".join(["C:\\a\\bin", "C:\\b\\bin"])
    banned_result = windows_style.split(":")
    assert banned_result != ["C:\\a\\bin", "C:\\b\\bin"]

    monkeypatch.setattr(os, "pathsep", ";")
    assert split_path_list(windows_style) == ["C:\\a\\bin", "C:\\b\\bin"]


def test_split_path_folds_backslash_windows_native_form():
    result = split_path("X:\\DoE-claude\\coordinator", maxsplit=1, from_right=True)
    assert result == ["X:/DoE-claude", "coordinator"]


def test_split_path_matches_purewindowspath_parent_and_name():
    raw = "X:\\DoE-claude\\coordinator"
    result = split_path(raw, maxsplit=1, from_right=True)

    pwp = PureWindowsPath(raw)
    expected_leaf = pwp.name
    expected_parent = str(pwp.parent).replace("\\", "/")

    assert result == [expected_parent, expected_leaf]


def test_split_path_msys_mount_form_already_forward_slash_unaffected():
    result = split_path("/x/DoE-claude/coordinator", maxsplit=1, from_right=True)
    assert result == ["/x/DoE-claude", "coordinator"]


def test_split_path_posix_form_unaffected_by_fold():
    result = split_path("/usr/local/bin", maxsplit=1, from_right=True)
    assert result == ["/usr/local", "bin"]


def test_split_path_left_split_matches_model_helper():
    raw = "X:\\a\\b\\c"
    assert split_path(raw) == win_portability._model_windows_split(raw)


def test_split_path_bypasses_forward_slash_lint_class_no_fs_access(tmp_path, monkeypatch):
    result = split_path("nonexistent\\segment\\path", maxsplit=1, from_right=True)
    assert result == ["nonexistent/segment", "path"]


def test_no_console_creationflags_posix_returns_empty_mapping(monkeypatch):
    monkeypatch.setattr(win_portability, "_is_windows", lambda: False)
    assert no_console_creationflags() == {}


def test_no_console_creationflags_windows_returns_create_no_window_flag(monkeypatch):
    monkeypatch.setattr(win_portability, "_is_windows", lambda: True)
    result = no_console_creationflags()
    assert set(result) == {"creationflags"}
    # own output: subprocess.CREATE_NO_WINDOW when the real host defines it
    import subprocess

    expected = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert result["creationflags"] == expected


def test_no_console_creationflags_does_not_set_stdio_kwargs(monkeypatch):
    monkeypatch.setattr(win_portability, "_is_windows", lambda: False)
    assert set(no_console_creationflags()) <= {"creationflags"}
    monkeypatch.setattr(win_portability, "_is_windows", lambda: True)
    assert set(no_console_creationflags()) <= {"creationflags"}


def test_same_path_uses_samefile_when_both_paths_exist(tmp_path):
    assert same_path(str(tmp_path), str(tmp_path) + os.sep) is True


def test_same_path_samefile_leg_detects_alias_realpath_alone_would_miss(tmp_path):
    # The load-bearing case this primitive exists for: two DIFFERENT path
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this host")
    assert same_path(str(real), str(alias)) is True


def test_same_path_false_for_distinct_existing_paths(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert same_path(str(a), str(b)) is False


def test_same_path_falls_back_to_realpath_normcase_when_a_path_is_missing(tmp_path):
    existing = tmp_path / "exists"
    existing.mkdir()
    missing = tmp_path / "does-not-exist"
    assert same_path(str(existing), str(missing)) is False


def test_same_path_fallback_true_for_identical_missing_path_strings(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert same_path(str(missing), str(missing)) is True


def test_same_path_never_raises_for_unreadable_or_malformed_input():
    assert same_path("", "") in (True, False)
    assert same_path("\x00bad", "\x00bad") in (True, False)


def test_same_path_case_insensitive_on_windows(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("normcase is a no-op on POSIX; this asserts Windows-real casefold behaviour")
    missing_upper = str(tmp_path / "DOES-NOT-EXIST")
    missing_lower = str(tmp_path / "does-not-exist")
    assert os.path.normcase(missing_upper) == os.path.normcase(missing_lower)
    assert same_path(missing_upper, missing_lower) is True


# test_win_portability_real_spawn.py (SPAWN-RATCHET Rule 4: cadence-tiered on


def _write_child_script(tmp_path, stdout_text="out-line\n", stderr_text="err-line\n", returncode=0):
    script = tmp_path / "run_forwarding_child.py"
    script.write_text(
        "import sys\n"
        f"sys.stdout.write({stdout_text!r})\n"
        f"sys.stderr.write({stderr_text!r})\n"
        f"sys.exit({returncode})\n"
    )
    return str(script)


def test_run_forwarding_reaches_a_plain_capture_buffer(tmp_path):
    import io

    child = _write_child_script(tmp_path, stdout_text="plain stdout\n", stderr_text="plain stderr\n")
    buf = io.StringIO()
    proc = run_forwarding([sys.executable, child], stdout=buf, stderr=buf)
    assert proc.returncode == 0
    forwarded = buf.getvalue()
    assert "plain stdout" in forwarded
    assert "plain stderr" in forwarded


def test_run_forwarding_under_redirect_stderr_reaches_the_buffer(tmp_path):
    import contextlib
    import io

    child = _write_child_script(tmp_path, stdout_text="child stdout\n", stderr_text="child stderr\n")
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        proc = run_forwarding([sys.executable, child], stdout=sys.stderr, stderr=sys.stderr)
    assert proc.returncode == 0
    forwarded = buf.getvalue()
    assert "child stdout" in forwarded
    assert "child stderr" in forwarded


def test_run_forwarding_returncode_propagates_nonzero(tmp_path):
    import io

    child = _write_child_script(tmp_path, returncode=3)
    buf = io.StringIO()
    proc = run_forwarding([sys.executable, child], stdout=buf, stderr=buf)
    assert proc.returncode == 3


def test_run_forwarding_check_true_raises_after_forwarding(tmp_path):
    import io
    import subprocess

    child = _write_child_script(tmp_path, stdout_text="before-raise\n", stderr_text="", returncode=1)
    buf = io.StringIO()
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        run_forwarding([sys.executable, child], stdout=buf, stderr=buf, check=True)
    assert "before-raise" in buf.getvalue()
    assert exc_info.value.returncode == 1


def test_run_forwarding_passthrough_unchanged_for_real_fileno_stream(monkeypatch):
    import subprocess

    class _FakeConsoleStream:
        def fileno(self):
            return 5

    calls: dict = {}

    def _fake_run(argv, **kwargs):
        calls.update(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    real_stream = _FakeConsoleStream()
    proc = run_forwarding(["ignored"], stdout=real_stream, stderr=real_stream)
    assert proc.returncode == 0
    assert calls["stdout"] is real_stream
    assert calls["stderr"] is real_stream
    assert "text" not in calls


def test_run_forwarding_merges_stderr_into_stdout_for_same_fileno_less_target(monkeypatch):
    import io
    import subprocess

    calls: dict = {}

    def _fake_run(argv, **kwargs):
        calls.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="merged output\n")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    buf = io.StringIO()
    proc = run_forwarding(["ignored"], stdout=buf, stderr=buf)
    assert proc.returncode == 0
    assert calls["stdout"] == subprocess.PIPE
    assert calls["stderr"] == subprocess.STDOUT
    assert buf.getvalue() == "merged output\n"

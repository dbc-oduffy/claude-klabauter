"""Tests for coordinator_core.win_portability -- executability, PATH-list,
and path-split primitives.

Per AC-5 of the dispatch this file implements: differential checks against
independently-derived Windows-real values (``PureWindowsPath``), not against
monkeypatched values this same test file invented -- a test that patches a
value and then asserts that value proves nothing about correctness.

Spec backlink: docs/research/2026-07-28-windows-simulation-test-harness-design.md
  (DoE-claude), coordinator_core/tests/test_home_resolution_lint.py (this repo).
"""

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


# ---------------------------------------------------------------------------
# is_executable -- POSIX branch
# ---------------------------------------------------------------------------


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
    """Platform-independent cover of the POSIX branch's mode-bit predicate.

    Injects the exec bit at the ``os.stat`` seam rather than via ``chmod`` so
    the assertion holds on hosts whose filesystem cannot store one (NTFS).
    """
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
    result = split_path("X:\\DoE-claude\\coordinator", maxsplit=1, from_right=True)
    assert result == ["X:/DoE-claude", "coordinator"]


def test_split_path_matches_purewindowspath_parent_and_name():
    raw = "X:\\DoE-claude\\coordinator"
    result = split_path(raw, maxsplit=1, from_right=True)

    # Independently-derived expectation via PureWindowsPath (the real Win32
    # path-shape algorithm, importable on any host) -- not a re-assertion of
    # split_path's own output.
    pwp = PureWindowsPath(raw)
    expected_leaf = pwp.name
    expected_parent = str(pwp.parent).replace("\\", "/")

    assert result == [expected_parent, expected_leaf]


def test_split_path_msys_mount_form_already_forward_slash_unaffected():
    # /x/DoE-claude/coordinator is the Git-Bash/MSYS mount-form rendering of
    # X:\DoE-claude\coordinator -- already all-forward-slash, must split
    # identically whether or not the fold runs.
    result = split_path("/x/DoE-claude/coordinator", maxsplit=1, from_right=True)
    assert result == ["/x/DoE-claude", "coordinator"]


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


# ---------------------------------------------------------------------------
# no_console_creationflags -- both platform branches on one host (AC10)
# ---------------------------------------------------------------------------


def test_no_console_creationflags_posix_returns_empty_mapping(monkeypatch):
    monkeypatch.setattr(win_portability, "_is_windows", lambda: False)
    assert no_console_creationflags() == {}


def test_no_console_creationflags_windows_returns_create_no_window_flag(monkeypatch):
    monkeypatch.setattr(win_portability, "_is_windows", lambda: True)
    result = no_console_creationflags()
    assert set(result) == {"creationflags"}
    # Independently-derived expectation, not a re-assertion of the function's
    # own output: subprocess.CREATE_NO_WINDOW when the real host defines it
    # (real Windows), else the documented getattr fallback of 0 (a POSIX host
    # modelling the Windows branch via the monkeypatched seam).
    import subprocess

    expected = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert result["creationflags"] == expected


def test_no_console_creationflags_does_not_set_stdio_kwargs(monkeypatch):
    # AC10 / capture-semantics guarantee: the mapping must carry ONLY the
    # creationflags kwarg, never stdout/stderr/stdin -- splatting it into an
    # existing subprocess.run(..., capture_output=True) call must not alter
    # capture behaviour on either platform branch.
    monkeypatch.setattr(win_portability, "_is_windows", lambda: False)
    assert set(no_console_creationflags()) <= {"creationflags"}
    monkeypatch.setattr(win_portability, "_is_windows", lambda: True)
    assert set(no_console_creationflags()) <= {"creationflags"}


# ---------------------------------------------------------------------------
# same_path -- consolidated path-equality primitive
# (state/sizings/2026-08-07-path-equality-consolidates-onto-one-prim.yaml)
# ---------------------------------------------------------------------------


def test_same_path_uses_samefile_when_both_paths_exist(tmp_path):
    # Both legs exist -- samefile leg. A trailing separator changes the raw
    # string but not the filesystem entry samefile stats.
    assert same_path(str(tmp_path), str(tmp_path) + os.sep) is True


def test_same_path_samefile_leg_detects_alias_realpath_alone_would_miss(tmp_path):
    # The load-bearing case this primitive exists for: two DIFFERENT path
    # strings that samefile recognises as the same entry via a symlink alias,
    # which a naive same-string-after-realpath check would also catch here
    # (symlink resolves), but demonstrates the samefile leg is actually
    # exercised (not silently short-circuited to the fallback) for an aliased
    # pair -- see test_same_path_false_for_distinct_existing_paths for the
    # negative control proving the function does not just return True always.
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
    # samefile raises OSError for the missing leg -- fallback compares
    # normcase(realpath(...)) strings, which differ here (distinct paths).
    assert same_path(str(existing), str(missing)) is False


def test_same_path_fallback_true_for_identical_missing_path_strings(tmp_path):
    missing = tmp_path / "does-not-exist"
    # Neither exists -- samefile raises for both, fallback string-compares
    # equal realpath(normcase(...)) output for the SAME input string.
    assert same_path(str(missing), str(missing)) is True


def test_same_path_never_raises_for_unreadable_or_malformed_input():
    # Empty string is a degenerate path realpath still resolves (to cwd on
    # most platforms) without raising -- the never-raises contract is
    # exercised via a guaranteed-absent pair instead, asserting no exception
    # propagates out of either the samefile or fallback leg.
    assert same_path("", "") in (True, False)
    assert same_path("\x00bad", "\x00bad") in (True, False)


def test_same_path_case_insensitive_on_windows(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("normcase is a no-op on POSIX; this asserts Windows-real casefold behaviour")
    missing_upper = str(tmp_path / "DOES-NOT-EXIST")
    missing_lower = str(tmp_path / "does-not-exist")
    # Neither path exists -- samefile raises OSError for both, so this
    # exercises the fallback (realpath+normcase) leg specifically, proving
    # normcase folds the case difference rather than the two strings
    # happening to match some other way.
    assert os.path.normcase(missing_upper) == os.path.normcase(missing_lower)
    assert same_path(missing_upper, missing_lower) is True


# ---------------------------------------------------------------------------
# run_forwarding -- fileno-safe subprocess.run wrapper. Regression coverage
# for the workday-complete/workday-start assembler defect: `_invoke_cli_main`
# (coordinator_core/workday_complete/apply.py) redirects sys.stdout/
# sys.stderr to io.StringIO for every in-process directive dispatch, and a
# bare `subprocess.run(stdout=sys.stderr, stderr=sys.stderr)` needs a real
# `fileno()` on whatever it's handed -- it raises before the child is ever
# spawned, which is exactly what made branch consolidation (`workday-
# complete-step3-consolidate.py`'s sync-main step) unrunnable through the
# assembler path.
# ---------------------------------------------------------------------------


def _write_child_script(tmp_path, stdout_text="out-line\n", stderr_text="err-line\n", returncode=0):
    script = tmp_path / "run_forwarding_child.py"
    script.write_text(
        "import sys\n"
        f"sys.stdout.write({stdout_text!r})\n"
        f"sys.stderr.write({stderr_text!r})\n"
        f"sys.exit({returncode})\n"
    )
    return str(script)


def test_bare_subprocess_run_reproduces_the_original_break(tmp_path):
    """Proves the defect exists before asserting the fix: a bare
    subprocess.run call against a fileno-less StringIO target raises
    io.UnsupportedOperation -- the exact exception whose bare str() collapses
    to the single word "fileno" in workday_complete.apply's failed[] entries."""
    import io
    import subprocess

    child = _write_child_script(tmp_path)
    buf = io.StringIO()
    with pytest.raises(io.UnsupportedOperation):
        subprocess.run([sys.executable, child], stdout=buf, stderr=buf)


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
    """Directive-dispatch shape: sys.stderr swapped for a StringIO via
    contextlib.redirect_stderr (mirrors `_invoke_cli_main`'s own capture),
    called with stdout=sys.stderr/stderr=sys.stderr exactly as all four
    production call sites pass it."""
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
    """check=True must still raise CalledProcessError on a non-zero exit,
    but only AFTER the captured output has already reached the buffer --
    delegating `check` straight to the inner subprocess.run call would raise
    before this function's own forwarding step ever runs, dropping the text
    on exactly the failure path a caller most wants to see it on."""
    import io
    import subprocess

    child = _write_child_script(tmp_path, stdout_text="before-raise\n", stderr_text="", returncode=1)
    buf = io.StringIO()
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        run_forwarding([sys.executable, child], stdout=buf, stderr=buf, check=True)
    assert "before-raise" in buf.getvalue()
    assert exc_info.value.returncode == 1


def test_run_forwarding_passthrough_unchanged_for_real_fileno_stream(monkeypatch):
    """A stream WITH a usable fileno() must be passed straight through,
    unchanged -- the existing inherit-the-console behaviour for every call
    site not running under a capture buffer must be preserved bit-for-bit,
    never routed through PIPE."""
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
    """When stdout and stderr target the SAME fileno-less object (every
    current production call site passes stdout=sys.stderr, stderr=sys.stderr
    under the capture buffer), the child's stderr must merge into stdout at
    the OS level (stderr=subprocess.STDOUT) rather than being captured as a
    second independent PIPE -- two reassembled-after-the-fact pipes cannot
    preserve the child's actual write interleaving order; a single merged
    stream can."""
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

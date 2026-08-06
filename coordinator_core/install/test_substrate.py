"""Unit tests for coordinator_core.install.substrate helpers other than the
manifest loader (see coordinator_core/tests/test_setup_template_manifest.py
for _load_setup_template_manifest's own dedicated coverage).

Review: code-reviewer (Lane B install F2) — the former _parse_bash_string_array
bash-array parser this file used to cover was deleted outright in the
b644d5a9 executable-surface relocation: the manifest it parsed
(setup-templates-manifest.sh) no longer exists anywhere — it was replaced by
a plain Python module (setup-templates-manifest.py) loaded natively via
importlib, with no bash-array grammar left to parse. There is no successor
parser in this file to test; the loader-level tests live alongside the
manifest's own coverage in test_setup_template_manifest.py.
"""
from __future__ import annotations

import ntpath
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from coordinator_core.install import _shared
from coordinator_core.install import substrate
from coordinator_core.install.substrate import (
    SubstrateFatalError,
    _AGENT_FORWARDER_MARKER,
    _c10a_steps,
    _fnm_step,
    _percolation_and_path_steps,
    _prepare_rendered_python3_cmd,
    _prune_orphaned_static_bin_names,
    _refuse_machine_mutation,
    _render_python3_cmd,
    _resolve_baked_python_bin,
    _sweep_orphaned_agent_helpers,
    _windows_health_steps,
    _write_agent_cmd_forwarder,
    _write_bin_manifest,
)


# --- _resolve_baked_python_bin / _render_python3_cmd ------------------------
#
# Regression coverage for a swallowed-error report against install-substrate's
# python3.cmd baked-interpreter step: resolve_python_bin() raising (e.g. a
# pinned interpreter that fails validation) was being absorbed by a bare
# `except Exception: python3_cmd_resolved_bin = ""` with no diagnostic
# surfaced anywhere. "" itself is a documented, valid substitution (the
# template's own runtime fallback to `py -3`), so the fix is not to reject
# "" -- it's to stop discarding the *error* that produced it.


def test_resolve_baked_python_bin_returns_resolved_bin_on_success(monkeypatch):
    import coordinator_core.pyresolve as pyresolve

    # Pinned to the Windows host explicitly: the only artifacts this value
    # reaches are `.cmd` files, so baking is a Windows-host-only behavior and
    # an unpinned test would assert the opposite result on a macOS runner.
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(pyresolve, "resolve_python_bin", lambda **_: ("/usr/bin/python3", []))
    assert _resolve_baked_python_bin() == "/usr/bin/python3"


@pytest.mark.parametrize("host_os_name", ["posix", "java"])
def test_resolve_baked_python_bin_bakes_nothing_off_windows(monkeypatch, host_os_name):
    # A `.cmd` is inert on macOS/Linux, so anything resolvable HERE is an
    # absolute path belonging to the platform that will never execute the
    # file. This is the root of the ~290 macOS-path launchers that reached a
    # Windows box through a synced ~/.claude: nothing correct can be baked
    # from the non-Windows side, so nothing is.
    import coordinator_core.pyresolve as pyresolve

    monkeypatch.setattr(os, "name", host_os_name)
    monkeypatch.setattr(
        pyresolve,
        "resolve_python_bin",
        lambda **_: ("/Users/example-operator/.coordinator-claude-settings/.coordinator-venv/bin/python", []),
    )
    assert _resolve_baked_python_bin() == ""


def test_resolve_baked_python_bin_off_windows_does_not_probe_the_resolver(monkeypatch, capsys):
    # The gate is a hard precondition, not a post-filter -- an off-Windows host
    # must not pay for (or fail on) interpreter resolution it can never use.
    # Asserted via a call-recording spy rather than a raising stub: the
    # function's `except Exception` would absorb a raise into the same ""
    # return and the test could not tell the two apart.
    import coordinator_core.pyresolve as pyresolve

    calls = []

    def _spy(**_):
        calls.append(1)
        return ("/opt/homebrew/bin/python3", [])

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(pyresolve, "resolve_python_bin", _spy)

    assert _resolve_baked_python_bin() == ""
    assert calls == [], "resolve_python_bin must not be probed off Windows"
    assert capsys.readouterr().err == ""


def test_resolve_baked_python_bin_returns_empty_for_launcher_names(monkeypatch):
    import coordinator_core.pyresolve as pyresolve

    monkeypatch.setattr(pyresolve, "resolve_python_bin", lambda **_: ("py", []))
    assert _resolve_baked_python_bin() == ""


def test_resolve_baked_python_bin_returns_empty_when_nothing_found(monkeypatch):
    import coordinator_core.pyresolve as pyresolve

    monkeypatch.setattr(pyresolve, "resolve_python_bin", lambda **_: ("", []))
    assert _resolve_baked_python_bin() == ""


def test_resolve_baked_python_bin_surfaces_resolution_error(monkeypatch, capsys):
    import coordinator_core.pyresolve as pyresolve

    def _raise(**_):
        raise pyresolve.PythonPinInvalid("pinned interpreter is invalid; rebuild the venv")

    monkeypatch.setattr(pyresolve, "resolve_python_bin", _raise)
    # Without this the `os.name != "nt"` early-return in
    # `_resolve_baked_python_bin` short-circuits before the resolver is ever
    # called, so `result == ""` passes vacuously and the warning this test
    # exists to pin is never emitted -- green on Windows, unreachable
    # everywhere else.
    monkeypatch.setattr(os, "name", "nt")

    # Falls back to "" (still functional -- the template's runtime `py -3`
    # branch covers it) but the failure must not vanish silently.
    result = _resolve_baked_python_bin()
    assert result == ""
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "pinned interpreter is invalid" in err


# --- _resolve_baked_python_bin -- console-vs-windowless bake safety ---------
#
# Regression coverage for the pythonw.exe-baked-as-general-shim defect
# (cross-repo/inbox/2026-07-28-example-retrieval-repo-ue-addon-em-install-substrate-
# persistent-path-write-and-pythonw-shim.md § 2): pythonw.exe is
# /SUBSYSTEM:WINDOWS -- a general-purpose python3.cmd shim baked with it gave
# a caller with a live stdin pipe a null std handle, observed as a silent
# permanent hang on a real box. `_resolve_baked_python_bin` must (a) request
# the console interpreter from the resolver, and (b) reject a windowless
# result outright as defense-in-depth against a future resolver regression.


# Positive-assertion note (all five tests below): production
# (`_console_sibling` / `_resolve_baked_python_bin`, see substrate.py's own
# docstrings there) deliberately parses these Windows-shaped strings with the
# `ntpath` module directly rather than the ambient `os.path` -- `os.path` stays
# bound to `posixpath` on a POSIX test host no matter what `os.name` is
# monkeypatched to, per `coordinator_core/win_portability.py`'s precedent. That
# means swapping the ambient `os.path` for `ntpath` in THESE tests (the pattern
# `write_guards/tests/test_windows_platform_simulation.py`'s `_windows_os_path`
# fixture uses for code that legitimately relies on ambient `os.path`) would be
# the wrong fix here: it would make a regression BACK to ambient `os.path` in
# production invisible again, since the test's own `os.path` would then also
# behave like `ntpath` regardless of what production calls. Instead each test
# below records every path `os.path.isfile` is probed with and asserts the
# probe includes the exact sibling path real `ntpath`-based splitting produces
# (computed here via the `ntpath` module directly, not a hand-derived guess) --
# a regression to `posixpath`-based splitting mis-splits the backslash path,
# never probes that candidate, and this assertion fails loudly instead of the
# final return value coincidentally matching anyway.


def test_resolve_baked_python_bin_requests_console_interpreter(monkeypatch):
    import coordinator_core.pyresolve as pyresolve

    captured = {}

    def _fake_resolve(**kwargs):
        captured.update(kwargs)
        return "/usr/bin/python3", []

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(pyresolve, "resolve_python_bin", _fake_resolve)
    assert _resolve_baked_python_bin() == "/usr/bin/python3"
    # `captured` is only populated if `resolve_python_bin` was actually
    # invoked, which only happens once the `os.name != "nt"` early return has
    # been cleared -- a direct proof the Windows branch ran, not just that the
    # final return value happens to match.
    assert captured == {"prefer_windowless": False}


def test_resolve_baked_python_bin_never_returns_a_windowless_path(monkeypatch):
    import coordinator_core.pyresolve as pyresolve

    monkeypatch.setattr(os, "name", "nt")
    windowless = r"C:\Program Files\Python313\pythonw.exe"
    monkeypatch.setattr(pyresolve, "resolve_python_bin", lambda **_: (windowless, []))
    isfile_probes = []
    monkeypatch.setattr(
        os.path, "isfile", lambda p: isfile_probes.append(p) or False  # no sibling on disk
    )
    assert _resolve_baked_python_bin() == ""
    expected_probe = ntpath.join(ntpath.dirname(windowless), "python.exe")
    assert expected_probe in isfile_probes, (
        f"expected a console-sibling probe at {expected_probe!r} (real ntpath "
        f"splitting of {windowless!r}); got {isfile_probes!r} instead -- the "
        "Windows path-splitting branch did not actually run"
    )


def test_resolve_baked_python_bin_prefers_console_sibling_when_present(monkeypatch):
    import coordinator_core.pyresolve as pyresolve

    monkeypatch.setattr(os, "name", "nt")
    windowless = r"C:\Program Files\Python313\pythonw.exe"
    console_sibling = ntpath.join(ntpath.dirname(windowless), "python.exe")
    monkeypatch.setattr(pyresolve, "resolve_python_bin", lambda **_: (windowless, []))
    isfile_probes = []
    monkeypatch.setattr(
        os.path, "isfile", lambda p: isfile_probes.append(p) or p == console_sibling
    )
    assert _resolve_baked_python_bin() == console_sibling
    # A posixpath-based mis-split would never probe the correctly-joined
    # sibling at all -- this is the load-bearing check, not the return value
    # above (a broken split could otherwise still coincidentally return "").
    assert console_sibling in isfile_probes


def test_resolve_baked_python_bin_pyw_falls_back_to_py_console_sibling(monkeypatch):
    import coordinator_core.pyresolve as pyresolve

    monkeypatch.setattr(os, "name", "nt")
    windowless = r"C:\Program Files\Python313\pyw.exe"
    console_sibling = ntpath.join(ntpath.dirname(windowless), "py.exe")
    monkeypatch.setattr(pyresolve, "resolve_python_bin", lambda **_: (windowless, []))
    isfile_probes = []
    monkeypatch.setattr(
        os.path, "isfile", lambda p: isfile_probes.append(p) or p == console_sibling
    )
    assert _resolve_baked_python_bin() == console_sibling
    assert console_sibling in isfile_probes


def test_resolve_baked_python_bin_windowless_with_no_console_sibling_warns_and_falls_back(
    monkeypatch, capsys
):
    import coordinator_core.pyresolve as pyresolve

    monkeypatch.setattr(os, "name", "nt")
    windowless = r"C:\Program Files\Python313\pythonw.exe"
    monkeypatch.setattr(pyresolve, "resolve_python_bin", lambda **_: (windowless, []))
    isfile_probes = []
    monkeypatch.setattr(os.path, "isfile", lambda p: isfile_probes.append(p) or False)

    result = _resolve_baked_python_bin()
    assert result == ""
    expected_probe = ntpath.join(ntpath.dirname(windowless), "python.exe")
    assert expected_probe in isfile_probes, (
        f"expected a console-sibling probe at {expected_probe!r}; got "
        f"{isfile_probes!r} -- the Windows path-splitting branch did not run"
    )
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "windowless" in err


def test_render_python3_cmd_substitutes_resolved_bin():
    rendered = _render_python3_cmd('set "_bin=__PYTHON_BIN__"\n', "/usr/bin/python3")
    assert rendered == 'set "_bin=/usr/bin/python3"\n\n'


def test_render_python3_cmd_empty_bin_is_a_valid_substitution():
    # "" is the template's own documented "nothing baked, use `py -3` at
    # runtime" contract -- not a defect, so it must render cleanly rather
    # than raise.
    rendered = _render_python3_cmd('set "_bin=__PYTHON_BIN__"\n', "")
    assert rendered == 'set "_bin="\n\n'


def test_render_python3_cmd_none_bin_raises_instead_of_writing_corrupt_wrapper():
    # A None resolved_bin is a caller-contract violation, not a valid "nothing
    # to bake" state -- must fail loudly rather than stringify to the literal
    # "None" inside the emitted .cmd wrapper.
    with pytest.raises(SubstrateFatalError, match="resolved_bin=None"):
        _render_python3_cmd('set "_bin=__PYTHON_BIN__"\n', None)


# --- _prepare_rendered_python3_cmd -- check-only staleness comparison (2026-08-05
# bug-blitz Defect A) and CRLF-on-write (Defect B) -----------------------------
#
# Regression coverage for install-maximalist.py --check-only reporting
# python3.cmd permanently stale on a correctly-installed machine. Root cause:
# `run()` used to build the rendered temp file only `if not check_only`, so
# the check-only comparison ran `filecmp.cmp` against the RAW template (still
# carrying the literal `__PYTHON_BIN__` token and LF line endings from
# `read_text()`'s universal-newlines normalization) instead of the rendered,
# token-substituted, CRLF-restored content a real install writes. That
# comparison could never pass, on any machine, regardless of actual drift.


def test_prepare_rendered_python3_cmd_substitutes_token(tmp_path):
    ml_bin = tmp_path
    (ml_bin / "python3.cmd").write_text(
        'set "_coordinator_python3_bin=__PYTHON_BIN__"\r\n', encoding="utf-8"
    )
    rendered_path = _prepare_rendered_python3_cmd(ml_bin, "/usr/bin/python3")
    try:
        content = rendered_path.read_bytes()
        assert b"__PYTHON_BIN__" not in content
        assert b"/usr/bin/python3" in content
    finally:
        rendered_path.unlink()


def test_prepare_rendered_python3_cmd_writes_crlf_regardless_of_host_linesep(tmp_path):
    # Template on disk uses CRLF (its native, tracked form); read_text()'s
    # universal-newlines mode normalizes that down to bare "\n" on ANY host,
    # POSIX included. A naive write_text() re-emits only os.linesep, which is
    # "\n" on macOS/Linux -- corrupting a Windows batch file's line endings
    # even when the eventual consumer is a Windows machine reading a synced
    # settings-home. This must hold on every host this suite runs on, not
    # just Windows -- that is precisely the bug.
    ml_bin = tmp_path
    (ml_bin / "python3.cmd").write_text(
        'set "_coordinator_python3_bin=__PYTHON_BIN__"\r\npy -3 %*\r\n', encoding="utf-8"
    )
    rendered_path = _prepare_rendered_python3_cmd(ml_bin, "")
    try:
        raw = rendered_path.read_bytes()
        assert b"\r\n" in raw
        # No bare LF unaccompanied by a preceding CR anywhere in the output.
        assert b"\n" not in raw.replace(b"\r\n", b"")
    finally:
        rendered_path.unlink()


def test_check_only_compares_rendered_content_not_raw_template(tmp_path, monkeypatch):
    # Direct regression for Defect A: a destination that already holds
    # exactly what a real (check_only=False) install would write must be
    # reported up to date under check_only=True -- not "stale forever"
    # because the comparison secretly ran against the unrendered template.
    monkeypatch.setattr(substrate, "_resolve_baked_python_bin", lambda: "")
    ml_bin = tmp_path / "ml_bin"
    ml_bin.mkdir()
    (ml_bin / "python3.cmd").write_text(
        'set "_coordinator_python3_bin=__PYTHON_BIN__"\r\npy -3 %*\r\n', encoding="utf-8"
    )
    dst = tmp_path / "installed" / "python3.cmd"
    dst.parent.mkdir()

    resolved_bin = substrate._resolve_baked_python_bin()
    rendered_path = _prepare_rendered_python3_cmd(ml_bin, resolved_bin)
    try:
        dst.write_bytes(rendered_path.read_bytes())

        # Re-render (mirrors what `run()` does on the next invocation, cold)
        # and compare via the SAME check-only path `_install_one` uses.
        recheck_path = _prepare_rendered_python3_cmd(ml_bin, resolved_bin)
        try:
            substrate._install_one(
                recheck_path, dst, False, "machine-local", check_only=True
            )
        finally:
            recheck_path.unlink()
    finally:
        rendered_path.unlink()


def test_check_only_still_catches_genuine_drift(tmp_path, monkeypatch):
    # The fix must not degrade into a comparison that stops noticing real
    # drift -- an installed file that does NOT match the rendered content
    # (e.g. the pre-existing LF-only corruption from Defect B) must still be
    # reported stale.
    monkeypatch.setattr(substrate, "_resolve_baked_python_bin", lambda: "")
    ml_bin = tmp_path / "ml_bin"
    ml_bin.mkdir()
    (ml_bin / "python3.cmd").write_text(
        'set "_coordinator_python3_bin=__PYTHON_BIN__"\r\npy -3 %*\r\n', encoding="utf-8"
    )
    dst = tmp_path / "installed" / "python3.cmd"
    dst.parent.mkdir()
    # Simulate an LF-only corrupted prior install (Defect B's symptom).
    dst.write_text('set "_coordinator_python3_bin="\npy -3 %*\n', encoding="utf-8")

    rendered_path = _prepare_rendered_python3_cmd(ml_bin, "")
    try:
        with pytest.raises(SubstrateFatalError, match="is stale at"):
            substrate._install_one(
                rendered_path, dst, False, "machine-local", check_only=True
            )
    finally:
        rendered_path.unlink()


# --- python3.cmd runtime shim -- baked-path-missing deadlock (F7) -----------
#
# The exist-check the shim's own `if not "..."=="" if exist "..." (` guard
# adds lives in example-doctrine-repo's templates/bin/python3.cmd batch body, not in any
# Python here — `_render_python3_cmd` only substitutes the token, it never
# executes the result. These two tests invoke the REAL rendered .cmd via
# cmd.exe to exercise that batch-level branch directly, on the real example-doctrine-repo
# template (not a hand-copied fixture, so a future template edit that drops
# the exist-check trips this test rather than silently reverting the fix).
#
# Regression: before the exist-check, a baked path that named the coordinator
# venv's own now-deleted interpreter deadlocked ensure-coordinator-venv --
# this shim is used to CREATE that venv, so once the venv was gone every
# invocation through it failed and the venv could never be rebuilt.


def _real_python3_cmd_template() -> str:
    try:
        coordinator_root = _shared.resolve_coordinator_root()
    except RuntimeError as exc:
        pytest.skip(f"example-doctrine-repo coordinator root unresolvable on this machine: {exc}")
    template_path = Path(coordinator_root) / "templates" / "bin" / "python3.cmd"
    if not template_path.is_file():
        pytest.skip(f"no templates/bin/python3.cmd under resolved coordinator root: {template_path}")
    return template_path.read_text(encoding="utf-8")


def _run_rendered_shim(tmp_path: Path, resolved_bin: str) -> subprocess.CompletedProcess:
    template_content = _real_python3_cmd_template()
    rendered = _render_python3_cmd(template_content, resolved_bin)
    shim_path = tmp_path / "python3.cmd"
    shim_path.write_text(rendered, encoding="utf-8")
    return subprocess.run(
        [str(shim_path), "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.mark.real_home
@pytest.mark.skipif(sys.platform != "win32", reason="python3.cmd is a Windows-only shim")
def test_python3_cmd_shim_falls_back_to_py_launcher_when_baked_path_missing(tmp_path):
    missing_bin = str(tmp_path / "coordinator-venv-gone" / "Scripts" / "python.exe")
    assert not Path(missing_bin).exists()

    proc = _run_rendered_shim(tmp_path, missing_bin)

    assert proc.returncode == 0, proc.stderr
    assert "Python" in proc.stdout


@pytest.mark.real_home
@pytest.mark.skipif(sys.platform != "win32", reason="python3.cmd is a Windows-only shim")
def test_python3_cmd_shim_prefers_baked_path_when_present(tmp_path):
    # sys.executable is guaranteed to exist and answer --version -- stands in
    # for "the interpreter resolved and baked in at install time" without
    # depending on any particular install layout.
    baked_bin = sys.executable

    proc = _run_rendered_shim(tmp_path, baked_bin)

    expected = subprocess.run(
        [baked_bin, "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected.stdout.strip()


# --- agent-helper .cmd forwarder -- self-healing baked interpreter ----------
#
# `_write_agent_cmd_forwarder` bakes the install-time interpreter path into
# every generated `.cmd` launcher as a fast path. The baked rung must fall
# through when the path is empty OR absent from disk -- a WRONG bake was
# previously a permanent hard rc=3 ("The system cannot find the path
# specified") with no self-heal, because only an EMPTY bake reached the
# `where python.exe` / `py -3` rungs.
#
# This is not hypothetical: a `~/.claude` synced between a Mac and a Windows
# box carries macOS interpreter paths in launchers Windows executes (and vice
# versa). Neither sweeping nor regenerating can be correct on both machines at
# once -- falling back on non-existence is the repair that is right on
# whichever platform is actually running.
#
# These tests execute the REAL generated batch body under cmd.exe rather than
# asserting on its text: the branch under test is batch-level (`if not
# ""=="" if exist ""`), and only cmd.exe's own parser can confirm the quoting
# survives paths with spaces and POSIX separators.


_FORWARDER_TOKEN = "forwarder-ran-ok"


def _render_forwarder_pair(tmp_path: Path, baked_bin: str) -> Path:
    """Write a `<name>` Unix-half stub (a real Python script) plus the
    generated `<name>.cmd` half beside it, and return the `.cmd` path."""
    name = "coordinator-fake-cli"
    unix_half = tmp_path / name
    unix_half.write_text(
        f"import sys\nprint({_FORWARDER_TOKEN!r})\nprint(' '.join(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    cmd_half = tmp_path / f"{name}.cmd"
    _write_agent_cmd_forwarder(
        name, cmd_half, False, python3_cmd_resolved_bin=baked_bin
    )
    return cmd_half


def _run_forwarder(cmd_half: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(cmd_half), *args],
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.mark.skipif(sys.platform != "win32", reason=".cmd forwarders are Windows-only")
def test_agent_cmd_forwarder_uses_baked_interpreter_when_it_exists(tmp_path):
    # sys.executable is guaranteed to exist -- stands in for a healthy bake.
    proc = _run_forwarder(_render_forwarder_pair(tmp_path, sys.executable), "hello")

    assert proc.returncode == 0, proc.stderr
    assert _FORWARDER_TOKEN in proc.stdout
    assert "hello" in proc.stdout


@pytest.mark.skipif(sys.platform != "win32", reason=".cmd forwarders are Windows-only")
def test_agent_cmd_forwarder_falls_through_on_empty_bake(tmp_path):
    # "" is the documented "nothing resolvable at install time" value -- the
    # `where python.exe` / `py -3` rungs must carry it.
    proc = _run_forwarder(_render_forwarder_pair(tmp_path, ""), "hello")

    assert proc.returncode == 0, proc.stderr
    assert _FORWARDER_TOKEN in proc.stdout


@pytest.mark.skipif(sys.platform != "win32", reason=".cmd forwarders are Windows-only")
def test_agent_cmd_forwarder_falls_through_on_foreign_platform_bake(tmp_path):
    # The synced-~/.claude case verbatim: a macOS venv interpreter path baked
    # into a launcher running on Windows. Pre-fix this was rc=3 forever.
    macos_bake = "/Users/example-operator/.coordinator-claude-settings/.coordinator-venv/bin/python"
    assert not Path(macos_bake).exists()

    proc = _run_forwarder(_render_forwarder_pair(tmp_path, macos_bake), "hello")

    assert proc.returncode == 0, proc.stderr
    assert _FORWARDER_TOKEN in proc.stdout


@pytest.mark.skipif(sys.platform != "win32", reason=".cmd forwarders are Windows-only")
def test_agent_cmd_forwarder_falls_through_on_missing_bake_with_spaces(tmp_path):
    # Quoting check with teeth: an unquoted `if exist %_py%` would make
    # cmd.exe parse this as multiple tokens rather than falling through
    # cleanly, so a green here is evidence the guard quotes its operand.
    missing_bake = str(tmp_path / "Program Files" / "gone" / "python.exe")
    assert not Path(missing_bake).exists()

    proc = _run_forwarder(_render_forwarder_pair(tmp_path, missing_bake), "hello")

    assert proc.returncode == 0, proc.stderr
    assert _FORWARDER_TOKEN in proc.stdout


# --- _refuse_machine_mutation / _windows_health_steps guard -----------------
#
# Regression coverage for a 2026-07-28 incident: a pytest `tmp_path` fixture
# rooted under the system temp dir (a sandboxed fake `$HOME`) was written into
# a REAL operator's `HKCU\Environment` user PATH by `_windows_health_steps`'s
# then-unguarded `[Environment]::SetEnvironmentVariable` call. The test
# sandbox redirected the filesystem but not this machine-state side effect --
# `_refuse_machine_mutation` is the fix, and the suite-wide
# `COORDINATOR_DISABLE_MACHINE_MUTATION=1` set by
# `coordinator_core/conftest.py::_quarantine_real_home` is the belt-and-braces
# backstop these tests would otherwise be masked by, so each test that
# exercises the path heuristic explicitly clears the env var first.

# A fake path that is deliberately NOT under the system temp dir. Built as a
# literal string (never touched on disk, never created) rather than derived
# from `tmp_path` -- `tmp_path` (and the suite's own HOME-quarantine dir) live
# under the system temp dir, so a fixture-derived path cannot stand in for
# "a genuine install location" here.
_FAKE_REAL_INSTALL_PATH = (
    r"C:\fake-operator-profile\.coordinator-claude-settings\bin"
    if os.name == "nt"
    else "/fake-operator-profile/.coordinator-claude-settings/bin"
)


def test_refuse_machine_mutation_blocks_temp_rooted_path(monkeypatch, tmp_path):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)

    reason = _refuse_machine_mutation(str(tmp_path / "settings-home" / "bin"), what="test mutation")

    assert reason is not None
    assert "temp" in reason


def test_refuse_machine_mutation_allows_non_temp_non_disabled_path(monkeypatch):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)

    reason = _refuse_machine_mutation(_FAKE_REAL_INSTALL_PATH, what="test mutation")

    assert reason is None


def test_refuse_machine_mutation_env_opt_out_blocks_every_path(monkeypatch):
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")

    reason = _refuse_machine_mutation(_FAKE_REAL_INSTALL_PATH, what="test mutation")

    assert reason is not None
    assert "COORDINATOR_DISABLE_MACHINE_MUTATION" in reason


def _fake_powershell_for_windows_health_steps(calls, already_response="no"):
    def fake(command, env=None):
        calls.append(command)
        # The PATH-membership read that gates the mutation attempt -- see
        # `_windows_health_steps`'s `$t=$env:BIN_DST_WIN` check.
        if "$t=$env:BIN_DST_WIN" in command:
            return already_response
        # Every other call (AppX stub probe, python/py resolution) answers
        # "nothing found", which only produces harmless advisory prints.
        return ""

    return fake


def test_windows_health_steps_refuses_temp_rooted_bin_dst(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    bin_dst = tmp_path / "t6a_home" / ".coordinator-claude-settings" / "bin"

    calls: list = []
    monkeypatch.setattr(substrate, "_cygpath_w", lambda p: p)
    monkeypatch.setattr(substrate, "_powershell", _fake_powershell_for_windows_health_steps(calls))

    _windows_health_steps(bin_dst, check_only=False)

    assert not any("SetEnvironmentVariable" in c for c in calls), (
        "a temp-rooted bin_dst must never reach the real PATH-mutation call"
    )
    assert "REFUSED" in capsys.readouterr().err


def test_windows_health_steps_respects_disable_env_even_for_non_temp_path(monkeypatch, capsys):
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    bin_dst = Path(_FAKE_REAL_INSTALL_PATH)

    calls: list = []
    monkeypatch.setattr(substrate, "_cygpath_w", lambda p: p)
    monkeypatch.setattr(substrate, "_powershell", _fake_powershell_for_windows_health_steps(calls))

    _windows_health_steps(bin_dst, check_only=False)

    assert not any("SetEnvironmentVariable" in c for c in calls)
    assert "REFUSED" in capsys.readouterr().err


def test_windows_health_steps_mutates_for_a_genuine_non_temp_path(monkeypatch, capsys):
    # Positive control: with the belt-and-braces opt-out cleared and a path
    # that is NOT temp-rooted, the mutation must actually proceed -- proving
    # the guard blocks only the sandbox case, not every install.
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    bin_dst = Path(_FAKE_REAL_INSTALL_PATH)

    calls: list = []
    monkeypatch.setattr(substrate, "_cygpath_w", lambda p: p)
    monkeypatch.setattr(substrate, "_powershell", _fake_powershell_for_windows_health_steps(calls))

    _windows_health_steps(bin_dst, check_only=False)

    assert any("SetEnvironmentVariable" in c for c in calls)
    assert "REFUSED" not in capsys.readouterr().err


# --- _windows_health_steps: orphan AppX stub deletion gate -------------------
#
# Follow-up to the coverage-extension pass below (Coordinator ruling on
# state/bug-backlog/2026-08-06-coordinator-disable-machine-mutation-cov-
# 70b1bc2d3e77.yaml): a consent prompt is a different axis from the kill
# switch -- an operator who sets COORDINATOR_DISABLE_MACHINE_MUTATION=1 and is
# then prompted to delete a real $LOCALAPPDATA file has been told the switch
# protects them and it doesn't. The guard is checked BEFORE the consent
# prompt fires, so the disabled case must never reach `input()` either.

_FAKE_APPX_STUB_PATH = (  # abs-path-ok: synthetic fixture string, never touches real disk
    r"C:\Users\fake-operator\AppData\Local\Microsoft\WindowsApps\python3.exe"
)


def _fake_powershell_for_appx_stub(calls, already_response="yes"):
    def fake(command, env=None):
        calls.append(command)
        if "$t=$env:BIN_DST_WIN" in command:
            return already_response
        if "WindowsApps\\python3.exe" in command and "ReparsePoint" in command:
            return _FAKE_APPX_STUB_PATH
        # python.exe probe (the other stub_name in the loop) and the
        # store-alias-on-PATH warning that follows -- answer "nothing found".
        return ""

    return fake


def test_windows_health_steps_appx_stub_disabled_never_prompts_or_deletes(monkeypatch, capsys):
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    bin_dst = Path(_FAKE_REAL_INSTALL_PATH)

    calls: list = []
    monkeypatch.setattr(substrate, "_cygpath_w", lambda p: p)
    monkeypatch.setattr(substrate, "_powershell", _fake_powershell_for_appx_stub(calls))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("COORDINATOR_NON_INTERACTIVE", raising=False)

    def _unexpected_input(prompt=""):
        raise AssertionError("consent prompt must never fire when the guard refuses first")

    monkeypatch.setattr("builtins.input", _unexpected_input)

    _windows_health_steps(bin_dst, check_only=False)

    assert not any("Remove-Item" in c for c in calls), (
        "disabled mutation must not delete the orphan AppX stub"
    )
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "COORDINATOR_DISABLE_MACHINE_MUTATION" in err


def test_windows_health_steps_appx_stub_enabled_prompt_still_deletes(monkeypatch, capsys):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    bin_dst = Path(_FAKE_REAL_INSTALL_PATH)

    calls: list = []
    monkeypatch.setattr(substrate, "_cygpath_w", lambda p: p)
    monkeypatch.setattr(substrate, "_powershell", _fake_powershell_for_appx_stub(calls))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("COORDINATOR_NON_INTERACTIVE", raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    _windows_health_steps(bin_dst, check_only=False)

    assert any("Remove-Item" in c for c in calls), (
        "with the guard unset, consenting to the prompt must still delete the stub"
    )
    assert "REFUSED" not in capsys.readouterr().err


# --- COORDINATOR_DISABLE_MACHINE_MUTATION coverage extension ----------------
#
# Regression coverage for state/bug-backlog/2026-08-06-coordinator-disable-
# machine-mutation-cov-70b1bc2d3e77.yaml and the sibling
# 2026-08-06-fnm-step-pipes-curl-into-bash-with-no-ma-e73ec94bb998.yaml:
# `_refuse_machine_mutation` used to gate only the two Windows PATH writes.
# Every site below now shares that same guard. Each pair proves the guard is
# a GATE, not a disable: the disabled case asserts on the absence of the
# underlying mutation (no unlink/write/subprocess spawn), never merely on a
# printed message; the unset case asserts the mutation still fires.


def test_sweep_orphaned_agent_helpers_disabled_does_not_unlink(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    dst_dir = tmp_path / "bin"
    dst_dir.mkdir()
    orphan = dst_dir / "retired-helper"
    orphan.write_text(f"#!/usr/bin/env python3\n{_AGENT_FORWARDER_MARKER}\n", encoding="utf-8")

    _sweep_orphaned_agent_helpers(dst_dir, {}, {}, check_only=False)

    assert orphan.exists(), "disabled mutation must not delete the orphan forwarder"
    assert "REFUSED" in capsys.readouterr().err


def test_sweep_orphaned_agent_helpers_enabled_unlinks(monkeypatch, tmp_path):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    # tmp_path itself lives under the system temp dir, which would otherwise
    # trip _refuse_machine_mutation's OWN sandbox heuristic -- redirect that
    # heuristic's notion of "temp" away from tmp_path so this positive
    # control isolates the env-var trigger being tested, not the path one.
    monkeypatch.setattr(substrate.tempfile, "gettempdir", lambda: str(tmp_path / "_unrelated-temp-root"))
    dst_dir = tmp_path / "bin"
    dst_dir.mkdir()
    orphan = dst_dir / "retired-helper"
    orphan.write_text(f"#!/usr/bin/env python3\n{_AGENT_FORWARDER_MARKER}\n", encoding="utf-8")

    _sweep_orphaned_agent_helpers(dst_dir, {}, {}, check_only=False)

    assert not orphan.exists(), "with the guard unset the orphan sweep must still delete"


def test_prune_orphaned_static_bin_names_disabled_does_not_unlink(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    stale = bin_dst / "retired-tool"
    stale.write_text("stale", encoding="utf-8")
    _write_bin_manifest(bin_dst, frozenset({"retired-tool"}))

    _prune_orphaned_static_bin_names(bin_dst, frozenset(), check_only=False)

    assert stale.exists(), "disabled mutation must not prune the manifest-recorded file"
    assert "REFUSED" in capsys.readouterr().err


def test_prune_orphaned_static_bin_names_enabled_unlinks(monkeypatch, tmp_path):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    monkeypatch.setattr(substrate.tempfile, "gettempdir", lambda: str(tmp_path / "_unrelated-temp-root"))
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    stale = bin_dst / "retired-tool"
    stale.write_text("stale", encoding="utf-8")
    _write_bin_manifest(bin_dst, frozenset({"retired-tool"}))

    _prune_orphaned_static_bin_names(bin_dst, frozenset(), check_only=False)

    assert not stale.exists(), "with the guard unset the prune must still delete"


def test_percolation_and_path_steps_disabled_skips_rc_block_writes(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    install_base = tmp_path / "home"
    install_base.mkdir()
    bin_dst = install_base / ".coordinator-claude-settings" / "bin"
    bin_dst.mkdir(parents=True)
    setup_src = tmp_path / "setup-src"
    setup_src.mkdir()

    calls: list = []
    monkeypatch.setattr(
        substrate,
        "write_path_entry_guard_blocks",
        lambda **kwargs: calls.append(kwargs) or {"modified": False, "already_present": True, "results": {}},
    )

    _percolation_and_path_steps(
        setup_src, [], [], [], str(install_base), bin_dst, check_only=False,
    )

    assert calls == [], "disabled mutation must never reach write_path_entry_guard_blocks"
    assert "REFUSED" in capsys.readouterr().err


def test_percolation_and_path_steps_enabled_writes_rc_block(monkeypatch, tmp_path):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    monkeypatch.setattr(substrate.tempfile, "gettempdir", lambda: str(tmp_path / "_unrelated-temp-root"))
    install_base = tmp_path / "home"
    install_base.mkdir()
    bin_dst = install_base / ".coordinator-claude-settings" / "bin"
    bin_dst.mkdir(parents=True)
    setup_src = tmp_path / "setup-src"
    setup_src.mkdir()

    calls: list = []
    monkeypatch.setattr(
        substrate,
        "write_path_entry_guard_blocks",
        lambda **kwargs: calls.append(kwargs) or {"modified": False, "already_present": True, "results": {}},
    )

    _percolation_and_path_steps(
        setup_src, [], [], [], str(install_base), bin_dst, check_only=False,
    )

    assert len(calls) == 1, "with the guard unset the settings-home bin PATH block write must still run"
    assert calls[0]["sentinel_id"] == "SETTINGS_HOME_BIN"


def test_c10a_steps_disabled_does_not_replace_legacy_whoami(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    install_base = tmp_path / "home"
    settings_home_path = tmp_path / "settings-home"
    plugin_root = tmp_path / "plugin-root"
    bin_dst = settings_home_path / "bin"
    for d in (install_base, settings_home_path, plugin_root, bin_dst):
        d.mkdir(parents=True)

    legacy_whoami = install_base / ".claude" / "coordinator-whoami"
    legacy_whoami.mkdir(parents=True)
    (legacy_whoami / "marker").write_text("real dir", encoding="utf-8")

    dst_whoami = settings_home_path / "coordinator-whoami"
    dst_whoami.mkdir(parents=True)
    (dst_whoami / "marker").write_text("already relocated", encoding="utf-8")

    rc = _c10a_steps(str(install_base), settings_home_path, plugin_root, bin_dst, check_only=False)

    assert rc == 0
    assert legacy_whoami.is_dir() and not legacy_whoami.is_symlink(), (
        "disabled mutation must not replace the legacy real dir with a pointer"
    )
    assert "REFUSED" in capsys.readouterr().err


def test_c10a_steps_enabled_replaces_legacy_whoami(monkeypatch, tmp_path):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    monkeypatch.setattr(substrate.tempfile, "gettempdir", lambda: str(tmp_path / "_unrelated-temp-root"))
    install_base = tmp_path / "home"
    settings_home_path = tmp_path / "settings-home"
    plugin_root = tmp_path / "plugin-root"
    bin_dst = settings_home_path / "bin"
    for d in (install_base, settings_home_path, plugin_root, bin_dst):
        d.mkdir(parents=True)

    legacy_whoami = install_base / ".claude" / "coordinator-whoami"
    legacy_whoami.mkdir(parents=True)
    (legacy_whoami / "marker").write_text("real dir", encoding="utf-8")

    dst_whoami = settings_home_path / "coordinator-whoami"
    dst_whoami.mkdir(parents=True)
    (dst_whoami / "marker").write_text("already relocated", encoding="utf-8")

    rc = _c10a_steps(str(install_base), settings_home_path, plugin_root, bin_dst, check_only=False)

    assert rc == 0
    assert legacy_whoami.is_symlink(), (
        "with the guard unset the legacy real dir must still be replaced by a compat pointer"
    )


def test_c10a_steps_disabled_does_not_remove_legacy_venv(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    install_base = tmp_path / "home"
    settings_home_path = tmp_path / "settings-home"
    plugin_root = tmp_path / "plugin-root"
    bin_dst = settings_home_path / "bin"
    for d in (install_base, settings_home_path, plugin_root, bin_dst):
        d.mkdir(parents=True)

    # dst_whoami already populated (skips relocation) and legacy_whoami absent
    # (skips the pointer-replace leg above) so only the legacy-venv leg fires.
    dst_whoami = settings_home_path / "coordinator-whoami"
    dst_whoami.mkdir(parents=True)
    (dst_whoami / "marker").write_text("already relocated", encoding="utf-8")
    (dst_whoami / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    legacy_venv = install_base / ".claude" / ".coordinator-venv"
    legacy_venv.mkdir(parents=True)
    (legacy_venv / "marker").write_text("legacy venv", encoding="utf-8")

    from coordinator_core.install import ensure_venv as _ensure_venv_module

    monkeypatch.setattr(
        _ensure_venv_module, "ensure_coordinator_venv",
        lambda *a, **k: "healthy (stub)",
    )
    monkeypatch.setattr(_ensure_venv_module, "_venv_healthy", lambda *a, **k: True)

    rc = _c10a_steps(str(install_base), settings_home_path, plugin_root, bin_dst, check_only=False)

    assert rc == 0
    assert legacy_venv.is_dir(), "disabled mutation must not remove the legacy venv"
    assert "REFUSED" in capsys.readouterr().err


def test_c10a_steps_enabled_removes_legacy_venv(monkeypatch, tmp_path):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    monkeypatch.setattr(substrate.tempfile, "gettempdir", lambda: str(tmp_path / "_unrelated-temp-root"))
    install_base = tmp_path / "home"
    settings_home_path = tmp_path / "settings-home"
    plugin_root = tmp_path / "plugin-root"
    bin_dst = settings_home_path / "bin"
    for d in (install_base, settings_home_path, plugin_root, bin_dst):
        d.mkdir(parents=True)

    dst_whoami = settings_home_path / "coordinator-whoami"
    dst_whoami.mkdir(parents=True)
    (dst_whoami / "marker").write_text("already relocated", encoding="utf-8")
    (dst_whoami / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    legacy_venv = install_base / ".claude" / ".coordinator-venv"
    legacy_venv.mkdir(parents=True)
    (legacy_venv / "marker").write_text("legacy venv", encoding="utf-8")

    from coordinator_core.install import ensure_venv as _ensure_venv_module

    monkeypatch.setattr(
        _ensure_venv_module, "ensure_coordinator_venv",
        lambda *a, **k: "healthy (stub)",
    )
    monkeypatch.setattr(_ensure_venv_module, "_venv_healthy", lambda *a, **k: True)

    rc = _c10a_steps(str(install_base), settings_home_path, plugin_root, bin_dst, check_only=False)

    assert rc == 0
    assert not legacy_venv.is_dir(), (
        "with the guard unset the legacy venv removal must still run"
    )


def test_fnm_step_disabled_does_not_spawn_brew_or_curl(monkeypatch, capsys):
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    monkeypatch.setattr(
        substrate.shutil, "which",
        lambda name: None if name == "fnm" else ("/usr/bin/brew" if name == "brew" else None),
    )

    calls: list = []
    monkeypatch.setattr(substrate, "_run", lambda *a, **k: calls.append(a) or None)
    monkeypatch.setattr(
        substrate.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess.run must not be spawned")),
    )

    _fnm_step(check_only=False)

    assert calls == [], "disabled mutation must never spawn brew/curl"
    assert "REFUSED" in capsys.readouterr().err


def test_fnm_step_enabled_installs_via_brew(monkeypatch):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    monkeypatch.setattr(
        substrate.shutil, "which",
        lambda name: None if name == "fnm" else ("/usr/bin/brew" if name == "brew" else None),
    )

    calls: list = []

    class _FakeCompleted:
        returncode = 0

    monkeypatch.setattr(
        substrate, "_run",
        lambda argv, **k: calls.append(argv) or _FakeCompleted(),
    )

    _fnm_step(check_only=False)

    assert calls == [["brew", "install", "fnm"]], (
        "with the guard unset the brew install must still run"
    )

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

import json
import ntpath
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from coordinator_core.install import _shared
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.install import substrate
from coordinator_core.install.policy_gate import PolicyGateVerdict
from coordinator_core.install.substrate import (
    SubstrateFatalError,
    _AGENT_FORWARDER_MARKER,
    _AGENT_PS1_FORWARDER_MARKER,
    _LEGACY_CMD_MARKER,
    _PS1_POLICY_STATUS_FILENAME,
    _RAW_CMDLINE_TARGETS,
    _agent_cmd_raw_cmdline_block,
    _agent_ps1_dest_name,
    _c10a_steps,
    _emit_and_verify_ps1_forwarders,
    _handle_ps1_gate_verdict,
    _ps1_policy_repair_message,
    _ps1_policy_status_path,
    _report_ps1_policy_gate_skip,
    _write_ps1_policy_status,
    _fnm_step,
    _percolation_and_path_steps,
    _prune_orphaned_static_bin_names,
    _refuse_machine_mutation,
    _resolve_agent_cmd_dest_collisions,
    _resolve_baked_python_bin,
    _static_bin_family_names,
    _sweep_orphaned_agent_helpers,
    _windows_health_steps,
    _write_agent_cmd_forwarder,
    _write_agent_ps1_forwarder,
    _write_bin_manifest,
)

# Real cmd.exe/subprocess spawns are load-bearing: the .cmd-forwarder tests
# assert on batch-level quoting/parsing behaviour (`if not ""=="" if exist
# ""`) that only cmd.exe's own parser can confirm -- a text-only assertion on
# the generated body cannot prove the quoting survives paths with spaces.
# Windows-only (skipif not win32); per-test isolation via tmp_path is already
# module-scope-safe since each test renders its own forwarder pair.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


# --- _resolve_baked_python_bin -----------------------------------------------
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
        name, cmd_half, False, python3_cmd_resolved_bin=baked_bin, target=f"{name}.py"
    )
    return cmd_half


def _run_forwarder(cmd_half: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(cmd_half), *args],
        capture_output=True,
        text=True,
        timeout=60,
        **no_console_creationflags(),
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


# --- _RAW_CMDLINE_TARGETS coverage -- scoped-git-commit / cross-repo-memo ----
#
# cross-repo/inbox/2026-08-07-example-doctrine-repo-em-cmd-forwarder-drops-everything-
# after-a-newline.md: `%*`-populated batch parameters silently lose everything
# after a literal newline in an argument (a `.cmd` forwarder parse-time
# defect, not a caller-side quoting bug -- see `_agent_cmd_raw_cmdline_block`'s
# docstring). `coordinator-write-review-trail.py` was the only target opted
# into the `%CMDCMDLINE%`-capture workaround; `scoped-git-commit` and
# `cross-repo-memo` take multi-line arguments as a matter of course (commit
# messages, memo bodies) and were silently NOT covered.
#
# These first two tests are platform-portable: they assert on the GENERATED
# TEXT of the raw-cmdline capture block and the full `.cmd` body, not on
# runtime behavior -- the actual newline-survives-the-round-trip behavior is
# only observable by running a real `.cmd` file under `cmd.exe`, which this
# environment (macOS) cannot do. The third test below is the Windows-only
# runtime proof and is skipped here; it is included for the Windows box that
# runs this suite.


def test_raw_cmdline_targets_cover_scoped_git_commit_and_cross_repo_memo():
    assert "scoped-git-commit" in _RAW_CMDLINE_TARGETS
    assert "cross-repo-memo" in _RAW_CMDLINE_TARGETS
    # Regression guard (AC1): the original sole member must still be present.
    assert "coordinator-write-review-trail.py" in _RAW_CMDLINE_TARGETS


@pytest.mark.parametrize("target", ["scoped-git-commit", "cross-repo-memo"])
def test_agent_cmd_raw_cmdline_block_emits_capture_for_newly_covered_targets(target):
    block = _agent_cmd_raw_cmdline_block(target)

    assert block != ""
    assert "_LAUNCHER_RAW_CMDLINE_FILE" in block
    assert "%CMDCMDLINE%" in block


def test_agent_cmd_raw_cmdline_block_stays_empty_for_uncovered_target():
    # AC1: a target NOT in `_RAW_CMDLINE_TARGETS` renders byte-identical to
    # before this mechanism existed -- the gate must stay closed by default.
    assert _agent_cmd_raw_cmdline_block("some-other-cli") == ""


@pytest.mark.parametrize("target", ["scoped-git-commit", "cross-repo-memo"])
def test_agent_cmd_forwarder_body_includes_raw_cmdline_capture_for_target(
    tmp_path, target
):
    dst = tmp_path / f"{target}.cmd"
    _write_agent_cmd_forwarder(
        target, dst, False, python3_cmd_resolved_bin="", target=target
    )
    body = dst.read_text(encoding="utf-8")

    assert "_LAUNCHER_RAW_CMDLINE_FILE" in body
    assert "%CMDCMDLINE%" in body


def test_agent_cmd_forwarder_body_omits_raw_cmdline_capture_for_uncovered_target(
    tmp_path,
):
    dst = tmp_path / "some-other-cli.cmd"
    _write_agent_cmd_forwarder(
        "some-other-cli", dst, False, python3_cmd_resolved_bin="", target="some-other-cli"
    )
    body = dst.read_text(encoding="utf-8")

    assert "_LAUNCHER_RAW_CMDLINE_FILE" not in body
    assert "%CMDCMDLINE%" not in body


@pytest.mark.skipif(sys.platform != "win32", reason=".cmd forwarders are Windows-only")
def test_agent_cmd_forwarder_raw_cmdline_survives_embedded_newline(tmp_path):
    # Windows-only runtime proof: the Unix-half stub below reads the raw
    # cmdline capture file the .cmd half writes and echoes it back, so a
    # newline embedded in the invocation (the exact defect this mechanism
    # exists to work around) surviving into that echoed text is direct
    # evidence the capture mechanism engages for a newly-covered target, not
    # just that the generated text contains the right tokens.
    name = "scoped-git-commit"
    unix_half = tmp_path / name
    unix_half.write_text(
        "import os\n"
        "p = os.environ.get('_LAUNCHER_RAW_CMDLINE_FILE', '')\n"
        "print('CAPTURED:' + (open(p, encoding='utf-8').read() if p else ''))\n",
        encoding="utf-8",
    )
    cmd_half = tmp_path / f"{name}.cmd"
    _write_agent_cmd_forwarder(
        name, cmd_half, False, python3_cmd_resolved_bin=sys.executable, target=name
    )

    proc = subprocess.run(
        [str(cmd_half), "line-one^\nline-two"],
        capture_output=True,
        text=True,
        timeout=60,
        shell=False,
        **no_console_creationflags(),
    )

    assert proc.returncode == 0, proc.stderr
    assert "CAPTURED:" in proc.stdout
    assert "line-two" in proc.stdout


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


# --- .ps1 launcher class -- orphan sweep (C1 of the ps1-launcher-class plan) -
#
# `_sweep_orphaned_agent_helpers` used to treat `.ps1` as an ordinary
# extensionless file, requiring `_AGENT_FORWARDER_MARKER` -- a string no
# emitted `.ps1` body carries -- so a retired CLI's `.ps1` forwarder could
# never reach deletion. These tests cover the new `.ps1`-specific marker
# branch and its `protected_names` complement, both required together (see
# `_sweep_orphaned_agent_helpers`'s docstring, condition 1 vs condition 2).


def test_sweep_orphaned_agent_helpers_retires_both_legs_cmd_and_ps1(monkeypatch, tmp_path):
    """AC2: retiring a CLI (absent from this run's derived maps) must sweep
    BOTH launcher legs -- the pre-existing `.cmd` forwarder AND its `.ps1`
    sibling. The `.ps1` must not survive merely because it isn't the `.cmd`
    branch; before limb 1, landing emission first would leave a retired
    CLI's `.ps1` leg executable under PowerShell."""
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    monkeypatch.setattr(substrate.tempfile, "gettempdir", lambda: str(tmp_path / "_unrelated-temp-root"))
    name = "retired-cli"
    cmd_orphan = tmp_path / f"{name}.cmd"
    _write_agent_cmd_forwarder(
        name, cmd_orphan, False, python3_cmd_resolved_bin="", target=f"{name}.py"
    )
    ps1_orphan = tmp_path / f"{name}.ps1"
    ps1_orphan.write_text(
        f"# {_AGENT_PS1_FORWARDER_MARKER}\n& python3 '{name}.py' @args\n",
        encoding="utf-8",
    )

    _sweep_orphaned_agent_helpers(tmp_path, {}, {}, False)

    assert not cmd_orphan.exists(), "orphaned .cmd leg must be swept"
    assert not ps1_orphan.exists(), "orphaned .ps1 leg must be swept alongside its .cmd sibling"


def test_sweep_does_not_remove_ps1_orphan_lacking_the_marker(tmp_path):
    """The `.ps1` branch is content-gated by the same positive-marker
    discipline as the `.cmd` and extensionless branches, not name-gated -- a
    hand-authored or foreign-tool `.ps1` sharing an orphaned name must
    survive."""
    decoy = tmp_path / "retired-cli.ps1"
    decoy.write_text("# hand-authored, not ours\n", encoding="utf-8")

    _sweep_orphaned_agent_helpers(tmp_path, {}, {}, False)

    assert decoy.is_file()


def test_sweep_static_family_ps1_with_legacy_marker_survives(tmp_path):
    """AC2b regression: `_LEGACY_CMD_MARKER` is stamped by
    `coordinator/bin/gen-launcher-shim.py::render_ps1` into source-side
    `.ps1` files for every CLI it renders, including static-family ones --
    it is NOT exclusive to agent-helper forwarders (mirroring the existing
    `.cmd` hazard). `_AGENT_PS1_FORWARDER_MARKER` was deliberately chosen to
    be distinct from `_LEGACY_CMD_MARKER` and the `.ps1` branch does not
    accept the legacy marker at all -- a static-family `.ps1` must survive.

    The real manifest carries `platform-localize.cmd` (and `.py`), NOT
    `platform-localize.ps1` -- verified against the live, un-monkeypatched
    `_static_bin_family_names()` at HEAD (18 names; `.cmd`/`.py` present,
    `.ps1` absent). So survival does NOT come from direct `.ps1` membership
    in that set -- it comes from `_sweep_orphaned_agent_helpers`'s limb 2,
    which synthesizes `Path(n).stem + ".ps1"` for every protected name
    (including `platform-localize.cmd`) before checking `protected_names`.
    A monkeypatch that injects `platform-localize.ps1` directly into
    `_static_bin_family_names()`'s return value tests membership-protection
    only -- never in doubt -- and stays green even if limb 2 is deleted,
    which defeats the regression this test exists to be. Using the REAL,
    unpatched function is what makes the assertion fail if limb 2 goes."""
    assert "platform-localize.cmd" in _static_bin_family_names()
    assert "platform-localize.ps1" not in _static_bin_family_names()
    protected = tmp_path / "platform-localize.ps1"
    protected.write_text(f"# {_LEGACY_CMD_MARKER}\n$null = 1\n", encoding="utf-8")

    _sweep_orphaned_agent_helpers(tmp_path, {}, {}, False)

    assert protected.is_file()


def test_sweep_protects_ps1_sibling_of_currently_installed_name(tmp_path):
    """Limb 2: a `.ps1` sibling of a name in THIS run's derived maps must
    survive even without the ps1 marker at all -- `protected_names`
    extends to the `.ps1` form of every protected bare name. This is a
    complement to the marker branch, not a substitute for it: protection
    alone would make `.ps1` orphans unsweepable, which is why limb 1's
    marker branch is what makes deletion reachable in the first place."""
    name = "cross-repo-memo"
    kept_ps1 = tmp_path / f"{name}.ps1"
    kept_ps1.write_text("# no marker at all\n", encoding="utf-8")

    _sweep_orphaned_agent_helpers(tmp_path, {name: f"{name}.py"}, {}, False)

    assert kept_ps1.is_file()


def test_sweep_protects_ps1_derived_via_limb_two_not_marker_or_static_family(monkeypatch, tmp_path):
    """Review: code-reviewer (P1, coordinatorcode-reviewer-2032cde5) -- limb 2
    (`protected_names |= {Path(n).stem + ".ps1" for n in protected_names}`)
    is checked BEFORE the marker branch in the sweep loop, so a fixture must
    be deletion-ELIGIBLE under limb 1 (carries `_AGENT_PS1_FORWARDER_MARKER`)
    while its bare name is protected ONLY via limb 2's derivation -- never
    directly as a `.ps1` entry in `_static_bin_family_names()` or the maps.
    `foo-cli.cmd` is the protected name (via `agent_cmd_dest_map.values()`);
    `foo-cli.ps1` is not itself a member of any protected set, and
    `foo-cli.ps1` is not in `_static_bin_family_names()`. Deleting limb 2
    would drop `foo-cli.ps1` from `protected_names` entirely, and since it
    carries the marker it would then be unlinked -- this test fails without
    limb 2 present."""
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    monkeypatch.setattr(substrate.tempfile, "gettempdir", lambda: str(tmp_path / "_unrelated-temp-root"))
    assert "foo-cli.ps1" not in _static_bin_family_names()
    ps1 = tmp_path / "foo-cli.ps1"
    ps1.write_text(f"# {_AGENT_PS1_FORWARDER_MARKER}\n$null = 1\n", encoding="utf-8")

    _sweep_orphaned_agent_helpers(tmp_path, {}, {"foo-cli": "foo-cli.cmd"}, False)

    assert ps1.is_file(), (
        "foo-cli.ps1 is protected only via limb 2's derivation from the "
        "protected foo-cli.cmd name -- surviving here is evidence limb 2 ran"
    )


def test_sweep_ps1_legacy_marker_alone_grants_no_deletion_eligibility(monkeypatch, tmp_path):
    """Review: code-reviewer (P3, coordinatorcode-reviewer-2032cde5) -- the
    `.ps1` branch's marker check does NOT accept `_LEGACY_CMD_MARKER` (only
    `_AGENT_PS1_FORWARDER_MARKER`), so a `.ps1` file carrying only the legacy
    marker, whose bare name is UNPROTECTED (not in
    `_static_bin_family_names()`, not derivable via limb 2 from any name in
    this run's maps), must still survive -- proving the `.ps1` branch's
    deletion-eligibility gate (`_AGENT_PS1_FORWARDER_MARKER not in text`)
    never treats `_LEGACY_CMD_MARKER` as sufficient. This is the negative
    control the `..._legacy_marker_survives` test above cannot provide on
    its own, since that test's survival comes from static-family membership
    and never reaches this branch's marker check at all -- it cannot tell
    "protected" apart from "would also survive a legacy-marker allowance had
    one existed." Here protection is deliberately absent, so survival is
    proof positive the marker check itself is what's gating deletion, not
    incidental protected-name membership. A regression that widened the
    `.ps1` branch to accept `_LEGACY_CMD_MARKER` (mirroring the `.cmd`
    branch's `or` today) would make this fixture deletion-eligible and,
    being unprotected, actually deleted -- flipping this assertion."""
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    monkeypatch.setattr(substrate.tempfile, "gettempdir", lambda: str(tmp_path / "_unrelated-temp-root"))
    name = "not-a-real-static-family-member"
    assert f"{name}.cmd" not in _static_bin_family_names()
    assert f"{name}.py" not in _static_bin_family_names()
    decoy = tmp_path / f"{name}.ps1"
    decoy.write_text(f"# {_LEGACY_CMD_MARKER}\n$null = 1\n", encoding="utf-8")

    _sweep_orphaned_agent_helpers(tmp_path, {}, {}, False)

    assert decoy.is_file(), (
        "an unprotected legacy-marker-only .ps1 must survive -- the .ps1 "
        "branch's marker check grants no legacy-marker deletion-eligibility"
    )


def test_static_bin_family_names_marker_constant_is_module_sourced():
    """AC12: `_AGENT_PS1_FORWARDER_MARKER` is a real module-level constant
    (not a literal string improvised in this test file) and is distinct
    from every other marker -- the property the sweep's docstring claims
    for the whole marker family."""
    assert _AGENT_PS1_FORWARDER_MARKER
    assert _AGENT_PS1_FORWARDER_MARKER != _LEGACY_CMD_MARKER
    assert _AGENT_PS1_FORWARDER_MARKER not in _LEGACY_CMD_MARKER
    assert _LEGACY_CMD_MARKER not in _AGENT_PS1_FORWARDER_MARKER
    assert _AGENT_PS1_FORWARDER_MARKER != _AGENT_FORWARDER_MARKER
    assert _AGENT_PS1_FORWARDER_MARKER != substrate._AGENT_CMD_FORWARDER_MARKER


# --- .ps1 launcher class -- emission, post-emission verification, and
# rollback-on-red (C3 of the ps1-launcher-class plan) ------------------------
#
# `_emit_and_verify_ps1_forwarders` is the only function in this module that
# EMITS `.ps1` launchers. Every test below fakes the policy-gate seam
# (`substrate.evaluate_policy_gate`) rather than depending on this box's real
# execution policy or a real PowerShell being present -- the gate's own
# soundness is C2's coverage (test_policy_gate.py); these tests cover what
# the INSTALLER does with a given verdict.


def _fake_green_verdict(*, reason: str = "fake green") -> PolicyGateVerdict:
    return PolicyGateVerdict(green=True, host_verdicts=[], reason=reason)


def _fake_red_verdict(*, reason: str = "fake red") -> PolicyGateVerdict:
    return PolicyGateVerdict(green=False, host_verdicts=[], reason=reason)


def _detemper(monkeypatch, tmp_path):
    """`_refuse_machine_mutation`'s trigger 2 refuses any path resolving
    under the OS temp dir -- exactly where pytest's own `tmp_path` lives.
    Mirrors the existing sweep tests' fix: repoint `tempfile.gettempdir()`
    at an unrelated subdirectory so `tmp_path` reads as a genuine install
    location, not a test sandbox."""
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    monkeypatch.setattr(substrate.tempfile, "gettempdir", lambda: str(tmp_path / "_unrelated-temp-root"))


def test_write_agent_ps1_forwarder_body_carries_shared_marker_constant(tmp_path):
    """AC12: the emitted `.ps1` body contains `_AGENT_PS1_FORWARDER_MARKER`
    sourced from the shared constant, not a literal re-typed in this
    generator -- the actual C1<->C3 no-drift claim, exercised end to end."""
    dst = tmp_path / "some-cli.ps1"
    _write_agent_ps1_forwarder("some-cli", dst, False, python3_cmd_resolved_bin="")
    text = dst.read_text(encoding="utf-8")
    assert _AGENT_PS1_FORWARDER_MARKER in text


def test_agent_ps1_dest_name_derives_from_cmd_dest_stem():
    assert _agent_ps1_dest_name("foo.cmd") == "foo.ps1"
    # Extension-carrying installed name parity with `_agent_cmd_dest_name`
    # (AC7 parity fix on that function's own docstring) -- never a malformed
    # double-suffix `foo.sh.cmd.ps1`.
    assert _agent_ps1_dest_name("foo.sh.cmd") == "foo.sh.ps1"


def test_emit_ps1_forwarders_collision_winner_parity(monkeypatch, tmp_path):
    """AC1: `.ps1` emission is driven off the RESOLVED map
    (`_resolve_agent_cmd_dest_collisions`'s return value), never the raw
    unresolved target map -- both legs must resolve the SAME winner for a
    colliding name. `render-handoff-tracker` (non-`.js`) must win over
    `render-handoff-tracker.js`; the loser gets neither a `.cmd` nor a
    `.ps1`."""
    _detemper(monkeypatch, tmp_path)
    monkeypatch.setattr(substrate, "evaluate_policy_gate", _fake_green_verdict)
    monkeypatch.setattr(substrate, "_unblock_files", lambda paths: None)

    target_map = {
        "render-handoff-tracker": "render-handoff-tracker.py",
        "render-handoff-tracker.js": "render-handoff-tracker.js",
    }
    resolved_cmd_map = _resolve_agent_cmd_dest_collisions(target_map)
    assert resolved_cmd_map == {"render-handoff-tracker": "render-handoff-tracker.cmd"}

    verdict = _emit_and_verify_ps1_forwarders(
        tmp_path, resolved_cmd_map, False, python3_cmd_resolved_bin="",
    )

    assert verdict is not None and verdict.green
    assert (tmp_path / "render-handoff-tracker.ps1").exists()
    assert not (tmp_path / "render-handoff-tracker.js.ps1").exists()


def test_emit_ps1_forwarders_rollback_on_red_leaves_zero_ps1_behind(monkeypatch, tmp_path):
    """AC7: a forced-RED verdict rolls back EVERY `.ps1` this pass emitted --
    zero left behind, no half-emitted state."""
    _detemper(monkeypatch, tmp_path)
    monkeypatch.setattr(substrate, "evaluate_policy_gate", _fake_red_verdict)
    monkeypatch.setattr(substrate, "_unblock_files", lambda paths: None)

    resolved_cmd_map = {"foo-cli": "foo-cli.cmd", "bar-cli": "bar-cli.cmd"}
    verdict = _emit_and_verify_ps1_forwarders(
        tmp_path, resolved_cmd_map, False, python3_cmd_resolved_bin="",
    )

    assert verdict is not None and not verdict.green
    assert not (tmp_path / "foo-cli.ps1").exists()
    assert not (tmp_path / "bar-cli.ps1").exists()


def test_emit_ps1_forwarders_red_verdict_does_not_raise_install_still_succeeds(monkeypatch, tmp_path):
    """AC8: a policy-blocked (RED) host does not hard-fail the install --
    this function returns normally with the RED verdict rather than
    raising, so the caller (`_install_bin_resolvers`) never aborts the
    install chain on a RED `.ps1` gate; the `.cmd` leg (out of this
    function's write surface entirely) stays untouched and usable
    regardless."""
    _detemper(monkeypatch, tmp_path)
    monkeypatch.setattr(substrate, "evaluate_policy_gate", _fake_red_verdict)
    monkeypatch.setattr(substrate, "_unblock_files", lambda paths: None)

    verdict = _emit_and_verify_ps1_forwarders(
        tmp_path, {"foo-cli": "foo-cli.cmd"}, False, python3_cmd_resolved_bin="",
    )

    assert verdict is not None
    assert verdict.green is False
    assert verdict.reason


def test_emit_ps1_forwarders_consent_refused_skips_unblock_but_still_emits(monkeypatch, tmp_path):
    """AC14: a refused `COORDINATOR_DISABLE_MACHINE_MUTATION=1` consent
    SKIPS the `Unblock-File` call without failing emission -- a declined
    operator preference is not an error. The gate still runs and the
    `.ps1` still lands."""
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    called = []
    monkeypatch.setattr(substrate, "_unblock_files", lambda paths: called.append(list(paths)))
    monkeypatch.setattr(substrate, "evaluate_policy_gate", _fake_green_verdict)

    verdict = _emit_and_verify_ps1_forwarders(
        tmp_path, {"foo-cli": "foo-cli.cmd"}, False, python3_cmd_resolved_bin="",
    )

    assert verdict is not None and verdict.green
    assert (tmp_path / "foo-cli.ps1").exists()
    assert called == [], "consent-refused must skip the Unblock-File call entirely"


def test_emit_ps1_forwarders_unblock_target_set_equals_paths_written_this_pass(monkeypatch, tmp_path):
    """AC14: the unblock's target set is EXACTLY the paths this pass wrote --
    never a directory walk or a glob over the destination. A pre-existing
    `.ps1` this pass did NOT (re)write must never appear in the unblocked
    set -- the whole justification for the step is that it touches only
    our own seconds-old output."""
    _detemper(monkeypatch, tmp_path)
    captured = []
    monkeypatch.setattr(substrate, "_unblock_files", lambda paths: captured.append(list(paths)))
    monkeypatch.setattr(substrate, "evaluate_policy_gate", _fake_green_verdict)

    decoy = tmp_path / "decoy.ps1"
    decoy.write_text("pre-existing, not written this pass\n", encoding="utf-8")

    resolved_cmd_map = {"foo-cli": "foo-cli.cmd", "bar-cli": "bar-cli.cmd"}
    _emit_and_verify_ps1_forwarders(
        tmp_path, resolved_cmd_map, False, python3_cmd_resolved_bin="",
    )

    assert len(captured) == 1
    assert set(captured[0]) == {tmp_path / "foo-cli.ps1", tmp_path / "bar-cli.ps1"}


def test_emit_ps1_forwarders_check_only_never_writes_or_gates(monkeypatch, tmp_path):
    """`check_only` must neither emit nor invoke the gate -- `None` return
    (nothing to verify or roll back) and no `.ps1` on disk."""
    gate_called = []
    monkeypatch.setattr(substrate, "evaluate_policy_gate", lambda: gate_called.append(1) or _fake_green_verdict())
    monkeypatch.setattr(substrate, "_unblock_files", lambda paths: None)

    with pytest.raises(SubstrateFatalError):
        _emit_and_verify_ps1_forwarders(
            tmp_path, {"foo-cli": "foo-cli.cmd"}, True, python3_cmd_resolved_bin="",
        )

    assert gate_called == []
    assert not (tmp_path / "foo-cli.ps1").exists()


# --- C4: the loud skip, the repair entrypoint, and the durable surface -----
#
# `_handle_ps1_gate_verdict` is the single seam `_install_bin_resolvers`
# calls with the non-`None` `PolicyGateVerdict` `_emit_and_verify_ps1_
# forwarders` computed. Tested directly here (both branches) rather than by
# standing up `_install_bin_resolvers`'s full fixture set.


def test_ps1_policy_status_path_sits_beside_bin_not_inside_it(tmp_path):
    bin_dst = tmp_path / "settings-home" / "bin"
    status_path = _ps1_policy_status_path(bin_dst)
    assert status_path == bin_dst.parent / _PS1_POLICY_STATUS_FILENAME
    assert status_path.parent == bin_dst.parent


def test_ps1_policy_repair_message_names_python_and_extensionless_forwarder(tmp_path):
    """AC9: the fallback MUST invoke the extensionless forwarder via
    `python`, which preserves argv regardless of execution policy."""
    message = _ps1_policy_repair_message(tmp_path / "bin")
    assert message.startswith("python ")
    assert str(tmp_path / "bin") in message
    assert "execution policy" in message


def test_handle_ps1_gate_verdict_red_emits_reason_and_repair_entrypoint(tmp_path, capsys):
    """AC9: a RED verdict emits the reason, the failing host's reason text,
    and the repair entrypoint, all on stderr (loud)."""
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    verdict = PolicyGateVerdict(
        green=False, host_verdicts=[], reason="pwsh: effective policy 'Restricted' does not permit unsigned scripts",
    )

    _handle_ps1_gate_verdict(verdict, bin_dst)

    err = capsys.readouterr().err
    assert "SKIPPED" in err
    assert verdict.reason in err
    assert _ps1_policy_repair_message(bin_dst) in err
    assert "python " in err


def test_handle_ps1_gate_verdict_green_prints_nothing(tmp_path, capsys):
    """AC13's second half: the green path does not print the skip warning."""
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    verdict = _fake_green_verdict()

    _handle_ps1_gate_verdict(verdict, bin_dst)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_handle_ps1_gate_verdict_writes_durable_surface_on_red(tmp_path, capsys):
    """AC13: the skip reason and repair entrypoint are written to a durable,
    findable-later surface -- not just printed once at install time."""
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    verdict = _fake_red_verdict(reason="powershell: host not found on PATH")

    _handle_ps1_gate_verdict(verdict, bin_dst)

    status_path = _ps1_policy_status_path(bin_dst)
    assert status_path.exists()
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["green"] is False
    assert payload["reason"] == verdict.reason
    assert payload["repair_entrypoint"] == _ps1_policy_repair_message(bin_dst)


def test_handle_ps1_gate_verdict_writes_durable_surface_on_green_too(tmp_path):
    """The durable surface is not the AC9 stdout warning -- it is written
    on BOTH verdicts, because the population it serves (an operator whose
    host was GREEN at install time and only had policy tightened
    afterward) never sees an install-time warning to recall in the first
    place; only something already on disk can reach them."""
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    verdict = _fake_green_verdict()

    _handle_ps1_gate_verdict(verdict, bin_dst)

    payload = json.loads(_ps1_policy_status_path(bin_dst).read_text(encoding="utf-8"))
    assert payload["green"] is True
    assert payload["repair_entrypoint"] == _ps1_policy_repair_message(bin_dst)


def test_write_ps1_policy_status_survives_unwritable_path(tmp_path, capsys):
    """A failure to write the durable surface must not raise -- it is a
    best-effort record, not load-bearing for the install to succeed."""
    bin_dst = tmp_path / "does-not-exist" / "bin"

    _write_ps1_policy_status(bin_dst, _fake_red_verdict())

    assert "WARNING" in capsys.readouterr().err


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
    # `Path.is_symlink()` only detects the NTFS `IO_REPARSE_TAG_SYMLINK` tag
    # -- the Windows branch here creates a directory junction
    # (`IO_REPARSE_TAG_MOUNT_POINT` instead), which `is_symlink()` reports
    # as False even though it IS the compat pointer this asserts for. Use
    # this repo's own `is_pointer` (symlink-OR-junction) instead — see its
    # docstring for the identical idempotence bug this sidesteps.
    assert _shared.is_pointer(legacy_whoami), (
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

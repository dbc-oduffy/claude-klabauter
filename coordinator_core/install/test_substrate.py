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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from coordinator_core.install import _shared
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.install import substrate
from coordinator_core.install.substrate import (
    SubstrateFatalError,
    _AGENT_FORWARDER_MARKER,
    _AGENT_PS1_FORWARDER_MARKER,
    _LEGACY_CMD_MARKER,
    _RAW_CMDLINE_TARGETS,
    _agent_cmd_raw_cmdline_block,
    _c10a_steps,
    _derive_agent_helper_target_map,
    _fnm_step,
    _percolation_and_path_steps,
    _prune_orphaned_static_bin_names,
    _refuse_machine_mutation,
    _resolve_agent_cmd_dest_collisions,
    _resolve_baked_python_bin,
    resolve_hook_python_bin,
    _static_bin_family_names,
    _sweep_orphaned_agent_helpers,
    _windows_health_steps,
    _write_bin_manifest,
    run,
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
        lambda **_: ("/Users/alice/.coordinator-claude-settings/.coordinator-venv/bin/python", []),
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


# GRAVESTONE -- the agent-helper `.cmd` forwarder body suite (deleted
# 2026-08-29 with `_write_agent_cmd_forwarder` itself; PM ruling: one
# native entrypoint per platform, and that entrypoint is the door).
#
# Seven tests went, all of them executing a REAL generated batch body under
# cmd.exe: the self-healing baked-interpreter rungs (empty bake, foreign-
# platform bake from a Mac/Windows-synced `~/.claude`, on-disk-absent bake),
# the %LOCALAPPDATA% resolution cache rungs, the no-new-process guarantee
# for cache hits, and the raw-command-line capture including its embedded-
# newline case.
#
# THE SELF-HEAL THOSE TESTS PROTECTED IS NOT LOST -- IT IS UNREACHABLE.
# Every one of them existed because a `.cmd` had to rediscover a Python
# interpreter on each call, and could bake the wrong platform's path. The
# door image starts no interpreter to resolve, so the whole failure class
# (rc=3 'system cannot find the path specified' from a foreign bake) has no
# surface left. Deleting these is removing tests for a mechanism, not
# lowering a bar: what replaced the mechanism is asserted in
# `install/tests/test_forwarder_routes_through_door.py`.
#
# The one property here NOT inherited by the door is `%*` argument fidelity
# through cmd.exe's re-parse -- because the door never goes through cmd.exe.
# It reads its own command line via `GetCommandLineW`/`CommandLineToArgvW`
# (`door.c`), so quote-and-space payloads that the `.cmd` had to defend
# against are structurally not at risk.


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


def _fake_win_user_path_entries_for_windows_health_steps(already_present=False):
    # C11: replaces the old `_powershell` fake -- `_windows_health_steps` now
    # reads the user PATH via `_win_user_path_entries` instead of spawning
    # powershell.exe. `already_present` mirrors the old `already_response`
    # ("yes"/"no") knob.
    entries = [_FAKE_REAL_INSTALL_PATH] if already_present else []
    return lambda: (entries, ";".join(entries), 2)


def test_windows_health_steps_refuses_temp_rooted_bin_dst(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    bin_dst = tmp_path / "t6a_home" / ".coordinator-claude-settings" / "bin"

    calls: list = []
    monkeypatch.setattr(substrate, "_cygpath_w", lambda p: p)
    monkeypatch.setattr(substrate, "_win_user_path_entries", _fake_win_user_path_entries_for_windows_health_steps())
    monkeypatch.setattr(substrate, "_win_user_path_prepend", lambda *a, **k: calls.append("PREPEND") or True)
    monkeypatch.setattr(substrate, "_orphan_appx_stub", lambda path: False)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    _windows_health_steps(bin_dst, check_only=False)

    assert not calls, (
        "a temp-rooted bin_dst must never reach the real PATH-mutation call"
    )
    assert "REFUSED" in capsys.readouterr().err


def test_windows_health_steps_respects_disable_env_even_for_non_temp_path(monkeypatch, capsys):
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    bin_dst = Path(_FAKE_REAL_INSTALL_PATH)

    calls: list = []
    monkeypatch.setattr(substrate, "_cygpath_w", lambda p: p)
    monkeypatch.setattr(substrate, "_win_user_path_entries", _fake_win_user_path_entries_for_windows_health_steps())
    monkeypatch.setattr(substrate, "_win_user_path_prepend", lambda *a, **k: calls.append("PREPEND") or True)
    monkeypatch.setattr(substrate, "_orphan_appx_stub", lambda path: False)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    _windows_health_steps(bin_dst, check_only=False)

    assert not calls
    assert "REFUSED" in capsys.readouterr().err


def test_windows_health_steps_mutates_for_a_genuine_non_temp_path(monkeypatch, capsys):
    # Positive control: with the belt-and-braces opt-out cleared and a path
    # that is NOT temp-rooted, the mutation must actually proceed -- proving
    # the guard blocks only the sandbox case, not every install.
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    bin_dst = Path(_FAKE_REAL_INSTALL_PATH)

    calls: list = []
    monkeypatch.setattr(substrate, "_cygpath_w", lambda p: p)
    monkeypatch.setattr(substrate, "_win_user_path_entries", _fake_win_user_path_entries_for_windows_health_steps())
    monkeypatch.setattr(substrate, "_win_user_path_prepend", lambda *a, **k: calls.append("PREPEND") or True)
    monkeypatch.setattr(substrate, "_orphan_appx_stub", lambda path: False)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    _windows_health_steps(bin_dst, check_only=False)

    assert "PREPEND" in calls
    assert "REFUSED" not in capsys.readouterr().err


# --- _windows_health_steps: soft-gate failures must not degrade silently ----
#
# state/bug-backlog/2026-08-14-setup-only-installs-never-put-the-launch-
# be74f05c9f5b.yaml: a `cygpath`/`powershell.exe` probe failure previously
# degraded to a bare "skipping PATH integration" WARNING with no stated
# consequence or remediation -- an operator reading it had no way to know
# bare-name CLI invocation would fail, nor what to do about it.


def test_windows_health_steps_cygpath_unavailable_states_consequence_and_fix(monkeypatch, capsys):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    bin_dst = Path(_FAKE_REAL_INSTALL_PATH)

    monkeypatch.setattr(substrate, "_cygpath_w", lambda p: "")
    # The AppX-stub/store-alias legs downstream of the PATH check still run
    # (they don't depend on a resolved Windows path) -- answer "nothing
    # found", rather than asserting this leg is never reached.
    monkeypatch.setattr(substrate, "_orphan_appx_stub", lambda path: False)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    _windows_health_steps(bin_dst, check_only=False)

    err = capsys.readouterr().err
    assert "cygpath unavailable" in err
    assert "bare-name CLI invocation will fail" in err
    assert "re-run install" in err


def test_windows_health_steps_unreadable_user_path_states_consequence_and_fix(monkeypatch, capsys):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    bin_dst = Path(_FAKE_REAL_INSTALL_PATH)

    monkeypatch.setattr(substrate, "_cygpath_w", lambda p: p)
    monkeypatch.setattr(substrate, "_win_user_path_entries", lambda: None)
    monkeypatch.setattr(substrate, "_orphan_appx_stub", lambda path: False)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    _windows_health_steps(bin_dst, check_only=False)

    err = capsys.readouterr().err
    assert "could not read Windows user PATH" in err
    assert "bare-name CLI invocation will fail" in err
    assert "re-run install" in err


# --- run(): setup-only / soft-gate PATH visibility --------------------------
#
# Regression coverage for the same backlog item -- `run(setup_only=True)`
# never wrote the Windows PATH entry and, on the full chain, `_is_windows_shell()`
# returning False (OSTYPE/OS unset) silently skipped `_windows_health_steps`
# with no operator-visible trace. Both branches previously returned 0 with
# no message naming the consequence or the fix; this coverage locks in that
# they now do, without re-exercising the full install-substrate write chain
# (already covered by the `_windows_health_steps`/`_install_bin_resolvers`
# suites elsewhere in this file).


def _stub_run_dependencies(monkeypatch, tmp_path):
    """Monkeypatch every `run()` step up to (and past) the `setup_only`
    checkpoint so tests can isolate the new PATH-visibility messaging
    without standing up the full install-substrate write surface."""
    plugin_root = tmp_path / "plugin"
    (plugin_root / "templates" / "machine-local").mkdir(parents=True)
    (plugin_root / "templates" / "bin").mkdir(parents=True)
    (plugin_root / "templates" / "setup").mkdir(parents=True)

    claude_klabauter_root = tmp_path / "claude-klabauter"
    ch_bin = claude_klabauter_root / "coordinator" / "lib" / "claude-home"
    ch_bin.mkdir(parents=True)

    ml_templates = plugin_root / "templates" / "machine-local"
    for name in substrate._TRACKED_ML_FILES:
        (ml_templates / name).write_text("")
    (ml_templates / f"{substrate._ML_UNREAL_TOML_NAME}.example").write_text("")
    (ml_templates / f"{substrate._ML_REGISTRY_TOML_NAME}.example").write_text("")
    (ml_templates / f"{substrate._ML_HARDWARE_TOML_NAME}.example").write_text("")

    home = tmp_path / "home"
    home.mkdir()
    settings = tmp_path / "settings-home"

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    # Patched under the name substrate imports it as. `848072a10` renamed this
    # from `coordinator_claude_klabauter_root_with_class` and did not carry the stub, so
    # all four run_setup_only tests died in setup on AttributeError rather than
    # on anything they assert.
    monkeypatch.setattr(
        substrate, "coordinator_engine_root_with_class", lambda: (str(claude_klabauter_root), "live-working-tree")
    )
    monkeypatch.setattr(
        substrate, "_load_setup_template_manifest", lambda root: (["a"], [], [])
    )
    monkeypatch.setattr(substrate, "require_home", lambda who: str(home))
    monkeypatch.setattr(
        substrate, "migrate_substrate_to_settings_home", lambda *a, **k: 0
    )
    monkeypatch.setattr(substrate, "settings_home", lambda: settings)
    monkeypatch.setattr(substrate, "_resolve_baked_python_bin", lambda: "")
    monkeypatch.setattr(substrate, "_install_bin_resolvers", lambda *a, **k: None)
    monkeypatch.setattr(substrate, "_percolation_and_path_steps", lambda *a, **k: None)
    monkeypatch.setattr(substrate, "_register_hardware_concern", lambda *a, **k: None)
    monkeypatch.setattr(substrate, "_run_hardware_audit", lambda *a, **k: None)
    monkeypatch.setattr(substrate, "_c10a_steps", lambda *a, **k: 0)
    monkeypatch.setattr(substrate, "_install_seed_wikis", lambda *a, **k: None)
    monkeypatch.setattr(substrate, "_fnm_step", lambda *a, **k: None)
    return settings / "bin"


def test_run_setup_only_on_windows_states_path_not_written(monkeypatch, tmp_path, capsys):
    _stub_run_dependencies(monkeypatch, tmp_path)
    monkeypatch.setattr(substrate, "_is_windows_shell", lambda: True)

    rc = run(setup_only=True)

    assert rc == 0
    err = capsys.readouterr().err
    assert "was NOT added to the Windows user PATH" in err
    assert "Bare-name coordinator CLI invocation will not resolve" in err
    assert "without --setup-only" in err


def test_run_setup_only_off_windows_keeps_original_notice(monkeypatch, tmp_path, capsys):
    _stub_run_dependencies(monkeypatch, tmp_path)
    monkeypatch.setattr(substrate, "_is_windows_shell", lambda: False)

    rc = run(setup_only=True)

    assert rc == 0
    out = capsys.readouterr().out
    assert "skipping fnm/Windows machine-env steps" in out
    assert "substrate seeded" in out


def test_run_setup_only_check_only_off_windows_does_not_claim_seeded(monkeypatch, tmp_path, capsys):
    # R3: under check_only nothing is written, so the notice must not claim
    # the substrate WAS seeded.
    bin_dst = _stub_run_dependencies(monkeypatch, tmp_path)
    ml_dst = bin_dst.parent / "machine-local"
    ml_dst.mkdir(parents=True)
    for name in substrate._TRACKED_ML_FILES:
        (ml_dst / name).write_text("")
    monkeypatch.setattr(substrate, "_is_windows_shell", lambda: False)

    rc = run(setup_only=True, check_only=True)

    assert rc == 0
    out = capsys.readouterr().out
    assert "skipping fnm/Windows machine-env steps" in out
    assert "substrate would be seeded" in out
    assert "substrate seeded" not in out


def test_run_setup_only_check_only_on_windows_does_not_claim_seeded(monkeypatch, tmp_path, capsys):
    # R3 sibling: the Windows-branch notice has the same defect ("forwarders
    # seeded") under check_only.
    bin_dst = _stub_run_dependencies(monkeypatch, tmp_path)
    ml_dst = bin_dst.parent / "machine-local"
    ml_dst.mkdir(parents=True)
    for name in substrate._TRACKED_ML_FILES:
        (ml_dst / name).write_text("")
    monkeypatch.setattr(substrate, "_is_windows_shell", lambda: True)

    rc = run(setup_only=True, check_only=True)

    assert rc == 0
    err = capsys.readouterr().err
    assert "forwarders would be seeded" in err
    assert "forwarders seeded" not in err


# A `run()`-level test of the `os.name == "nt"` soft-gate-mismatch branch
# (`_is_windows_shell()` False while `os.name == "nt"`) is not exercisable in
# this sandbox: flipping `os.name` mid-process makes every subsequent
# `pathlib.Path(...)` construction resolve to `WindowsPath`, which raises
# `pathlib.UnsupportedOperation` on a non-Windows interpreter (Python 3.14's
# strict pathlib) -- `run()` constructs fresh `Path` objects throughout, so
# this combination cannot be driven end-to-end off this host. The branch
# itself is reasoned from source (mirrors the already-covered `_is_windows_
# shell() is True` setup-only branch immediately above), not executed here.


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "cannot simulate POSIX on a Windows host: this test monkeypatches "
        "substrate.os.name to 'posix', and pathlib dispatches Path() on os.name, so "
        "every subsequent Path(...) inside run() constructs a PosixPath from a "
        "Windows-shaped tmp_path and raises UnsupportedOperation before reaching "
        "the assertion. The POSIX chain is covered for real on POSIX CI."
    ),
)
def test_run_full_chain_posix_no_warning(monkeypatch, tmp_path, capsys):
    _stub_run_dependencies(monkeypatch, tmp_path)
    monkeypatch.setattr(substrate, "_is_windows_shell", lambda: False)
    monkeypatch.setattr(substrate.os, "name", "posix")

    rc = run(setup_only=False)

    assert rc == 0
    assert "Windows PATH integration skipped" not in capsys.readouterr().err


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


def _fake_orphan_appx_stub(calls, orphan_path=_FAKE_APPX_STUB_PATH):
    # C11: replaces the old `_powershell` fake -- `_windows_health_steps`
    # now probes each candidate path via `_orphan_appx_stub` instead of a
    # PowerShell `Get-Item`/`Test-Path` spawn. Only the python3.exe
    # candidate (matching `orphan_path`) reports an orphan; python.exe (the
    # other stub_name in the loop) reports none.
    def fake(path):
        calls.append(path)
        return path == orphan_path

    return fake


# --- _orphan_appx_stub: real body against real live aliases -----------------
#
# Review: coordinator:code-reviewer (P1/P2) -- every test above monkeypatches
# `_orphan_appx_stub` itself, never exercising its real lstat/stat body, so
# nothing here proved the resolvability split (live vs. orphaned APPEXECLINK)
# actually holds. Measured directly (Windows 11, 2026-08-14): 55 zero-length
# APPEXECLINK aliases enumerated under a real WindowsApps directory, zero
# reported as orphans by the real function. This test locks that in against
# whatever live aliases the box running it happens to have, and skips cleanly
# rather than asserting anything when there are none to check.
@pytest.mark.skipif(sys.platform != "win32", reason="APPEXECLINK aliases are Windows-only")
def test_orphan_appx_stub_reports_no_false_positives_on_real_live_aliases():
    windows_apps = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps"
    if not windows_apps.is_dir():
        pytest.skip("no WindowsApps directory on this box")

    checked = 0
    for candidate in windows_apps.iterdir():
        try:
            st = os.lstat(candidate)
        except OSError:
            continue
        if st.st_size != 0:
            continue
        if getattr(st, "st_reparse_tag", 0) != substrate._IO_REPARSE_TAG_APPEXECLINK:
            continue
        checked += 1
        assert not substrate._orphan_appx_stub(str(candidate)), (
            f"{candidate} is a live app-execution alias present on disk; "
            "_orphan_appx_stub must not report it as orphaned"
        )

    if checked == 0:
        pytest.skip("no zero-length APPEXECLINK aliases present on this box")


def test_windows_health_steps_appx_stub_disabled_never_prompts_or_deletes(monkeypatch, capsys):
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    bin_dst = Path(_FAKE_REAL_INSTALL_PATH)

    calls: list = []
    removed: list = []
    # Not tmp_path-rooted -- `_refuse_machine_mutation`'s temp-path check
    # would otherwise mask the assertion this test is actually about (the
    # DISABLE_ENV guard, not the temp-path one).
    fake_local_app_data = str(Path(_FAKE_APPX_STUB_PATH).parent.parent.parent)
    monkeypatch.setenv("LOCALAPPDATA", fake_local_app_data)
    orphan_path = _FAKE_APPX_STUB_PATH
    monkeypatch.setattr(substrate, "_cygpath_w", lambda p: p)
    monkeypatch.setattr(substrate, "_win_user_path_entries", _fake_win_user_path_entries_for_windows_health_steps(already_present=True))
    monkeypatch.setattr(substrate, "_orphan_appx_stub", _fake_orphan_appx_stub(calls, orphan_path))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(os, "remove", lambda p: removed.append(p))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("COORDINATOR_NON_INTERACTIVE", raising=False)

    def _unexpected_input(prompt=""):
        raise AssertionError("consent prompt must never fire when the guard refuses first")

    monkeypatch.setattr("builtins.input", _unexpected_input)

    _windows_health_steps(bin_dst, check_only=False)

    assert not removed, (
        "disabled mutation must not delete the orphan AppX stub"
    )
    err = capsys.readouterr().err
    assert "REFUSED" in err
    assert "COORDINATOR_DISABLE_MACHINE_MUTATION" in err


def test_windows_health_steps_appx_stub_enabled_prompt_still_deletes(monkeypatch, capsys):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    bin_dst = Path(_FAKE_REAL_INSTALL_PATH)

    calls: list = []
    removed: list = []
    fake_local_app_data = str(Path(_FAKE_APPX_STUB_PATH).parent.parent.parent)
    monkeypatch.setenv("LOCALAPPDATA", fake_local_app_data)
    orphan_path = _FAKE_APPX_STUB_PATH
    monkeypatch.setattr(substrate, "_cygpath_w", lambda p: p)
    monkeypatch.setattr(substrate, "_win_user_path_entries", _fake_win_user_path_entries_for_windows_health_steps(already_present=True))
    monkeypatch.setattr(substrate, "_orphan_appx_stub", _fake_orphan_appx_stub(calls, orphan_path))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(os, "remove", lambda p: removed.append(p))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.delenv("COORDINATOR_NON_INTERACTIVE", raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    _windows_health_steps(bin_dst, check_only=False)

    assert removed, (
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
    # Fabricated rather than generated: the generator is deleted, but the
    # SWEEP that clears what it left on real boxes is live and is what this
    # test is about. Only the marker matters to the sweep.
    cmd_orphan.write_text(
        f"@echo off\nREM {substrate._AGENT_CMD_FORWARDER_MARKER}\nREM stale fixture for {name}\n",
        encoding="utf-8",
        newline="\n",
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


def test_c10a_steps_without_allow_venv_fallback_skips_venv_step_entirely(monkeypatch, tmp_path, capsys):
    """Step C10a-3 (venv rebuild + legacy-venv removal) is break-glass only
    (docs/plans/2026-08-18-retire-coordinator-venv.md chunk C4, AC5):
    without `allow_venv_fallback=True`, the step is skipped outright and the
    legacy venv survives regardless of `COORDINATOR_DISABLE_MACHINE_MUTATION`."""
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
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

    def _fail_if_called(*a, **k):
        raise AssertionError(
            "ensure_coordinator_venv must not be reached without allow_venv_fallback"
        )

    monkeypatch.setattr(_ensure_venv_module, "ensure_coordinator_venv", _fail_if_called)

    rc = _c10a_steps(str(install_base), settings_home_path, plugin_root, bin_dst, check_only=False)

    assert rc == 0
    assert legacy_venv.is_dir(), "no allow_venv_fallback opt-in -- legacy venv must survive"
    out = capsys.readouterr().out
    assert "--allow-venv-fallback" in out


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

    rc = _c10a_steps(
        str(install_base), settings_home_path, plugin_root, bin_dst, check_only=False,
        allow_venv_fallback=True,
    )

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

    rc = _c10a_steps(
        str(install_base), settings_home_path, plugin_root, bin_dst, check_only=False,
        allow_venv_fallback=True,
    )

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


# --- resolve_hook_python_bin -- machine interpreter, not the venv pin -------
#
# Regression coverage for docs/plans/2026-08-14-the-venv-fallback-stops-
# being-something.md C2: the resolver used to call resolve_python_bin(),
# whose tier 1/2 is COORDINATOR_PYTHON / machine-local coordinator.python --
# the venv pin ensure_venv.py writes for purposes (a)/(b), unrelated to
# hooks. A box that merely HAS that pin set (e.g. for coordinator_whoami)
# got every hook command pointed at the venv regardless. This resolver must
# instead resolve the OS-detect (machine) tier directly, ignoring any pin.


def test_resolve_hook_python_bin_ignores_the_venv_pin(monkeypatch):
    import coordinator_core.pyresolve as pyresolve

    # A pin is present (as it would be on a box that ran ensure_venv.py for
    # purpose (a)/(b)) -- resolve_python_bin() would return this if called.
    monkeypatch.setenv("COORDINATOR_PYTHON", "/settings-home/.coordinator-venv/bin/python3")

    monkeypatch.setattr(pyresolve, "_is_windows", lambda: False)
    monkeypatch.setattr(pyresolve, "_resolve_non_windows", lambda: ("/usr/bin/python3", []))

    def _fail_if_called(*a, **k):
        raise AssertionError("resolve_python_bin (pin-aware) must not be called")

    monkeypatch.setattr(pyresolve, "resolve_python_bin", _fail_if_called)

    assert resolve_hook_python_bin() == "/usr/bin/python3"


def test_resolve_hook_python_bin_resolves_windows_console_interpreter(monkeypatch):
    import coordinator_core.pyresolve as pyresolve

    captured = {}

    def _fake_resolve_windows(**kwargs):
        captured.update(kwargs)
        return "/machine/python.exe", []

    monkeypatch.setattr(pyresolve, "_is_windows", lambda: True)
    monkeypatch.setattr(pyresolve, "_resolve_windows", _fake_resolve_windows)
    monkeypatch.setattr(
        pyresolve, "_resolve_non_windows",
        lambda: (_ for _ in ()).throw(AssertionError("non-Windows tier must not run on a Windows host")),
    )

    assert resolve_hook_python_bin() == "/machine/python.exe"
    assert captured == {"prefer_windowless": False}


def test_resolve_hook_python_bin_empty_for_launcher_args(monkeypatch):
    import coordinator_core.pyresolve as pyresolve

    monkeypatch.setattr(pyresolve, "_is_windows", lambda: True)
    monkeypatch.setattr(pyresolve, "_resolve_windows", lambda **_: ("py", ["-3"]))
    assert resolve_hook_python_bin() == ""


def test_resolve_hook_python_bin_surfaces_resolution_error(monkeypatch, capsys):
    import coordinator_core.pyresolve as pyresolve

    def _raise():
        raise RuntimeError("no interpreter resolvable")

    monkeypatch.setattr(pyresolve, "_is_windows", _raise)

    result = resolve_hook_python_bin()
    assert result == ""
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "no interpreter resolvable" in err


# --- C13: forwarder-set gap closed -----------------------------------------
#
# docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md § C13. The four
# names the chunk's delta-measure found genuinely missing from the publish
# allowlist (`measure-amplification-discriminator`, `publish_refusal_record`,
# `query-work-state`, and `classify-resolver-callers` -- the fourth, added by
# EM ruling 2026-08-19 after C7 landed that CLI post-dating this chunk body's
# last write) must resolve identically through BOTH surfaces this chunk
# touches:
#   - `_derive_agent_helper_target_map` (this module) -- the live-tree
#     installed-name -> on-disk-target map `exec_cli`'s published-vs-live
#     gate was measured against.
#   - the field-7 allowlist of the `claude-klabauter-coordinator-bin` row in
#     `setup/publish-targets.portable` -- the ONLY per-name inclusion list
#     `publish.py`/`substrate.py` consult (neither carries one itself; see
#     the chunk body's WHERE-THE-INCLUSION-LIST correction).
#
# Regression guard: a future rename/removal of any of the four that is not
# mirrored in the allowlist should fail this test rather than silently
# reintroduce the exec_cli-fallback-dependent gap C13 closed.
_C13_MEASURED_GAP_NAMES = (
    "measure-amplification-discriminator",
    "publish_refusal_record",
    "query-work-state",
    "classify-resolver-callers",
)


def _publish_allowlist_names() -> "set[str]":
    """Field-7 allowlist of the `claude-klabauter-coordinator-bin` row in
    `setup/publish-targets.portable` (repo-tracked copy), split on the
    comma-separated allowlist field. Row shape: 7 pipe-separated fields,
    field 7 (index 6) is the comma-separated filename allowlist -- see
    `verify-publish-targets-portable-sync.py`'s own docstring for the field
    layout this mirrors."""
    portable_path = (
        Path(__file__).resolve().parents[2] / "setup" / "publish-targets.portable"
    )
    for line in portable_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("claude-klabauter-coordinator-bin|"):
            fields = line.split("|")
            return set(fields[6].split(","))
    raise AssertionError(
        "claude-klabauter-coordinator-bin row not found in "
        f"{portable_path}"
    )


def test_c13_measured_gap_names_present_in_publish_allowlist():
    allowlist_names = _publish_allowlist_names()
    for installed_name in _C13_MEASURED_GAP_NAMES:
        assert installed_name in allowlist_names or any(
            n.startswith(installed_name + ".") for n in allowlist_names
        ), f"{installed_name} missing from publish-targets.portable allowlist"


def test_c13_target_map_and_publish_allowlist_agree_on_measured_gap_names():
    agent_bin = Path(__file__).resolve().parents[2] / "coordinator" / "bin"
    target_map = _derive_agent_helper_target_map(agent_bin)
    allowlist_names = _publish_allowlist_names()

    for installed_name in _C13_MEASURED_GAP_NAMES:
        assert installed_name in target_map, (
            f"{installed_name} not resolvable via "
            "_derive_agent_helper_target_map against the live tree"
        )
        ondisk_target = target_map[installed_name]
        assert ondisk_target in allowlist_names, (
            f"{installed_name} -> {ondisk_target} resolved by "
            "_derive_agent_helper_target_map but that on-disk filename is "
            "absent from publish-targets.portable's allowlist"
        )

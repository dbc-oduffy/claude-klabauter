"""
coordinator_core.plugin_health.tests.test_sentinel

Regression pin for _resolve_claude_home() — machine-local-registry.md §4a
semantics: CLAUDE_HOME is a $HOME SUBSTITUTE, not the .claude dir itself.

Backlog: state/bug-backlog/2026-07-19-sentinel-py-claude-home-4a-semantics.yaml
(residual verification of archive/bug-backlog/2026-07/2026-06-18-doctor-sentinel-
robustness-followups.yaml item (a) — the retired bash sentinel reassigned
`CLAUDE_HOME="${CLAUDE_HOME:-$HOME/.claude}"`, treating CLAUDE_HOME as already
being the .claude dir; a forwarder that later appended /.claude to that
reassigned value double-suffixed to .claude/.claude. The Python port never
reassigns CLAUDE_HOME itself — it only derives a local `claude_home` Path — so
the double-suffix path cannot recur. These tests pin that shape.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.install import prereq_probe
from coordinator_core.plugin_health import sentinel as S
from coordinator_core.plugin_health.sentinel import (
    _call_native_main,
    _currency_plugin_root,
    _exec_detail,
    _inconclusive,
    _is_runnable_file,
    _resolve_claude_home,
    _resolve_cli,
    _run_prereq_probe_function,
    probe_p15,
    probe_p17,
)


def test_resolve_claude_home_falls_back_to_home_when_unset():
    assert _resolve_claude_home(None) == Path.home() / ".claude"


def test_resolve_claude_home_falls_back_to_home_when_empty_string():
    assert _resolve_claude_home("") == Path.home() / ".claude"


def test_resolve_claude_home_appends_dot_claude_to_custom_home_substitute():
    assert _resolve_claude_home("/custom/home") == Path("/custom/home/.claude")


def test_resolve_claude_home_does_not_double_suffix_when_env_already_ends_in_dot_claude():
    # §4a: CLAUDE_HOME is a $HOME substitute — a value that already ends in
    # /.claude is itself a misconfiguration, not something our resolution
    # compounds. We append exactly once regardless of the input's shape,
    # matching the bash oracle's ${CLAUDE_HOME:-$HOME}/.claude/ — never
    # `${CLAUDE_HOME:-$HOME/.claude}/.claude/`.
    result = _resolve_claude_home("/fake/home/.claude")
    assert result == Path("/fake/home/.claude/.claude")
    assert str(result).count(".claude") == 2  # exactly one suffix append, not a self-compounding loop


def test_resolve_claude_home_is_idempotent_pure_function():
    # Calling twice with the same input never mutates shared state (the
    # retired bash bug's mechanism was CLAUDE_HOME REASSIGNMENT accumulating
    # across calls/children — this is a pure function over its argument).
    first = _resolve_claude_home("/custom/home")
    second = _resolve_claude_home("/custom/home")
    assert first == second == Path("/custom/home/.claude")


# ---------------------------------------------------------------------------
# Windows platform-awareness (the module had ZERO os.name references before
# 2026-07-20; every probe that exec'd a POSIX shebang script by bare path hit
# OSError [WinError 193] and converted it into a fabricated "broken" verdict).
#
# All of these monkeypatch os.name so they are meaningful on any platform.
# ---------------------------------------------------------------------------


def _as_nt(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")


def _as_posix(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")


# --- _resolve_cli: .cmd preference on Windows ---


def test_resolve_cli_prefers_dot_cmd_on_windows(tmp_path, monkeypatch):
    _as_nt(monkeypatch)
    sh_bin = tmp_path / "sh_bin"
    sh_bin.mkdir()
    (sh_bin / "machine-local").write_text("#!/usr/bin/env bash\n")
    (sh_bin / "machine-local.cmd").write_text("@echo off\n")
    assert _resolve_cli(sh_bin, tmp_path / "absent", "machine-local") == str(
        sh_bin / "machine-local.cmd"
    )


def test_resolve_cli_ignores_dot_cmd_on_posix(tmp_path, monkeypatch):
    # `_as_posix` fakes os.name, but a Windows filesystem cannot express the one
    # thing the POSIX branch then consults: the executable bit. `chmod(0o755)`
    # there only toggles the read-only attribute, so `is_executable` stays False,
    # `_resolve_cli` falls past both candidate dirs, and `shutil.which` answers
    # off the REAL PATH -- on a box with coordinator installed that is a live
    # `machine-local.CMD`, so the assertion failed with a host path having never
    # exercised the branch under test. Fake the bit too, and empty PATH so the
    # `shutil.which` rung cannot reach this machine at all: a regression now
    # fails as `None`, never as somebody's install.
    _as_posix(monkeypatch)
    monkeypatch.setattr(S, "is_executable", lambda p: Path(p).is_file())
    monkeypatch.setenv("PATH", str(tmp_path / "empty_path"))
    sh_bin = tmp_path / "sh_bin"
    sh_bin.mkdir()
    posix_wrapper = sh_bin / "machine-local"
    posix_wrapper.write_text("#!/usr/bin/env bash\n")
    posix_wrapper.chmod(0o755)
    (sh_bin / "machine-local.cmd").write_text("@echo off\n")
    assert _resolve_cli(sh_bin, tmp_path / "absent", "machine-local") == str(posix_wrapper)


def test_resolve_cli_falls_back_to_extensionless_on_windows_when_no_cmd(tmp_path, monkeypatch):
    _as_nt(monkeypatch)
    sh_bin = tmp_path / "sh_bin"
    sh_bin.mkdir()
    (sh_bin / "claude-home").write_text("#!/usr/bin/env bash\n")
    assert _resolve_cli(sh_bin, tmp_path / "absent", "claude-home") == str(sh_bin / "claude-home")


def test_resolve_cli_settings_home_bin_wins_over_compat_bin_on_windows(tmp_path, monkeypatch):
    # Precedence order (settings-home bin first, compat forwarder second) must
    # survive the .cmd preference — a .cmd in the LOWER-priority dir must not
    # outrank a resolvable CLI in the higher-priority one.
    _as_nt(monkeypatch)
    sh_bin = tmp_path / "sh_bin"
    bin_dir = tmp_path / "bin_dir"
    sh_bin.mkdir()
    bin_dir.mkdir()
    (sh_bin / "machine-local.cmd").write_text("@echo off\n")
    (bin_dir / "machine-local.cmd").write_text("@echo off\n")
    assert _resolve_cli(sh_bin, bin_dir, "machine-local") == str(sh_bin / "machine-local.cmd")


def test_resolve_cli_falls_through_to_compat_bin_cmd(tmp_path, monkeypatch):
    _as_nt(monkeypatch)
    sh_bin = tmp_path / "sh_bin"
    bin_dir = tmp_path / "bin_dir"
    sh_bin.mkdir()
    bin_dir.mkdir()
    (bin_dir / "machine-local.cmd").write_text("@echo off\n")
    assert _resolve_cli(sh_bin, bin_dir, "machine-local") == str(bin_dir / "machine-local.cmd")


def test_resolve_cli_returns_none_when_nothing_resolves(tmp_path, monkeypatch):
    _as_nt(monkeypatch)
    monkeypatch.setattr(S.shutil, "which", lambda _n: None)
    assert _resolve_cli(tmp_path / "a", tmp_path / "b", "machine-local") is None


# --- _is_runnable_file: X_OK is meaningless on Windows ---


def test_is_runnable_file_on_windows_needs_only_is_file(tmp_path, monkeypatch):
    _as_nt(monkeypatch)
    f = tmp_path / "probe.sh"
    f.write_text("#!/usr/bin/env bash\n")
    # Force X_OK False to prove the nt branch does not consult os.access at all.
    monkeypatch.setattr(S.os, "access", lambda *_a, **_k: False)
    assert _is_runnable_file(f) is True


def test_is_runnable_file_on_posix_still_requires_x_ok(tmp_path, monkeypatch):
    _as_posix(monkeypatch)
    f = tmp_path / "probe.sh"
    f.write_text("#!/usr/bin/env bash\n")
    monkeypatch.setattr(S.os, "access", lambda *_a, **_k: False)
    assert _is_runnable_file(f) is False


def test_is_runnable_file_false_for_missing_and_for_dirs(tmp_path, monkeypatch):
    _as_nt(monkeypatch)
    assert _is_runnable_file(tmp_path / "nope.sh") is False
    assert _is_runnable_file(tmp_path) is False


# --- P-9/P-11/P-13/P-18: presence-gate retirement — always in-process ---
#
# These 4 probes formerly shelled out to their own DoE-owned `.sh` sibling
# scripts via _sh_argv (retired). DoE's W4a rename (b5a4192c) turned each
# `.sh` into a thin polyglot trampoline over an already-native claude-klabauter module,
# so the probe now calls that module's main() directly in-process. A
# subsequent presence-gate (_sibling_present, checking for the sibling script
# on disk before making the call) has ALSO been retired: the call was always
# in-process against an unconditionally-imported native module, so the gate
# proved nothing about whether the call could run — it only produced a false
# GREEN whenever the on-disk sibling was relocated/renamed (as happened in
# b644d5a9). Every test below therefore asserts the probe calls its native
# module UNCONDITIONALLY, with no on-disk fixture required at all, and pins
# the "never a silent GREEN on native-call failure" invariant: a failing
# native call must surface as amber inconclusive(...), never [].


def _block_subprocess(monkeypatch):
    """Any of these 4 probes falling back to a subprocess spawn is exactly
    the regression this repoint eliminates — fail loud, don't silently pass."""

    def _boom(*_a, **_k):
        raise AssertionError("probe spawned a subprocess — expected an in-process main() call")

    monkeypatch.setattr(S.subprocess, "run", _boom)


# --- P-9 / verify_ue_overrides ---


def test_p9_calls_verify_ue_overrides_main_in_process_with_no_sibling_on_disk(tmp_path, monkeypatch):
    """No sibling script fixture at all — the call must still fire (the
    presence gate is gone)."""
    _block_subprocess(monkeypatch)
    sh_bin = tmp_path / "sh_bin"  # deliberately does NOT exist
    calls = {}

    def _fake_main(argv, script_dir=None):
        calls["argv"] = argv
        calls["script_dir"] = script_dir
        return 0

    monkeypatch.setattr(S.verify_ue_overrides, "main", _fake_main)
    assert S.probe_p9(sh_bin) == []
    assert calls["script_dir"] == str(sh_bin)


def test_p9_nonzero_rc_reports_amber(tmp_path, monkeypatch):
    _block_subprocess(monkeypatch)
    monkeypatch.setattr(S.verify_ue_overrides, "main", lambda *a, **k: 1)
    (note,) = S.probe_p9(tmp_path / "sh_bin")
    assert note.severity == "amber"
    # Deliberately NOT a private repo codename. Publish depersonalizes those on
    # the way to the mirror, so a key name hardcoded in operator-facing text
    # reaches the published engine as a placeholder no registry can hold. The
    # message points at the CLI, which names the real key at runtime.
    assert "verify-ue-overrides.py" in note.message
    assert "example_game_workbench_repo" not in note.message


def test_p9_main_raising_routes_to_inconclusive_never_silent_green(tmp_path, monkeypatch):
    _block_subprocess(monkeypatch)

    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(S.verify_ue_overrides, "main", _raise)
    (note,) = S.probe_p9(tmp_path / "sh_bin")
    assert note.severity == "amber"
    assert note.message.startswith("inconclusive(")
    assert "RuntimeError" in note.message


# --- P-11 / verify_templates_setup_sync ---


def test_p11_calls_verify_templates_setup_sync_main_in_process_with_no_sibling_on_disk(
    tmp_path, monkeypatch
):
    _block_subprocess(monkeypatch)
    plugins_root = tmp_path / "plugins"  # deliberately does NOT exist
    monkeypatch.setattr(S, "coordinator_doe_root", lambda: None)
    seen_plugin_root = {}

    def _fake_main(argv):
        seen_plugin_root["value"] = os.environ.get("CLAUDE_PLUGIN_ROOT")
        return 0

    monkeypatch.setattr(S.verify_templates_setup_sync, "main", _fake_main)
    assert S.probe_p11(plugins_root) == []
    assert seen_plugin_root["value"] == str(
        plugins_root / "coordinator-claude" / "coordinator"
    )
    # The temp env override must not leak past the call.
    assert "CLAUDE_PLUGIN_ROOT" not in os.environ


def test_p11_prefers_coordinator_root_when_given(tmp_path, monkeypatch):
    """coordinator_root, when it carries the templates/setup marker, is
    preferred as CLAUDE_PLUGIN_ROOT over the plugins_root-derived
    marketplace path — marker-verified, not assumed (_doe_payload_root)."""
    _block_subprocess(monkeypatch)
    monkeypatch.setattr(S, "coordinator_doe_root", lambda: None)
    plugins_root = tmp_path / "plugins"
    coordinator_root = tmp_path / "doe-clone" / "coordinator"
    (coordinator_root / "templates" / "setup").mkdir(parents=True)
    seen_plugin_root = {}

    def _fake_main(argv):
        seen_plugin_root["value"] = os.environ.get("CLAUDE_PLUGIN_ROOT")
        return 0

    monkeypatch.setattr(S.verify_templates_setup_sync, "main", _fake_main)
    assert S.probe_p11(plugins_root, coordinator_root) == []
    assert seen_plugin_root["value"] == str(coordinator_root)


def test_p11_nonzero_rc_reports_amber(tmp_path, monkeypatch):
    _block_subprocess(monkeypatch)
    monkeypatch.setattr(S.verify_templates_setup_sync, "main", lambda *a, **k: 1)
    (note,) = S.probe_p11(tmp_path / "plugins")
    assert note.severity == "amber"
    assert "templates/setup drift" in note.message


def test_p11_remediation_does_not_instruct_a_hand_clobber(tmp_path, monkeypatch):
    """AC8: the remediation must not tell the operator to `cp` a template over
    the (possibly foreign-repo-tracked) destination — C6 now delivers that
    kind of overwrite via a careful write, not a hand copy."""
    _block_subprocess(monkeypatch)
    monkeypatch.setattr(S.verify_templates_setup_sync, "main", lambda *a, **k: 1)
    (note,) = S.probe_p11(tmp_path / "plugins")
    assert "cp coordinator/templates/setup" not in note.message
    assert "cp " not in note.message
    assert "real, but not urgent" in note.message


def test_p11_remediation_does_not_promise_the_installer_will_clear_the_drift(
    tmp_path, monkeypatch
):
    """P-11 drift is a DECISION, not a sync, and the remediation must say so.

    The install chain does reach this mirror — substrate.py's
    `_percolation_and_path_steps` runs as Phase 3 Step 1 and owns
    templates/setup delivery — but for a drifted destination it classifies the
    file as operator-customized and PRESERVES it, logging `[machine-local]
    operator-customized <file> preserved; template at <path> for diff
    reference`. It cannot distinguish a deliberate local edit from stale
    content, so it declines to guess.

    Two opposite wrong messages have shipped here in one day, which is why this
    test pins the shape rather than either wording. The first claimed no
    install phase touches templates/setup at all (false — the machinery is
    real). The second concluded from that machinery's existence that
    re-running the installer is the remedy (also false — it preserves rather
    than overwrites). Both were plausible from a code read; only the
    experiment separates them.

    Empirical basis, 2026-08-12: perturbed ~/.claude/setup/.percolate-identity.example,
    ran install-maximalist.py --non-interactive to completion (rc=0), and the
    drift survived, with the preserve line in the log.
    """
    _block_subprocess(monkeypatch)
    monkeypatch.setattr(S.verify_templates_setup_sync, "main", lambda *a, **k: 1)
    (note,) = S.probe_p11(tmp_path / "plugins")
    # Must not promise a re-install clears it — the failure mode is an operator
    # running the installer twice and concluding the probe is broken.
    assert "re-running the coordinator installer" not in note.message
    assert "WILL NOT CLEAR THIS" in note.message
    # Must not swing back to claiming the machinery is absent either.
    assert "no install phase syncs templates/setup" not in note.message
    # Must still forbid the hand-clobber and name the decision-shaped exit.
    assert "cp " not in note.message.replace("`cp` over", "")
    assert "diff" in note.message.lower()


def test_p11_main_raising_routes_to_inconclusive_never_silent_green(tmp_path, monkeypatch):
    _block_subprocess(monkeypatch)

    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(S.verify_templates_setup_sync, "main", _raise)
    (note,) = S.probe_p11(tmp_path / "plugins")
    assert note.severity == "amber"
    assert note.message.startswith("inconclusive(")


# --- P-13 / probe_onboarding_currency ---


def test_p13_calls_probe_onboarding_currency_main_in_process_with_no_sibling_on_disk(
    tmp_path, monkeypatch
):
    _block_subprocess(monkeypatch)
    doe = tmp_path / "doe" / "coordinator"
    doe.mkdir(parents=True)
    (doe / "coordinator-schema-version").write_text("2\n")
    seen_env = {}

    def _fake_main(argv):
        seen_env["repo_root"] = os.environ.get("COORDINATOR_CURRENCY_REPO_ROOT")
        seen_env["plugin_root"] = os.environ.get("COORDINATOR_CURRENCY_PLUGIN_ROOT")
        print("current")
        return 0

    monkeypatch.setattr(S.probe_onboarding_currency, "main", _fake_main)
    assert S.probe_p13(tmp_path / "repo", tmp_path / "plugins", doe) == []
    assert seen_env["repo_root"] == str(tmp_path / "repo")
    assert seen_env["plugin_root"] == str(doe)
    assert "COORDINATOR_CURRENCY_REPO_ROOT" not in os.environ
    assert "COORDINATOR_CURRENCY_PLUGIN_ROOT" not in os.environ


@pytest.mark.parametrize(
    "stdout, expect_substr",
    [
        ("drift(1->2)", "onboarding stamp is stale"),
        ("unstamped(legacy)", "predates onboarding currency"),
        ("", "produced no output"),
    ],
)
def test_p13_stdout_classification_preserved(tmp_path, monkeypatch, stdout, expect_substr):
    _block_subprocess(monkeypatch)

    def _fake_main(argv):
        if stdout:
            print(stdout)
        return 0

    monkeypatch.setattr(S.probe_onboarding_currency, "main", _fake_main)
    (note,) = S.probe_p13(tmp_path / "repo", tmp_path / "plugins", None)
    assert note.severity == "amber"
    assert expect_substr in note.message


def test_p13_main_raising_routes_to_inconclusive_never_silent_green(tmp_path, monkeypatch):
    _block_subprocess(monkeypatch)

    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(S.probe_onboarding_currency, "main", _raise)
    (note,) = S.probe_p13(tmp_path / "repo", tmp_path / "plugins", None)
    assert note.severity == "amber"
    assert note.message.startswith("inconclusive(")


# --- P-18 / check_install_singularity ---


def test_p18_calls_check_install_singularity_main_in_process_with_no_sibling_on_disk(
    tmp_path, monkeypatch
):
    _block_subprocess(monkeypatch)

    def _fake_main(argv):
        return 0

    monkeypatch.setattr(S.check_install_singularity, "main", _fake_main)
    assert S.probe_p18(None) == []


def test_p18_nonzero_rc_reports_red_with_diagnostic_first_line(monkeypatch):
    _block_subprocess(monkeypatch)

    def _fake_main(argv):
        sys.stderr.write("ERROR [singularity]: multiple trees detected\n")
        return 1

    monkeypatch.setattr(S.check_install_singularity, "main", _fake_main)
    (note,) = S.probe_p18(None)
    assert note.severity == "red"
    assert "multiple trees detected" in note.message
    # The old gate-miss's unsatisfiable diagnosis must be gone.
    assert "plugin may be corrupted" not in note.message


def test_p18_sets_and_restores_claude_home_around_the_call(monkeypatch):
    _block_subprocess(monkeypatch)
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    seen = {}

    def _fake_main(argv):
        seen["value"] = os.environ.get("CLAUDE_HOME")
        return 0

    monkeypatch.setattr(S.check_install_singularity, "main", _fake_main)
    assert S.probe_p18("/custom/home") == []
    assert seen["value"] == "/custom/home"
    assert "CLAUDE_HOME" not in os.environ


def test_p18_main_raising_routes_to_inconclusive_never_silent_green(monkeypatch):
    _block_subprocess(monkeypatch)

    def _raise(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(S.check_install_singularity, "main", _raise)
    (note,) = S.probe_p18(None)
    assert note.severity == "amber"
    assert note.message.startswith("inconclusive(")


# --- Fix 4: exec failure routes to inconclusive, never a fabricated verdict ---


def test_inconclusive_is_amber_with_honest_prefix():
    (note,) = _inconclusive("P-4", "exec: OSError: boom")
    assert note.id == "P-4"
    assert note.severity == "amber"  # never "red" — an unrunnable probe is not a broken substrate
    assert note.message.startswith("inconclusive(")


def test_exec_detail_names_the_observed_error():
    assert "OSError" in _exec_detail(OSError(193, "%1 is not a valid Win32 application"))
    assert "timed out" in _exec_detail(subprocess.TimeoutExpired(["x"], 60))


# --- _call_native_main SystemExit handling: None -> 0, non-int -> 1 ---


def test_call_native_main_bare_sys_exit_maps_to_rc_zero():
    def _bare_exit(argv):
        sys.exit()

    rc, _out, _err = _call_native_main(_bare_exit, [])
    assert rc == 0


def test_call_native_main_system_exit_none_maps_to_rc_zero():
    def _exit_none(argv):
        raise SystemExit(None)

    rc, _out, _err = _call_native_main(_exit_none, [])
    assert rc == 0


def test_call_native_main_system_exit_non_int_maps_to_rc_one():
    def _exit_msg(argv):
        raise SystemExit("boom")

    rc, _out, _err = _call_native_main(_exit_msg, [])
    assert rc == 1


def test_call_native_main_system_exit_int_code_passes_through():
    def _exit_two(argv):
        raise SystemExit(2)

    rc, _out, _err = _call_native_main(_exit_two, [])
    assert rc == 2


@pytest.mark.parametrize(
    "probe_id, invoke",
    [
        ("P-4", lambda tp: S.probe_p4("machine-local", tp / "sh_bin")),
        ("P-10", lambda tp: S.probe_p10("claude-home", tp / "sh_bin")),
    ],
)
def test_resolver_probes_report_inconclusive_on_winerror_193(
    probe_id, invoke, tmp_path, monkeypatch
):
    """WinError 193 is exactly what CreateProcess raises for an extension-less
    shebang script. Before this fix each of these probes swallowed it and
    emitted a confident verdict — P-4 even asserted "registry.toml
    unparseable", a cause it had not observed (and which P-2 independently
    disproves by parsing the file successfully)."""
    _as_nt(monkeypatch)

    def _boom(*_a, **_k):
        raise OSError(193, "%1 is not a valid Win32 application")

    monkeypatch.setattr(S.subprocess, "run", _boom)

    notes = invoke(tmp_path)
    assert len(notes) == 1
    (note,) = notes
    assert note.id == probe_id
    assert note.severity == "amber"
    assert note.message.startswith("inconclusive(")
    assert "WinError 193" in note.message or "OSError" in note.message
    # The fabricated diagnoses must be gone.
    assert "registry.toml unparseable" not in note.message
    assert "resolver drift" not in note.message


# test_p18_main_raising_routes_to_inconclusive (above) supersedes the retired
# subprocess-exec-failure version of this coverage — P-18 now calls
# check_install_singularity.main() in-process, so the "could not run the
# check" case is a raising main(), not a subprocess OSError, but the
# assertion is the same: amber inconclusive(...), never a fabricated
# "singularity check failed".


def test_p4_still_reports_red_when_the_cli_actually_runs_and_fails(tmp_path, monkeypatch):
    """Guardrail on Fix 4: only EXEC failures become inconclusive. A CLI that
    ran and exited nonzero is a real, observed failure and must stay RED."""
    _as_nt(monkeypatch)
    monkeypatch.setattr(
        S.subprocess,
        "run",
        lambda argv, *_a, **_k: subprocess.CompletedProcess(argv, 3, stdout=b"", stderr=b""),
    )
    (note,) = S.probe_p4("machine-local", tmp_path / "sh_bin")
    assert note.severity == "red"
    assert "exited 3" in note.message


def test_p3_reports_amber_when_the_registry_declares_no_repo_keys():
    (note,) = S.probe_p3(["coordinator.machine_slug", "core.x"])
    assert note.severity == "amber"
    assert "no repos.* keys" in note.message


def test_p3_passes_when_a_repos_key_is_declared():
    assert S.probe_p3(["core.x", "repos.doe_claude"]) == []


def test_p3_stays_silent_when_there_is_no_registry_to_read():
    """`None` is "no registry file exists", which is P-1/P-2's finding. P-3
    must not convert it into "no repos.* keys populated" — a verdict about
    data it never read."""
    assert S.probe_p3(None) == []


def test_p3_spawns_nothing(monkeypatch):
    """G8 pin: P-3's question is about registry DATA, and the process this
    probe runs in can parse the registry. Any subprocess here is the
    interpreter-cold-start-per-lookup defect growing back."""
    def _forbidden(*_a, **_k):
        raise AssertionError("probe_p3 must not spawn a subprocess")

    monkeypatch.setattr(S.subprocess, "run", _forbidden)
    assert S.probe_p3(["repos.doe_claude"]) == []


def test_registry_keys_returns_none_when_no_registry_file_exists(tmp_path):
    assert S._registry_keys(tmp_path) is None


def test_registry_keys_reads_the_registry_in_process(tmp_path, monkeypatch):
    (tmp_path / "registry.toml").write_text(
        'schema = 1\n\n[repos]\ndoe_claude = "/tmp/doe"\n', encoding="utf-8"
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path))
    assert "repos.doe_claude" in S._registry_keys(tmp_path)


# --- Fix 3: P-13 plugin-root fallback chain ---


def test_currency_plugin_root_prefers_doe_root_when_schema_file_present(tmp_path):
    doe = tmp_path / "doe" / "coordinator"
    doe.mkdir(parents=True)
    (doe / "coordinator-schema-version").write_text("2\n")
    assert _currency_plugin_root(doe, tmp_path / "plugins") == doe


def test_currency_plugin_root_falls_back_when_doe_root_lacks_schema_file(tmp_path, monkeypatch):
    """Marketplace-layout non-regression: the DoE-clone value is only verified
    for the dev-clone layout, so it is used only when it demonstrably carries
    the schema-version file the probe needs. coordinator_doe_root() is
    monkeypatched to None so this stays hermetic against the real machine's
    own DoE clone (which may itself carry the marker)."""
    monkeypatch.setattr(S, "coordinator_doe_root", lambda: None)
    doe = tmp_path / "doe" / "coordinator"
    doe.mkdir(parents=True)
    plugins_root = tmp_path / "plugins"
    assert _currency_plugin_root(doe, plugins_root) == (
        plugins_root / "coordinator-claude" / "coordinator"
    )


def test_currency_plugin_root_falls_back_when_doe_root_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "coordinator_doe_root", lambda: None)
    plugins_root = tmp_path / "plugins"
    assert _currency_plugin_root(None, plugins_root) == (
        plugins_root / "coordinator-claude" / "coordinator"
    )


# --- Review: code-reviewer (P2, Finding 1) — _doe_coordinator_root()'s own
# resolution ladder, repointed onto coordinator_doe_root() so P-11/P-13 honor
# REPO_DOE_CLAUDE/machine-local like every other consumer in the doe-root-sweep
# wave. Prior coverage above only exercised _currency_plugin_root()'s downstream
# fallback given an already-resolved value; these pin the resolver itself.


def test_doe_coordinator_root_prefers_coordinator_bin_root_override(monkeypatch, tmp_path):
    """COORDINATOR_BIN_ROOT stays rung 1 — sentinel's own documented
    test-isolation seam, ahead of the shared coordinator_doe_root() ladder."""
    monkeypatch.delenv("REPO_DOE_CLAUDE", raising=False)
    bin_dir = tmp_path / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_BIN_ROOT", str(bin_dir))
    assert S._doe_coordinator_root() == tmp_path / "coordinator"


def test_doe_coordinator_root_resolves_via_repo_doe_claude_env(monkeypatch, tmp_path):
    """REPO_DOE_CLAUDE alone (no ~/.claude/.doe-root file) must resolve —
    this is the exact gap Finding 1 identified: the pre-fix resolver only ever
    read ~/.claude/.doe-root directly and never consulted REPO_DOE_CLAUDE."""
    monkeypatch.delenv("COORDINATOR_BIN_ROOT", raising=False)
    fake_doe_root = tmp_path / "fake-doe-claude"
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(fake_doe_root))
    assert S._doe_coordinator_root() == fake_doe_root / "coordinator"


def test_doe_coordinator_root_returns_none_when_ladder_unresolvable(monkeypatch, tmp_path):
    monkeypatch.delenv("COORDINATOR_BIN_ROOT", raising=False)
    monkeypatch.delenv("REPO_DOE_CLAUDE", raising=False)
    monkeypatch.setattr(S, "coordinator_doe_root", lambda: None)
    assert S._doe_coordinator_root() is None


# test_p13_calls_probe_onboarding_currency_main_in_process (above) supersedes
# the retired subprocess-env-capture version of this coverage — P-13 now
# calls probe_onboarding_currency.main() in-process, so the resolved plugin
# root is asserted via os.environ inside the fake main, not a captured
# subprocess env kwarg.


# ---------------------------------------------------------------------------
# DR-079 prereq_probe.sh bridge repoint (P-15 / P-17) — _run_prereq_probe_function
# now dispatches func_name to coordinator_core.install.prereq_probe in-process
# instead of shelling out via `bash -c 'source prereq_probe.sh; func_name'`.
# The (state, ndjson) contract in {"missing", "source_failed", "ok"} is
# preserved byte-for-byte for probe_p15/probe_p17 — these tests pin that
# contract against the native dispatch path, and (like the P-9/P-11/P-13
# in-process repoints above) fail loud if a subprocess spawn creeps back in.
# ---------------------------------------------------------------------------


def test_run_prereq_probe_function_ok_dispatches_probe_all_in_process(tmp_path, monkeypatch):
    _block_subprocess(monkeypatch)
    monkeypatch.setattr(prereq_probe, "probe_all", lambda: ['{"a": 1}\n', '{"b": 2}\n'])
    state, ndjson = _run_prereq_probe_function(tmp_path, "_co_prereq_probe_all")
    assert state == "ok"
    assert ndjson == '{"a": 1}\n{"b": 2}\n'


def test_run_prereq_probe_function_ok_dispatches_shell_login_env_in_process(tmp_path, monkeypatch):
    _block_subprocess(monkeypatch)
    monkeypatch.setattr(prereq_probe, "probe_shell_login_env", lambda: '{"name": "shell_login_env"}\n')
    state, ndjson = _run_prereq_probe_function(tmp_path, "_co_probe_shell_login_env")
    assert state == "ok"
    assert ndjson == '{"name": "shell_login_env"}\n'


def test_run_prereq_probe_function_missing_for_unrecognized_func_name(tmp_path, monkeypatch):
    _block_subprocess(monkeypatch)
    state, ndjson = _run_prereq_probe_function(tmp_path, "_co_some_unported_function")
    assert state == "missing"
    assert ndjson == ""


def test_run_prereq_probe_function_source_failed_when_native_callable_raises(tmp_path, monkeypatch):
    _block_subprocess(monkeypatch)

    def _raise():
        raise RuntimeError("boom")

    monkeypatch.setattr(prereq_probe, "probe_all", _raise)
    state, ndjson = _run_prereq_probe_function(tmp_path, "_co_prereq_probe_all")
    assert state == "source_failed"
    # Review: code-reviewer (nit) — source_failed now carries the exception
    # type/message instead of a bare "" so a coding-defect signature mismatch
    # is visible in the sentinel JSON rather than folded into a bare "raised".
    assert ndjson == "RuntimeError: boom"


def test_run_prereq_probe_function_does_not_consult_scripts_lib_dir_presence(tmp_path, monkeypatch):
    """The retired bash bridge gated "missing" on prereq_probe.sh's presence
    at scripts_lib_dir; the native dispatch runs in-process regardless of
    whether that path exists at all — a nonexistent scripts_lib_dir must not
    itself produce "missing" for a recognized func_name."""
    _block_subprocess(monkeypatch)
    monkeypatch.setattr(prereq_probe, "probe_all", lambda: ["{}\n"])
    absent_dir = tmp_path / "does" / "not" / "exist"
    assert not absent_dir.exists()
    state, ndjson = _run_prereq_probe_function(absent_dir, "_co_prereq_probe_all")
    assert state == "ok"
    assert ndjson == "{}\n"


# --- P-15 ---


def test_p15_none_scripts_lib_dir_skips(monkeypatch):
    _block_subprocess(monkeypatch)
    assert probe_p15(None) == []


def test_p15_missing_state_skips(tmp_path, monkeypatch):
    _block_subprocess(monkeypatch)
    monkeypatch.setattr(
        S, "_run_prereq_probe_function", lambda _lib, _fn: ("missing", "")
    )
    assert probe_p15(tmp_path) == []


def test_p15_source_failed_reports_amber(tmp_path, monkeypatch):
    _block_subprocess(monkeypatch)
    monkeypatch.setattr(
        S, "_run_prereq_probe_function", lambda _lib, _fn: ("source_failed", "")
    )
    (note,) = probe_p15(tmp_path)
    assert note.severity == "amber"
    assert "prereq_probe" in note.message


def test_p15_empty_ndjson_reports_red(tmp_path, monkeypatch):
    _block_subprocess(monkeypatch)
    monkeypatch.setattr(S, "_run_prereq_probe_function", lambda _lib, _fn: ("ok", "   "))
    (note,) = probe_p15(tmp_path)
    assert note.severity == "red"
    assert "no output" in note.message


def test_p15_hard_fail_reports_red_with_names(tmp_path, monkeypatch):
    _block_subprocess(monkeypatch)
    ndjson = (
        '{"name": "git", "status": "fail", "severity": "hard"}\n'
        '{"name": "uv", "status": "warn", "severity": "advisory"}\n'
    )
    monkeypatch.setattr(S, "_run_prereq_probe_function", lambda _lib, _fn: ("ok", ndjson))
    (note,) = probe_p15(tmp_path)
    assert note.severity == "red"
    assert "git" in note.message
    assert "uv" not in note.message


def test_p15_all_pass_reports_nothing(tmp_path, monkeypatch):
    _block_subprocess(monkeypatch)
    ndjson = '{"name": "git", "status": "pass", "severity": "hard"}\n'
    monkeypatch.setattr(S, "_run_prereq_probe_function", lambda _lib, _fn: ("ok", ndjson))
    assert probe_p15(tmp_path) == []


# --- P-17 ---


def test_p17_none_scripts_lib_dir_skips(monkeypatch):
    _block_subprocess(monkeypatch)
    assert probe_p17(None) == []


def test_p17_missing_state_skips(tmp_path, monkeypatch):
    _block_subprocess(monkeypatch)
    monkeypatch.setattr(
        S, "_run_prereq_probe_function", lambda _lib, _fn: ("missing", "")
    )
    assert probe_p17(tmp_path) == []


def test_p17_source_failed_reports_amber(tmp_path, monkeypatch):
    _block_subprocess(monkeypatch)
    monkeypatch.setattr(
        S, "_run_prereq_probe_function", lambda _lib, _fn: ("source_failed", "")
    )
    (note,) = probe_p17(tmp_path)
    assert note.severity == "amber"
    assert "prereq_probe" in note.message


def test_p17_empty_ndjson_reports_red(tmp_path, monkeypatch):
    _block_subprocess(monkeypatch)
    monkeypatch.setattr(S, "_run_prereq_probe_function", lambda _lib, _fn: ("ok", "   "))
    (note,) = probe_p17(tmp_path)
    assert note.severity == "red"
    assert "no output" in note.message


def test_p17_fail_status_reports_red_with_detail(tmp_path, monkeypatch):
    _block_subprocess(monkeypatch)
    ndjson = '{"status": "fail", "detail": "orphaned ~/.local/bin"}\n'
    monkeypatch.setattr(S, "_run_prereq_probe_function", lambda _lib, _fn: ("ok", ndjson))
    (note,) = probe_p17(tmp_path)
    assert note.severity == "red"
    assert "orphaned ~/.local/bin" in note.message


def test_p17_pass_status_reports_nothing(tmp_path, monkeypatch):
    _block_subprocess(monkeypatch)
    ndjson = '{"status": "pass", "detail": "n/a"}\n'
    monkeypatch.setattr(S, "_run_prereq_probe_function", lambda _lib, _fn: ("ok", ndjson))
    assert probe_p17(tmp_path) == []


# --- End-to-end (no S._run_prereq_probe_function monkeypatch): probe_p15 /
# probe_p17 driven all the way through the real dispatch to the native
# prereq_probe module, proving the full chain never spawns a subprocess. ---


def test_p15_end_to_end_native_dispatch_no_subprocess(tmp_path, monkeypatch):
    _block_subprocess(monkeypatch)
    monkeypatch.setattr(
        prereq_probe,
        "probe_all",
        lambda: ['{"name": "git", "status": "pass", "severity": "hard"}\n'],
    )
    assert probe_p15(tmp_path) == []


def test_p17_end_to_end_native_dispatch_no_subprocess(tmp_path, monkeypatch):
    _block_subprocess(monkeypatch)
    monkeypatch.setattr(
        prereq_probe,
        "probe_shell_login_env",
        lambda: '{"status": "pass", "detail": "n/a"}\n',
    )
    assert probe_p17(tmp_path) == []


# ---------------------------------------------------------------------------
# Manifest resolution (doctor-probes.toml) — regression coverage for the
# b644d5a9 executable-surface migration (2026-07-22): doctor-probes.toml moved
# from DoE-claude's coordinator/bin/ into claude-klabauter's own coordinator/bin/,
# but the manifest's non-override default kept resolving through
# _doe_coordinator_root()'s DoE-root ladder, which no longer houses the file.
# Every real invocation with no DOCTOR_PROBES_MANIFEST / COORDINATOR_BIN_ROOT
# override hard-failed at the selector ("manifest not found") before any probe
# fired. These tests pin that the DEFAULT resolution is claude-klabauter-native and does
# NOT depend on DoE-claude's coordinator/bin containing the manifest.
# ---------------------------------------------------------------------------


def test_claude_klabauter_bin_root_resolves_this_repos_own_coordinator_bin():
    """`_claude_klabauter_bin_root()` is a pure `__file__`-relative resolution of THIS
    repo's own `coordinator/bin/` -- no subprocess, no env, no registry."""
    expected = Path(__file__).resolve().parents[3] / "coordinator" / "bin"
    assert S._claude_klabauter_bin_root() == expected
    assert (expected / "doctor-probes.toml").is_file(), (
        "sanity check: the real manifest must actually live where "
        "_claude_klabauter_bin_root() resolves to, or this test would pass vacuously"
    )


def test_default_manifest_path_ignores_doe_root_and_uses_claude_klabauter_bin_root(monkeypatch, tmp_path):
    """The regression pin: even when a `bin_dir_sibling` derived from a (fake,
    manifest-less) DoE root is supplied and COORDINATOR_BIN_ROOT is unset, the
    default manifest path must resolve to claude-klabauter's own coordinator/bin/ -- the
    exact failure mode this fix closes (manifest resolution silently depending
    on a DoE-claude coordinator/bin/ that no longer carries the file)."""
    monkeypatch.delenv("COORDINATOR_BIN_ROOT", raising=False)
    fake_doe_bin = tmp_path / "fake-doe-claude" / "coordinator" / "bin"
    fake_doe_bin.mkdir(parents=True)
    # Deliberately does NOT contain doctor-probes.toml -- proves the default
    # path never even looks here.
    resolved = S._default_manifest_path(fake_doe_bin)
    assert resolved == S._claude_klabauter_bin_root() / "doctor-probes.toml"
    assert resolved.is_file()


def test_default_manifest_path_none_bin_dir_sibling_still_resolves_claude_klabauter_native(monkeypatch):
    """No DoE root resolvable at all (bin_dir_sibling is None) must still
    resolve to claude-klabauter's own manifest, not degrade to None/unresolvable."""
    monkeypatch.delenv("COORDINATOR_BIN_ROOT", raising=False)
    resolved = S._default_manifest_path(None)
    assert resolved == S._claude_klabauter_bin_root() / "doctor-probes.toml"
    assert resolved.is_file()


def test_default_manifest_path_coordinator_bin_root_override_still_wins(monkeypatch, tmp_path):
    """COORDINATOR_BIN_ROOT test-isolation override keeps working exactly as
    before this fix: when set, it still redirects manifest resolution (via
    bin_dir_sibling), independent of claude-klabauter's own default."""
    fake_bin = tmp_path / "coordinator" / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "doctor-probes.toml").write_text("[[probe]]\n", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_BIN_ROOT", str(fake_bin))
    resolved = S._default_manifest_path(fake_bin)
    assert resolved == fake_bin / "doctor-probes.toml"


def test_run_triage_end_to_end_resolves_manifest_without_doe_root(monkeypatch, tmp_path):
    """End-to-end: `_run("triage", "")` must not hard-fail at the selector even
    when the DoE-root ladder is entirely unresolvable (REPO_DOE_CLAUDE unset,
    COORDINATOR_BIN_ROOT unset, coordinator_doe_root() patched to None) and
    DOCTOR_PROBES_MANIFEST is not set -- the exact repro from the bug report
    (`python3 -m coordinator_core.plugin_health.sentinel --triage` exiting 3
    with "selector error: inconclusive: manifest not found")."""
    monkeypatch.delenv("COORDINATOR_BIN_ROOT", raising=False)
    monkeypatch.delenv("REPO_DOE_CLAUDE", raising=False)
    monkeypatch.delenv("DOCTOR_PROBES_MANIFEST", raising=False)
    monkeypatch.setattr(S, "coordinator_doe_root", lambda: None)
    stdout_lines, stderr_lines, exit_code = S._run("triage", "")
    assert exit_code == 0, (
        f"expected the probe suite to actually run and produce a verdict "
        f"(_run's normal-path exit code is always 0, regardless of "
        f"GREEN/AMBER/RED verdict severity), not a selector failure; "
        f"got exit_code={exit_code} stderr={stderr_lines}"
    )
    assert not any("manifest not found" in line for line in stderr_lines)


# --- P-20 / bash-version-on-PATH gate ---


class _FakeCompleted:
    def __init__(self, returncode, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def test_p20_passes_when_first_bash_on_path_is_modern(monkeypatch):
    monkeypatch.setattr(S.shutil, "which", lambda name: "/opt/homebrew/bin/bash")
    monkeypatch.setattr(S.subprocess, "run", lambda *a, **k: _FakeCompleted(0, "5"))
    assert S.probe_p20() == []


def test_p20_reports_red_when_first_bash_on_path_is_old(monkeypatch):
    monkeypatch.setattr(S.shutil, "which", lambda name: "/bin/bash")
    monkeypatch.setattr(S.subprocess, "run", lambda *a, **k: _FakeCompleted(0, "3"))
    (note,) = S.probe_p20()
    assert note.id == "P-20"
    assert note.severity == "red"
    assert "/bin/bash" in note.message
    assert "version 3" in note.message


def test_p20_no_bash_on_path_is_a_clean_pass_on_windows(monkeypatch):
    _as_nt(monkeypatch)
    monkeypatch.setattr(S.shutil, "which", lambda name: None)

    def _boom(*_a, **_k):
        raise AssertionError("probe must not spawn when no bash resolved on PATH")

    monkeypatch.setattr(S.subprocess, "run", _boom)
    assert S.probe_p20() == []


def test_p20_no_bash_on_path_is_inconclusive_on_posix(monkeypatch):
    _as_posix(monkeypatch)
    monkeypatch.setattr(S.shutil, "which", lambda name: None)
    (note,) = S.probe_p20()
    assert note.severity == "amber"
    assert note.message.startswith("inconclusive(")


def test_p20_exec_failure_routes_to_inconclusive_never_a_fabricated_verdict(monkeypatch):
    monkeypatch.setattr(S.shutil, "which", lambda name: "/bin/bash")

    def _boom(*_a, **_k):
        raise OSError("boom")

    monkeypatch.setattr(S.subprocess, "run", _boom)
    (note,) = S.probe_p20()
    assert note.severity == "amber"
    assert note.message.startswith("inconclusive(")


def test_p20_unparsable_version_output_is_inconclusive(monkeypatch):
    monkeypatch.setattr(S.shutil, "which", lambda name: "/bin/bash")
    monkeypatch.setattr(S.subprocess, "run", lambda *a, **k: _FakeCompleted(0, "not-a-version"))
    (note,) = S.probe_p20()
    assert note.severity == "amber"
    assert note.message.startswith("inconclusive(")


# --- P-21 / durable coordinator-root pointer (DR-072) ---


def test_p21_passes_on_durable_pointer(monkeypatch, tmp_path):
    ml_dir = tmp_path / "settings-home" / "machine-local"
    ml_dir.mkdir(parents=True)
    (ml_dir / ".doe-root").write_text("/some/doe-claude\n", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home-unused"))
    assert S.probe_p21() == []


def test_p21_pass_on_durable_pointer_never_touches_legacy_fallback(monkeypatch, tmp_path):
    """Passing on the durable location must not require reaching the legacy
    fallback — the legacy candidate dir here is deliberately absent."""
    ml_dir = tmp_path / "settings-home" / "machine-local"
    ml_dir.mkdir(parents=True)
    (ml_dir / ".doe-root").write_text("/some/doe-claude\n", encoding="utf-8")
    claude_home = tmp_path / "claude-home-absent"  # deliberately does NOT exist
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    assert S.probe_p21() == []
    assert not claude_home.exists()


def test_p21_falls_back_to_legacy_pointer(monkeypatch, tmp_path):
    claude_home = tmp_path / "claude-home"
    (claude_home / ".claude").mkdir(parents=True)
    (claude_home / ".claude" / ".doe-root").write_text("/some/doe-claude\n", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home-empty"))
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    assert S.probe_p21() == []


def test_p21_fails_when_neither_pointer_present(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home-empty"))
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home-empty"))
    (note,) = S.probe_p21()
    assert note.id == "P-21"
    assert note.severity == "red"
    assert "DR-072" in note.message


# --- Subset-mode rendering: an inconclusive-only run must not say "Failing" ---


def test_subset_mode_labels_inconclusive_probe_separately_from_failing(monkeypatch, tmp_path):
    """Regression for the defect this whole memo reports: the pre-fix render
    unconditionally joined red_probes + amber_probes under the label
    'Failing:', so a probe that never ran (inconclusive) was reported as if
    it had failed. Stubs probe_p20/probe_p21 (already-wired, already-active)
    to return inconclusive(...) directly, so this exercises `_run`'s
    rendering logic against exactly that note shape."""
    monkeypatch.delenv("COORDINATOR_BIN_ROOT", raising=False)
    monkeypatch.delenv("REPO_DOE_CLAUDE", raising=False)
    monkeypatch.setattr(S, "coordinator_doe_root", lambda: None)
    monkeypatch.setenv("DOCTOR_PROBES_MANIFEST", str(
        Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "doctor-probes.toml"
    ))
    monkeypatch.setattr(S, "resolve_active_probes", lambda mode, arg, manifest_path: ["P-20", "P-21"])
    monkeypatch.setattr(
        S, "probe_p20", lambda: S._inconclusive("P-20", "no bash found on PATH — cannot judge its version")
    )
    monkeypatch.setattr(S, "probe_p21", lambda: S._inconclusive("P-21", "pointer files absent"))

    stdout_lines, stderr_lines, exit_code = S._run("triage", "")
    assert exit_code == 0
    joined = "\n".join(stdout_lines)
    assert "Failing:" not in joined
    assert "Inconclusive (did not run): P-20 P-21" in joined
    assert "verdict=AMBER" in joined


def test_subset_mode_separates_a_genuine_failure_from_a_concurrent_inconclusive(
    monkeypatch, tmp_path
):
    """Mixed run: P-1 genuinely fails, P-20 is stubbed inconclusive. The
    pre-fix render joined both under one 'Failing: P-1 P-20' line — this
    pins that a genuine failure keeps its own label and does not absorb an
    unrelated inconclusive id, in either direction."""
    monkeypatch.delenv("COORDINATOR_BIN_ROOT", raising=False)
    monkeypatch.delenv("REPO_DOE_CLAUDE", raising=False)
    monkeypatch.setattr(S, "coordinator_doe_root", lambda: None)
    monkeypatch.setenv("DOCTOR_PROBES_MANIFEST", str(
        Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "doctor-probes.toml"
    ))
    monkeypatch.setattr(S, "resolve_active_probes", lambda mode, arg, manifest_path: ["P-1", "P-20"])
    monkeypatch.setattr(
        S, "probe_p20", lambda: S._inconclusive("P-20", "no bash found on PATH — cannot judge its version")
    )

    # probe_p1 fires red when <settings-home>/machine-local/ is absent — point
    # settings-home at an empty tmp dir so that's exactly what it sees.
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home-empty"))
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)

    stdout_lines, stderr_lines, exit_code = S._run("triage", "")
    assert exit_code == 0
    joined = "\n".join(stdout_lines)
    assert "Failing: P-1" in joined
    assert "Inconclusive (did not run): P-20" in joined
    assert "Failing: P-1 P-20" not in joined
    assert "P-20" not in joined.split("Failing:", 1)[1].split("\n", 1)[0]


# ---------------------------------------------------------------------------
# G8 — one budget for every spawn this suite makes
# ---------------------------------------------------------------------------


def test_probe_spawn_budget_derives_from_the_central_budget():
    """DR-349 § Decision 1: a site does not carry a timeout, it derives its
    bound from the budget. Pinned as an identity, not a value, so lowering
    the central budget lowers this with it and no edit here is needed."""
    from coordinator_core.ipc import CEREMONY_BUDGET_SECS

    assert S._PROBE_SPAWN_BUDGET_SECS == CEREMONY_BUDGET_SECS


def test_no_probe_spawn_carries_a_hand_typed_timeout():
    """The recurrence guard, and the load-bearing half of this fix. Before
    it, ten spawns here carried a literal 15 or 30 — two of the three house
    numbers that make up 48% of every dial in the tree
    (docs/problems/2026-08-21-the-over-budget-timeout-hitlist.md § headline).
    A new probe that types its own number regrows the group, so fail on the
    literal rather than on the number being large."""
    source = Path(S.__file__).read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in source.splitlines()
        if re.search(r"timeout\s*=\s*[0-9]", line)
    ]
    assert offenders == [], (
        "sentinel.py spawns must derive from _PROBE_SPAWN_BUDGET_SECS, "
        f"never a literal: {offenders}"
    )


def test_whoami_unrunnable_reports_inconclusive_not_a_red_import_failure():
    """A spawn that never ran says nothing about whether coordinator_whoami
    imports. Collapsing that into False made P-5 assert RED about an import
    it never performed."""
    (note,) = S.probe_p5(None, "python 3.11 at /usr/bin/python3", Path("/sh"))
    assert note.severity == "amber"
    assert note.message.startswith("inconclusive(")
    assert "not importable" not in note.message

    (note,) = S.probe_p6(None, "python3", [], "python 3.11", Path("/nonexistent-sh"))
    assert note.message.startswith("inconclusive(")
    (note,) = S.probe_p6s(None, "python3", [], "python 3.11")
    assert note.message.startswith("inconclusive(")


def test_whoami_observed_import_failure_is_still_red():
    """Guardrail on the tri-state: False is an OBSERVED failure and must not
    have been softened into an inconclusive along with None."""
    (note,) = S.probe_p5(False, "python 3.11 at /usr/bin/python3", Path("/sh"))
    assert note.severity == "red"
    assert "not importable" in note.message


def test_p6_module_name_is_discovered_never_a_source_literal(tmp_path):
    """P-6's envelope module name must be read from disk, never written here.

    `coordinator_whoami` installs OUTSIDE the engine tree, so publish's
    depersonalization rewrote a hardcoded `coordinator_whoami.<repo>` reference
    and renamed nothing at the other end: the mirror every box resolves spawned
    an import that could not succeed. Discovery is what makes that
    unrepresentable, so both halves are pinned -- the enumeration works, and no
    per-plugin module name survives as a literal in the sentinel source.
    """
    pkg = tmp_path / "coordinator-whoami" / "coordinator_whoami"
    for name in ("some_plugin", "session", "schemas", "_private"):
        (pkg / name).mkdir(parents=True)
    for name in ("some_plugin", "session", "_private"):
        (pkg / name / "__main__.py").write_text("", encoding="utf-8")

    # `session` is a generic envelope with its own probe (P-6s); `schemas` is
    # not runnable; `_private` is not a public subpackage.
    assert S._whoami_plugin_modules(tmp_path) == ["some_plugin"]

    # An absent package is inconclusive-shaped (empty), never a fabricated pass.
    assert S._whoami_plugin_modules(tmp_path / "nope") == []

    source = Path(S.__file__).read_text(encoding="utf-8")
    assert "coordinator_whoami.example_retrieval_repo" not in source


def test_p22_absent_checker_on_a_published_engine_is_not_a_finding(tmp_path):
    """A published engine carries neither the checker nor the publish driver.

    Both are denied from the mirror on purpose, so an inconclusive here would
    be a permanent amber on every published box for a file that is absent by
    design. The pair is the discriminator: no driver means nothing to audit.
    """
    bin_dir = tmp_path / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    assert S.probe_p22(tmp_path) == []


def test_p22_driver_without_its_checker_is_inconclusive(tmp_path):
    """An authoring tree missing one of the pair is a real gap, not a skip."""
    bin_dir = tmp_path / "coordinator" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "publish.py").write_text("", encoding="utf-8")
    (note,) = S.probe_p22(tmp_path)
    assert note.severity == "amber"
    assert note.message.startswith("inconclusive(")

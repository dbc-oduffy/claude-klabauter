"""test_workday_start_health_probes.py — regression suite for
workday-start-health-probes.py, the day-start health-probe CLI ported off
Example-doctrine-repo's `coordinator/commands/workday-start.md` inline bash logic
(Steps 1.10.64, 1.10.9, 5.6).

Covers each subcommand's fail-loud/skip/pass-through branches: the
observer-sidecar orphan sweep's rc-branching + date extraction, the
Claude-klabauter-bin sentinel's dir/exec checks, and the ceremony-hook
capture-then-conditionally-print wrapper.

The former exec-bit-check subcommand (Step 1.10.8) and its tests were
retired 2026-07-28 along with `check-all-shebanged-exec-bits.py` — see
workday-start-health-probes.py's module docstring "Retired 2026-07-28" note.

Spec backlink: coordinator/bin/workday-start-health-probes.py
"""
from __future__ import annotations

import importlib.util
import os
import subprocess

import pytest

_REPO_ROOT = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"],
    cwd=os.path.dirname(os.path.abspath(__file__)),
    capture_output=True,
    text=True,
    check=True,
).stdout.strip()
_TARGET = os.path.join(_REPO_ROOT, "coordinator", "bin", "workday-start-health-probes.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("workday_start_health_probes", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod():
    # Fresh module per test — several tests monkeypatch module-level state
    # (_SCRIPT_DIR) and must not leak across tests.
    return _load_module()


# ---------------------------------------------------------------------------
# observer-sidecar-scan
# ---------------------------------------------------------------------------


def test_observer_sidecar_scan_missing_dir_is_silent_noop(mod, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = mod.cmd_observer_sidecar_scan([])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""


def test_observer_sidecar_scan_clean_dir_is_silent(mod, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    scan_dir = tmp_path / "archive" / "daily-summaries"
    scan_dir.mkdir(parents=True)
    rc = mod.cmd_observer_sidecar_scan([])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""


def test_observer_sidecar_scan_orphan_found_emits_warn_with_date(mod, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    scan_dir = tmp_path / "archive" / "daily-summaries"
    scan_dir.mkdir(parents=True)
    # Orphaned sidecar: observer file present, no matching main daily summary.
    (scan_dir / "2026-01-15-testmachine.observer.md").write_text(
        "## Strategic Review (Sonnet daily observer)\n\nfindings\n", encoding="utf-8"
    )

    rc = mod.cmd_observer_sidecar_scan([])
    captured = capsys.readouterr()

    assert rc == 1
    assert "[workday-start] WARN: orphaned observer sidecar(s)" in captured.err
    assert "2026-01-15" in captured.err
    assert "archive/daily-summaries" in captured.err


def test_observer_sidecar_scan_custom_dir_flag(mod, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    scan_dir = tmp_path / "custom-dir"
    scan_dir.mkdir()
    (scan_dir / "2026-02-02-testmachine.observer.md").write_text(
        "## Strategic Review (Sonnet daily observer)\n\nfindings\n", encoding="utf-8"
    )

    rc = mod.cmd_observer_sidecar_scan(["--dir", "custom-dir"])
    captured = capsys.readouterr()

    assert rc == 1
    assert "2026-02-02" in captured.err
    assert "custom-dir" in captured.err


def test_observer_sidecar_scan_dir_flag_missing_value(mod, capsys):
    rc = mod.cmd_observer_sidecar_scan(["--dir"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "--dir requires a value" in captured.err


def test_observer_sidecar_scan_unrecognized_argument(mod, capsys):
    rc = mod.cmd_observer_sidecar_scan(["--bogus"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "unrecognized argument" in captured.err


# ---------------------------------------------------------------------------
# claude-klabauter-bin-sentinel
# ---------------------------------------------------------------------------


def test_claude_klabauter_bin_sentinel_passes_on_real_bin_dir(mod):
    # workday-start-health-probes.py's own coordinator/bin dir carries a real,
    # executable archive-stamp-cli — the steady-state case.
    rc = mod.cmd_claude_klabauter_bin_sentinel([])
    assert rc == 0


def test_claude_klabauter_bin_sentinel_fails_on_missing_sentinel(mod, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mod, "_SCRIPT_DIR", str(tmp_path))
    rc = mod.cmd_claude_klabauter_bin_sentinel([])
    captured = capsys.readouterr()
    assert rc == 1
    assert "missing or not executable" in captured.err


def test_claude_klabauter_bin_sentinel_fails_on_missing_bin_dir(mod, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mod, "_SCRIPT_DIR", str(tmp_path / "does-not-exist"))
    rc = mod.cmd_claude_klabauter_bin_sentinel([])
    captured = capsys.readouterr()
    assert rc == 1
    assert "missing — wrong checkout or stale clone" in captured.err


def test_claude_klabauter_bin_sentinel_fails_on_non_executable_sentinel(mod, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mod, "_SCRIPT_DIR", str(tmp_path))
    sentinel = tmp_path / "archive-stamp-cli"
    sentinel.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    os.chmod(sentinel, 0o644)
    rc = mod.cmd_claude_klabauter_bin_sentinel([])
    captured = capsys.readouterr()
    assert rc == 1
    assert "missing or not executable" in captured.err


# ---------------------------------------------------------------------------
# ceremony-hook
# ---------------------------------------------------------------------------


def test_ceremony_hook_requires_argument(mod, capsys):
    rc = mod.cmd_ceremony_hook([])
    captured = capsys.readouterr()
    assert rc == 2
    assert "usage:" in captured.err


def test_ceremony_hook_prints_captured_stdout(mod, monkeypatch, capsys):
    class _FakeCompletedProcess:
        returncode = 0
        stdout = "Post-workday-start hook: ran <redacted> (exit 0)\n"

    def _fake_run(_cmd, **_kwargs):
        return _FakeCompletedProcess()

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    rc = mod.cmd_ceremony_hook(["workday-start"])
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.out == "Post-workday-start hook: ran <redacted> (exit 0)\n"
    assert captured.err == ""


def test_ceremony_hook_silent_when_no_output_configured(mod, monkeypatch, capsys):
    class _FakeCompletedProcess:
        returncode = 0
        stdout = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *_a, **_kw: _FakeCompletedProcess())

    rc = mod.cmd_ceremony_hook(["workday-start"])
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""


def test_ceremony_hook_warns_but_never_blocks_on_nonzero_exit(mod, monkeypatch, capsys):
    class _FakeCompletedProcess:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(mod.subprocess, "run", lambda *_a, **_kw: _FakeCompletedProcess())

    rc = mod.cmd_ceremony_hook(["workday-start"])
    captured = capsys.readouterr()

    # Always exits 0 — the wrapped hook must never block the calling ceremony.
    assert rc == 0
    assert "WARN: ceremony-hook exited non-zero" in captured.err


def test_ceremony_hook_runs_real_sibling_script_end_to_end(mod, tmp_path, monkeypatch):
    # No coordinator.local.md post-command configured in a bare tmp_path repo —
    # the real coordinator-ceremony-hook.py should degrade to empty stdout,
    # exit 0, matching the opt-in no-op contract.
    monkeypatch.chdir(tmp_path)
    rc = mod.cmd_ceremony_hook(["workday-start"])
    assert rc == 0


# ---------------------------------------------------------------------------
# main() dispatch
# ---------------------------------------------------------------------------


def test_main_no_args_shows_usage(mod, capsys):
    rc = mod.main([])
    captured = capsys.readouterr()
    assert rc == 2
    assert "usage:" in captured.err


def test_main_unknown_subcommand(mod, capsys):
    rc = mod.main(["bogus-subcommand"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "unknown subcommand" in captured.err


def test_main_help_flag_exits_zero(mod, capsys):
    rc = mod.main(["--help"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "usage:" in captured.err


def test_main_dispatches_claude_klabauter_bin_sentinel(mod):
    rc = mod.main(["claude-klabauter-bin-sentinel"])
    assert rc == 0


# ---------------------------------------------------------------------------
# working-repo-registration
#
# Spec backlink: example-doctrine-repo `coordinator/hooks/scripts/_engine_root.py`
# `_is_engine_working_repo()`; DR-132.
# ---------------------------------------------------------------------------


def _write_flat_registry(reg_dir, filename, key, value):
    reg_dir.mkdir(parents=True, exist_ok=True)
    path = reg_dir / filename
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(existing + f'\n"{key}" = "{value}"\n', encoding="utf-8")


def test_working_repo_registration_passes_when_registered_and_matching(mod, tmp_path, monkeypatch):
    reg_dir = tmp_path / "machine-local"
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    _write_flat_registry(
        reg_dir, "registry.local.toml",
        "engine.working_repos.claude_klabauter", mod._REPO_ROOT,
    )
    rc = mod.cmd_working_repo_registration([])
    assert rc == 0


def test_working_repo_registration_fails_when_absent(mod, tmp_path, monkeypatch, capsys):
    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    rc = mod.cmd_working_repo_registration([])
    captured = capsys.readouterr()
    assert rc == 1
    assert "is not registered" in captured.err
    assert "machine-local set engine.working_repos.claude_klabauter" in captured.err
    assert "python3 scripts/setup.py" in captured.err


def test_working_repo_registration_fails_on_mismatch(mod, tmp_path, monkeypatch, capsys):
    reg_dir = tmp_path / "machine-local"
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    _write_flat_registry(
        reg_dir, "registry.local.toml",
        "engine.working_repos.claude_klabauter", "/some/other/stale/checkout",
    )
    rc = mod.cmd_working_repo_registration([])
    captured = capsys.readouterr()
    assert rc == 1
    assert "registered as" in captured.err
    assert "/some/other/stale/checkout" in captured.err
    assert mod._REPO_ROOT in captured.err


def test_working_repo_registration_empty_string_treated_as_absent(mod, tmp_path, monkeypatch, capsys):
    reg_dir = tmp_path / "machine-local"
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    _write_flat_registry(reg_dir, "registry.local.toml", "engine.working_repos.claude_klabauter", "")
    rc = mod.cmd_working_repo_registration([])
    captured = capsys.readouterr()
    assert rc == 1
    assert "is not registered" in captured.err


def test_working_repo_registration_case_differing_registration_still_matches(
    mod, tmp_path, monkeypatch
):
    # A registered value that differs from _REPO_ROOT only in case (e.g. a
    # differently-cased drive letter or username segment written at a
    # different time/tool) must be treated as MATCHING on Windows semantics
    # -- os.path.normcase folds case there (no-op on POSIX, where this test
    # exercises normpath-equivalence only).
    reg_dir = tmp_path / "machine-local"
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    _write_flat_registry(
        reg_dir, "registry.local.toml",
        "engine.working_repos.claude_klabauter", mod._REPO_ROOT.upper(),
    )
    monkeypatch.setattr(mod, "_resolve_repo_identity", lambda: "claude-klabauter")
    monkeypatch.setattr(mod.os.path, "normcase", str.lower)

    rc = mod.cmd_working_repo_registration([])
    assert rc == 0


def test_working_repo_registration_nested_table_form(mod, tmp_path, monkeypatch):
    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir(parents=True)
    (reg_dir / "registry.toml").write_text(
        "[engine.working_repos]\n"
        f'claude_klabauter = "{mod._REPO_ROOT}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    rc = mod.cmd_working_repo_registration([])
    assert rc == 0


def test_working_repo_registration_missing_registry_dir_is_absent_not_a_read_error(mod, tmp_path, monkeypatch):
    # A registry dir that was never created (vs. one that exists but lacks
    # the key) is still a genuine, positively-determined "not registered" —
    # registry_get's own not-found-collapses-to-None contract makes this
    # indistinguishable from the plain-absent case, and correctly so: this
    # IS the exact "wiped/never-registered" scenario the probe exists to
    # catch, so it must fail (1), not silently pass.
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "does-not-exist"))
    rc = mod.cmd_working_repo_registration([])
    assert rc == 1


def test_working_repo_registration_unreadable_registry_read_degrades_to_pass(mod, tmp_path, monkeypatch):
    # Genuine read/resolution failure (as opposed to a validly-empty/absent
    # registry) -- registry_get itself raising unexpectedly -- must degrade
    # to pass (0) per the "never raise, never block the boot path" contract,
    # never propagate.
    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    def _raise(_key):
        raise OSError("simulated unreadable registry")

    monkeypatch.setattr(mod, "registry_get", _raise)
    rc = mod.cmd_working_repo_registration([])
    assert rc == 0


def test_working_repo_registration_never_spawns_a_subprocess(mod, tmp_path, monkeypatch):
    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    def _forbidden_run(*_args, **_kwargs):
        raise AssertionError("working-repo-registration must be zero-spawn")

    monkeypatch.setattr(mod.subprocess, "run", _forbidden_run)
    rc = mod.cmd_working_repo_registration([])
    # Absent registration in this isolated tmp registry -> 1 (a positively-
    # determined absent, not a subprocess-spawn failure) -- the assertion is
    # that no AssertionError from _forbidden_run propagated, i.e. no spawn
    # occurred anywhere in the call path.
    assert rc == 1


def test_main_dispatches_working_repo_registration(mod, tmp_path, monkeypatch):
    reg_dir = tmp_path / "machine-local"
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    _write_flat_registry(
        reg_dir, "registry.local.toml",
        "engine.working_repos.claude_klabauter", mod._REPO_ROOT,
    )
    rc = mod.main(["working-repo-registration"])
    assert rc == 0


# ---------------------------------------------------------------------------
# working-repo-registration — identity guard + --fix apply half
#
# Spec backlink: cross-repo/inbox/2026-08-05-example-doctrine-repo-em-klabauter-
# location-belongs-in-the-registry-not-a-pointer-file.md; DR-132;
# scripts/setup.py::resolve_repo_identity / register_claude_klabauter_root.
# ---------------------------------------------------------------------------


def test_resolve_repo_identity_returns_none_when_setup_py_missing(mod, monkeypatch):
    # Real (non-monkeypatched) _resolve_repo_identity, exercising the
    # `except Exception: return None` branch -- setup.py absent means
    # spec_from_file_location either returns a spec whose exec_module
    # raises (file not found), which the broad except must swallow.
    monkeypatch.setattr(mod, "_SETUP_PY_PATH", "/nonexistent/path/setup.py")
    assert mod._resolve_repo_identity() is None


def test_resolve_repo_identity_returns_none_when_setup_py_unparsable(
    mod, tmp_path, monkeypatch
):
    bad_setup = tmp_path / "setup.py"
    bad_setup.write_text("this is not ( valid python", encoding="utf-8")
    monkeypatch.setattr(mod, "_SETUP_PY_PATH", str(bad_setup))
    assert mod._resolve_repo_identity() is None


def test_resolve_repo_identity_returns_none_when_spec_is_none(mod, tmp_path, monkeypatch):
    # A path with no recognized loader (e.g. a directory) makes
    # importlib.util.spec_from_file_location return None directly --
    # distinct from the except-Exception branch above, per the module
    # docstring's "spec is None or spec.loader is None" guard.
    monkeypatch.setattr(mod, "_SETUP_PY_PATH", str(tmp_path))
    assert mod._resolve_repo_identity() is None


def test_working_repo_registration_klabauter_identity_is_silent_noop(
    mod, tmp_path, monkeypatch, capsys
):
    # A claude-klabauter clone ships this same coordinator/bin/ tree but must
    # never be told to register engine.working_repos.claude_klabauter --
    # writing it there would classify the published engine mirror as a
    # working repo (the exact inversion this identity gate exists to stop).
    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setattr(mod, "_resolve_repo_identity", lambda: "claude-klabauter")

    rc = mod.cmd_working_repo_registration([])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert captured.out == ""


def test_working_repo_registration_unresolvable_identity_is_silent_noop(
    mod, tmp_path, monkeypatch, capsys
):
    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setattr(mod, "_resolve_repo_identity", lambda: None)

    rc = mod.cmd_working_repo_registration([])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""


def test_working_repo_registration_claude_klabauter_identity_still_fails_on_bad_key(
    mod, tmp_path, monkeypatch, capsys
):
    # The identity guard must not swallow the real, positively-determined
    # case: on an actual claude-klabauter checkout with a mismatched/absent
    # key, the probe still fails (1) exactly as before this guard existed.
    reg_dir = tmp_path / "machine-local"
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    _write_flat_registry(
        reg_dir, "registry.local.toml",
        "engine.working_repos.claude_klabauter", "/some/other/stale/checkout",
    )
    monkeypatch.setattr(mod, "_resolve_repo_identity", lambda: "claude-klabauter")

    rc = mod.cmd_working_repo_registration([])
    captured = capsys.readouterr()
    assert rc == 1
    assert "registered as" in captured.err


def test_working_repo_registration_fix_klabauter_identity_refuses_to_write(
    mod, tmp_path, monkeypatch, capsys
):
    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setattr(mod, "_resolve_repo_identity", lambda: "claude-klabauter")

    def _forbidden_run(*_args, **_kwargs):
        raise AssertionError("--fix must not write from a non-claude-klabauter identity")

    monkeypatch.setattr(mod.subprocess, "run", _forbidden_run)
    rc = mod.cmd_working_repo_registration(["--fix"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "SKIP" in captured.out
    assert "claude-klabauter" in captured.out


def test_working_repo_registration_fix_is_idempotent_noop_when_already_correct(
    mod, tmp_path, monkeypatch, capsys
):
    reg_dir = tmp_path / "machine-local"
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    _write_flat_registry(
        reg_dir, "registry.local.toml",
        "engine.working_repos.claude_klabauter", mod._REPO_ROOT,
    )
    monkeypatch.setattr(mod, "_resolve_repo_identity", lambda: "claude-klabauter")

    def _forbidden_run(*_args, **_kwargs):
        raise AssertionError("--fix must not spawn when the key is already correct")

    monkeypatch.setattr(mod.subprocess, "run", _forbidden_run)
    rc = mod.cmd_working_repo_registration(["--fix"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "already correct" in captured.out


def test_working_repo_registration_fix_writes_via_machine_local_set(
    mod, tmp_path, monkeypatch, capsys
):
    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setattr(mod, "_resolve_repo_identity", lambda: "claude-klabauter")

    calls = []

    class _FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompleted()

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    rc = mod.cmd_working_repo_registration(["--fix"])
    captured = capsys.readouterr()
    assert rc == 0
    assert len(calls) == 1
    argv = calls[0]
    assert argv[-3:] == ["set", "engine.working_repos.claude_klabauter", mod._REPO_ROOT]
    assert "wrote" in captured.out
    assert mod._REPO_ROOT in captured.out


def test_working_repo_registration_fix_reports_write_failure(
    mod, tmp_path, monkeypatch, capsys
):
    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setattr(mod, "_resolve_repo_identity", lambda: "claude-klabauter")

    class _FakeFailed:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeFailed())
    rc = mod.cmd_working_repo_registration(["--fix"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "FAILED" in captured.err

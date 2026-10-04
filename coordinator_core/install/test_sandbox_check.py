"""
coordinator_core.install.test_sandbox_check — parity tests for
coordinator_core.install.sandbox_check.

Port of: install-sandbox-check.sh (DoE b5a4192c, 2026-07-20).

Independently re-derives expected behavior (Reporter counting semantics,
subprocess timeout/stdin-guard behavior, DoE-clone resolution precedence,
transport-vs-business exit-code contract) from the bash oracle's own
documented contract rather than re-asserting this port's own transcription.
Also drives a full :func:`run_all` pass against a hand-built minimal fake
DoE clone (NOT the real sibling coordinator-content-repo checkout — this validator's own
job is to exercise *other* install scripts via subprocess, so a synthetic
fixture with a stub ``claude-author`` is the honest independent oracle here,
not a copy of the real coordinator/bin/ tree).

Spec backlink: coordinator-content-repo:pln-doe-maximalist-execution-plugi-6d808d § W4.1
Port backlink: docs/plans/2026-07-16-clean-slate-residual-migration.md
    (BIG_PORT Wave C, item install-sandbox-check)
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import pytest

from coordinator_core.content_root import _LEGACY_POINTER, CONTENT_ROOT_KEY, POINTER_NAME
from coordinator_core.install import sandbox_check
from coordinator_core.install.sandbox_check import (
    CLONE_LAYOUT_FLAT,
    CLONE_LAYOUT_MAXIMALIST,
    CLONE_LAYOUT_UNKNOWN,
    Reporter,
    SandboxCheckTransportError,
    _cold_bare_path,
    _run,
    clone_layout,
    main,
    resolve_doe_clone,
    run_all,
)


def test_reporter_counts_pass_fail_independently_of_skip_and_info():
    r = Reporter()
    r.ok("a")
    r.ok("b")
    r.bad("c")
    r.skip("d")
    r.info("e")
    assert r.pass_count == 2
    assert r.fail_count == 1
    assert any(line.startswith("PASS: a") for line in r.lines)
    assert any(line.startswith("FAIL: c") for line in r.lines)
    assert any(line.startswith("SKIP: d") for line in r.lines)
    assert any(line.startswith("INFO: e") for line in r.lines)


def test_run_applies_stdin_devnull_so_a_stdin_reading_child_does_not_hang():
    cp = _run(["cat"], timeout=5)
    assert cp.returncode == 0
    assert cp.stdout == ""


def test_run_timeout_converts_to_synthetic_rc_124_not_an_exception():
    cp = _run(["sleep", "5"], timeout=1)
    assert cp.returncode == 124
    assert "TIMEOUT" in cp.stderr


def test_run_missing_executable_converts_to_synthetic_rc_127_not_an_exception():
    cp = _run(["/no/such/binary/exists-xyz"], timeout=5)
    assert cp.returncode == 127


# ---------------------------------------------------------------------------
# resolve_doe_clone — REPO_CONTENT_ROOT precedence over machine-local
# ---------------------------------------------------------------------------


def test_resolve_doe_clone_prefers_env_var(monkeypatch):
    monkeypatch.setenv("REPO_CONTENT_ROOT", "/tmp/fake-doe-clone")
    clone, resolved = resolve_doe_clone()
    assert resolved is True
    assert clone == "/tmp/fake-doe-clone"


def test_resolve_doe_clone_returns_unresolved_when_nothing_available(monkeypatch):
    monkeypatch.delenv("REPO_CONTENT_ROOT", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent-bin-dir-xyz")
    monkeypatch.setattr(sandbox_check, "migrate_legacy_config", lambda: None)
    monkeypatch.setattr(sandbox_check.machine_resolver, "registry_get", lambda key: "")
    clone, resolved = resolve_doe_clone()
    assert resolved is False
    assert clone == ""


def test_ac10_resolve_doe_clone_reads_seeded_registry_in_process_before_cli_spawn(monkeypatch, tmp_path):
    """AC10: with REPO_CONTENT_ROOT unset, a seeded scratch registry, and
    `_run` monkeypatched to raise, resolve_doe_clone() returns the registered
    root and (value, True) — the in-process registry rung must resolve
    without ever reaching the CLI-spawn fallback."""
    monkeypatch.delenv("REPO_CONTENT_ROOT", raising=False)

    def _boom(*a, **kw):
        raise AssertionError("resolve_doe_clone must not spawn the CLI when the registry rung resolves")

    monkeypatch.setattr("coordinator_core.install.sandbox_check._run", _boom)

    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "registry.toml").write_text(f'"{CONTENT_ROOT_KEY}" = "/scratch/coordinator-content-repo"\n')
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    clone, resolved = resolve_doe_clone()
    assert resolved is True
    assert clone == "/scratch/coordinator-content-repo"


def test_ac10_resolve_doe_clone_reaches_cli_spawn_when_registry_empty(monkeypatch, tmp_path):
    monkeypatch.delenv("REPO_CONTENT_ROOT", raising=False)

    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    monkeypatch.setattr(sandbox_check, "migrate_legacy_config", lambda: None)
    monkeypatch.setattr(sandbox_check.machine_resolver, "registry_get", lambda key: "")

    reached = {"called": False}

    def _fake_run(cmd, *a, **kw):
        reached["called"] = True
        raise AssertionError("simulated CLI failure")

    monkeypatch.setattr("coordinator_core.install.sandbox_check._run", _fake_run)
    monkeypatch.setattr(
        "coordinator_core.install.sandbox_check._which", lambda name: "/usr/bin/machine-local"
    )

    with pytest.raises(AssertionError, match="simulated CLI failure"):
        resolve_doe_clone()
    assert reached["called"] is True


def test_ac4b_resolve_doe_clone_normalizes_msys_mount_form_registry_value(monkeypatch, tmp_path):
    monkeypatch.delenv("REPO_CONTENT_ROOT", raising=False)

    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "registry.toml").write_text(f'"{CONTENT_ROOT_KEY}" = "/x/coordinator-content-repo"\n')
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    clone, resolved = resolve_doe_clone()
    assert resolved is True
    if os.name == "nt":
        assert clone == "C:/coordinator-content-repo"
    else:
        assert clone == "/x/coordinator-content-repo"


def test_resolve_doe_clone_migrates_an_upgrade_box_that_carries_only_the_legacy_pointer(
    monkeypatch, tmp_path
):
    """An upgrade box has a legacy-named pointer and no `repos.content_root`:
    the check must resolve it through `migrate_legacy_config`, never by
    reading the legacy name itself, and leave the new pointer behind."""
    monkeypatch.delenv("REPO_CONTENT_ROOT", raising=False)
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    settings_home = tmp_path / "settings-home"
    machine_local = settings_home / "machine-local"
    machine_local.mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    (machine_local / _LEGACY_POINTER).write_text("/legacy/clone\n", encoding="utf-8")
    monkeypatch.setattr("coordinator_core.install.sandbox_check._which", lambda name: None)

    clone, resolved = resolve_doe_clone()

    assert resolved is True
    assert Path(clone).as_posix().endswith("/legacy/clone")
    assert (machine_local / POINTER_NAME).read_text(encoding="utf-8").strip() == "/legacy/clone"


def test_run_all_raises_transport_error_when_sandbox_creation_fails(monkeypatch):
    def _boom(*a, **kw):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(tempfile, "mkdtemp", _boom)
    with pytest.raises(SandboxCheckTransportError):
        run_all()


def test_main_returns_dedicated_transport_code_on_sandbox_creation_failure(monkeypatch):
    def _boom(*a, **kw):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(tempfile, "mkdtemp", _boom)
    rc = main([])
    assert rc == 3


def test_main_help_exits_zero(capsys):
    rc = main(["--help"])
    assert rc == 0


def test_main_unknown_argument_exits_transport_code(monkeypatch):
    monkeypatch.delenv("REPO_CONTENT_ROOT", raising=False)
    rc = main(["--totally-unknown-flag"])
    assert rc == 3


# ---------------------------------------------------------------------------
# run_all -- graceful degrade when DoE clone is unresolved (FAMILY-I contract)
# ---------------------------------------------------------------------------


def test_run_all_never_raises_when_doe_clone_unresolved(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.install.sandbox_check.resolve_doe_clone", lambda: ("", False)
    )
    r, sandbox = run_all()
    assert not os.path.isdir(sandbox)
    assert r.fail_count >= 1
    assert any("repos.content_root not resolved" in line for line in r.lines)


def test_run_all_keep_sandbox_preserves_directory(monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.install.sandbox_check.resolve_doe_clone", lambda: ("", False)
    )
    r, sandbox = run_all(keep_sandbox=True)
    try:
        assert os.path.isdir(sandbox)
    finally:
        import shutil

        shutil.rmtree(sandbox, ignore_errors=True)


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_doe_clone(tmp_path: Path) -> Path:
    clone = tmp_path / "fake-doe-clone"
    (clone / ".git").mkdir(parents=True)
    (clone / "coordinator" / "bin").mkdir(parents=True)
    (clone / "coordinator" / "lib").mkdir(parents=True)
    (clone / "coordinator" / "hooks").mkdir(parents=True)
    (clone / "coordinator" / "hooks" / "hooks.json").write_text('{"hooks": {}}', encoding="utf-8")
    (clone / "coordinator" / "templates" / "bin").mkdir(parents=True)
    (clone / "coordinator" / "templates" / "bin" / "_machine_local.py").write_text(
        "# stand-in for the real machine-local registry reader\n", encoding="utf-8"
    )
    wrapper = clone / "coordinator" / "bin" / "claude-author.py"
    _write_executable(
        wrapper,
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"clone = {str(clone)!r}\n"
        "if len(sys.argv) > 1 and sys.argv[1] == '--dry-run':\n"
        "    print(f'exec claude --plugin-dir {clone}/coordinator')\n"
        "    sys.exit(0)\n"
        "sys.exit(1)\n",
    )
    return clone


def test_run_all_full_pass_against_synthetic_fake_clone_no_crash(fake_doe_clone: Path, monkeypatch):
    monkeypatch.setenv("REPO_CONTENT_ROOT", str(fake_doe_clone))
    coordinator_root = str(fake_doe_clone / "coordinator")

    r, sandbox = run_all(coordinator_root_override=coordinator_root)

    assert not os.path.isdir(sandbox)
    assert any("clone present" in line for line in r.lines)
    assert any("clone's coordinator/ dir present" in line for line in r.lines)
    assert any("claude-author wrapper source present" in line for line in r.lines)
    assert any("claude-author --dry-run emitted exec line with --plugin-dir" in line for line in r.lines)
    assert any(
        "claude-author --dry-run exec line references clone's coordinator dir" in line for line in r.lines
    )
    assert any("cold-env content-root pointer seeded" in line for line in r.lines)
    assert any(
        "AC5: resolve_coordinator_clone.resolve_content_root()" in line and "returned expected" in line
        for line in r.lines
    )
    # The synthetic sandbox has no positive hook-generation marker, so
    # gen_settings_hooks DECLINES to write — reported as a SKIP, not a FAIL.
    # Until 2026-08-14 this was asserted as "settings.json hooks array empty",
    # i.e. the validator called the generator's correct refusal a defect.
    assert any(
        "hook seed declined by the generator" in line and "skipped" in line
        for line in r.lines
    )
    assert not any("settings.json hooks array empty" in line for line in r.lines)
    assert r.pass_count > 0


def test_tier2_boot_check_names_the_sentinel_a_writer_actually_writes():
    from coordinator_core.ops.session.guard_hook_generation_self_probe import _SENTINEL_NAME

    banner = sandbox_check._TIER2_BANNER
    assert _SENTINEL_NAME in banner
    assert ".session-sentinel" not in banner


def test_unevaluable_is_counted_apart_from_pass_fail_and_skip():
    """The channel exists so "not evaluated" can never be read as "passed".
    A SKIP is pass-equivalent and exits 0; UNEVALUABLE must not be."""
    r = Reporter()
    r.ok("a")
    r.skip("b")
    r.unevaluable("c")
    assert (r.pass_count, r.fail_count, r.unevaluable_count) == (1, 0, 1)
    assert any(line.startswith("UNEVALUABLE: c") for line in r.lines)


def test_main_returns_dedicated_code_two_when_nothing_failed_but_something_was_unevaluable(monkeypatch):
    def _fake_run_all(**_kw):
        r = Reporter()
        r.ok("evaluated")
        r.unevaluable("subject absent on this host")
        return r, "/nonexistent-sandbox"

    monkeypatch.setattr(sandbox_check, "run_all", _fake_run_all)
    assert main([]) == 2


def test_main_fail_outranks_unevaluable_so_a_real_defect_is_never_masked(monkeypatch):
    def _fake_run_all(**_kw):
        r = Reporter()
        r.bad("real defect")
        r.unevaluable("subject absent on this host")
        return r, "/nonexistent-sandbox"

    monkeypatch.setattr(sandbox_check, "run_all", _fake_run_all)
    assert main([]) == 1


def test_clone_layout_classifies_maximalist_flat_and_neither(tmp_path: Path):
    maximalist = tmp_path / "maximalist"
    (maximalist / "coordinator").mkdir(parents=True)
    assert clone_layout(str(maximalist)) == CLONE_LAYOUT_MAXIMALIST

    flat = tmp_path / "flat"
    (flat / "hooks").mkdir(parents=True)
    (flat / "skills").mkdir()
    assert clone_layout(str(flat)) == CLONE_LAYOUT_FLAT

    neither = tmp_path / "neither"
    neither.mkdir()
    assert clone_layout(str(neither)) == CLONE_LAYOUT_UNKNOWN
    assert clone_layout(str(tmp_path / "absent")) == CLONE_LAYOUT_UNKNOWN
    assert clone_layout("") == CLONE_LAYOUT_UNKNOWN


@pytest.fixture
def flat_mirror_clone(tmp_path: Path) -> Path:
    """The PUBLISHED FLAT MIRROR shape — what `repos.content_root` resolves to on
    a marketplace-served install (every cloud container). Its surfaces sit at
    the clone root, so no `<clone>/coordinator/...` path exists by construction.
    The pre-existing `fake_doe_clone` fixture only ever built the maximalist
    shape, which is why no test could fail on this divergence."""
    clone = tmp_path / "flat-published-mirror"
    (clone / ".git").mkdir(parents=True)
    (clone / "hooks").mkdir()
    (clone / "hooks" / "hooks.json").write_text('{"hooks": {}}', encoding="utf-8")
    (clone / "skills").mkdir()
    return clone


def test_flat_mirror_clone_reports_unevaluable_not_a_pass_equivalent_skip(
    flat_mirror_clone: Path, monkeypatch
):
    monkeypatch.setenv("REPO_CONTENT_ROOT", str(flat_mirror_clone))

    r, _sandbox = run_all()

    assert any(
        line.startswith("UNEVALUABLE") and "clone's coordinator/ dir absent" in line
        for line in r.lines
    ), r.lines
    assert not any("W4.2 cutover not yet completed" in line for line in r.lines)
    assert r.unevaluable_count > 0


def test_flat_mirror_clone_failures_name_the_layout_as_the_cause(
    flat_mirror_clone: Path, monkeypatch
):
    """A FAIL whose expected path form cannot exist on this clone must say so.
    Verdict is deliberately UNCHANGED (still FAIL) — only the diagnosis is
    fixed; turning these green on a flat host would be the silent oracle
    rewrite this check exists to prevent."""
    monkeypatch.setenv("REPO_CONTENT_ROOT", str(flat_mirror_clone))

    r, _sandbox = run_all()

    failures = [line for line in r.lines if line.startswith("FAIL")]
    assert failures, r.lines
    assert any("FLAT published-mirror layout" in line for line in failures), failures
    assert any("--coordinator-root" in line for line in failures), failures
    assert r.fail_count > 0


def test_flat_mirror_f8_setup_names_the_missing_oracle_input_not_a_clone_build_failure(
    flat_mirror_clone: Path, monkeypatch
):
    monkeypatch.setenv("REPO_CONTENT_ROOT", str(flat_mirror_clone))

    r, _sandbox = run_all()

    assert any(
        line.startswith("UNEVALUABLE")
        and "F8 setup: no hooks.json to seed" in line
        and "hooks.json" in line
        for line in r.lines
    ), r.lines
    assert not any("publish-repo-shaped sandbox clone build failed" in line for line in r.lines)


def test_cold_bare_path_is_host_shaped_not_posix_only(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    nt_path = _cold_bare_path()
    assert "System32" in nt_path
    assert "/usr/bin" not in nt_path

    monkeypatch.setattr(os, "name", "posix")
    posix_path = _cold_bare_path()
    assert "/usr/bin" in posix_path and "/bin" in posix_path
    assert "System32" not in posix_path


def test_cold_tier_probes_the_binary_the_resolver_actually_spawns(
    fake_doe_clone: Path, monkeypatch
):
    monkeypatch.setenv("REPO_CONTENT_ROOT", str(fake_doe_clone))

    r, _sandbox = run_all(coordinator_root_override=str(fake_doe_clone / "coordinator"))

    cold_rows = [line for line in r.lines if "cold PATH:" in line]
    assert cold_rows, r.lines
    assert all("machine-local" in line for line in cold_rows), cold_rows

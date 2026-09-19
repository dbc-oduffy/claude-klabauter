"""
coordinator_core.install.test_sandbox_check — parity tests for
coordinator_core.install.sandbox_check.

Port of: install-sandbox-check.sh (DoE b5a4192c, 2026-07-20).

Independently re-derives expected behavior (Reporter counting semantics,
subprocess timeout/stdin-guard behavior, DoE-clone resolution precedence,
transport-vs-business exit-code contract) from the bash oracle's own
documented contract rather than re-asserting this port's own transcription.
Also drives a full :func:`run_all` pass against a hand-built minimal fake
DoE clone (NOT the real sibling DoE-claude checkout — this validator's own
job is to exercise *other* install scripts via subprocess, so a synthetic
fixture with a stub ``claude-doe`` is the honest independent oracle here,
not a copy of the real coordinator/bin/ tree).

Spec backlink: DoE-claude:pln-doe-maximalist-execution-plugi-6d808d § W4.1
Port backlink: docs/plans/2026-07-16-clean-slate-residual-migration.md
    (BIG_PORT Wave C, item install-sandbox-check)
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

from coordinator_core.install import sandbox_check
from coordinator_core.install.sandbox_check import (
    CLONE_LAYOUT_FLAT,
    CLONE_LAYOUT_MAXIMALIST,
    CLONE_LAYOUT_UNKNOWN,
    Reporter,
    SandboxCheckTransportError,
    _cold_bare_path,
    _host_home,
    _run,
    clone_layout,
    main,
    resolve_doe_clone,
    run_all,
)


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _run — addendum A2 (timeout + stdin=DEVNULL), A4 (no-console on nt)
# ---------------------------------------------------------------------------


def test_run_applies_stdin_devnull_so_a_stdin_reading_child_does_not_hang():
    # `cat` with no args reads stdin until EOF; DEVNULL supplies immediate EOF.
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
# resolve_doe_clone — REPO_DOE_CLAUDE precedence over machine-local
# ---------------------------------------------------------------------------


def test_resolve_doe_clone_prefers_env_var(monkeypatch):
    monkeypatch.setenv("REPO_DOE_CLAUDE", "/tmp/fake-doe-clone")
    clone, resolved = resolve_doe_clone()
    assert resolved is True
    assert clone == "/tmp/fake-doe-clone"


def test_resolve_doe_clone_returns_unresolved_when_nothing_available(monkeypatch):
    monkeypatch.delenv("REPO_DOE_CLAUDE", raising=False)
    monkeypatch.setenv("PATH", "/nonexistent-bin-dir-xyz")
    clone, resolved = resolve_doe_clone()
    assert resolved is False
    assert clone == ""


def test_ac10_resolve_doe_clone_reads_seeded_registry_in_process_before_cli_spawn(monkeypatch, tmp_path):
    """AC10: with REPO_DOE_CLAUDE unset, a seeded scratch registry, and
    `_run` monkeypatched to raise, resolve_doe_clone() returns the registered
    root and (value, True) — the in-process registry rung must resolve
    without ever reaching the CLI-spawn fallback."""
    monkeypatch.delenv("REPO_DOE_CLAUDE", raising=False)

    def _boom(*a, **kw):
        raise AssertionError("resolve_doe_clone must not spawn the CLI when the registry rung resolves")

    monkeypatch.setattr("coordinator_core.install.sandbox_check._run", _boom)

    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "registry.toml").write_text('"repos.doe_claude" = "/scratch/DoE-claude"\n')
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    clone, resolved = resolve_doe_clone()
    assert resolved is True
    assert clone == "/scratch/DoE-claude"


def test_ac10_resolve_doe_clone_reaches_cli_spawn_when_registry_empty(monkeypatch, tmp_path):
    """AC10 (second half): with the registry empty, `_run` is still reached
    — the CLI-spawn fallback rung is preserved, unchanged."""
    monkeypatch.delenv("REPO_DOE_CLAUDE", raising=False)

    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

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
    """The in-process registry rung's return value is normalized (drive/MSYS
    form) via `native_path_form` — the same repair
    `gen_doe_root_pointer.py :: _resolve_doe_root` wraps both of its own
    rungs in — rather than the raw stored string. Mount-form (`/x/...`) is
    the shape this repair actually acts on; gated to `os.name == "nt"`, a
    no-op on POSIX."""
    monkeypatch.delenv("REPO_DOE_CLAUDE", raising=False)

    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir(parents=True, exist_ok=True)
    (reg_dir / "registry.toml").write_text('"repos.doe_claude" = "/x/DoE-claude"\n')
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))

    clone, resolved = resolve_doe_clone()
    assert resolved is True
    if os.name == "nt":
        assert clone == "X:/DoE-claude"  # abs-path-ok: synthetic MSYS-normalization fixture value, not a real path
    else:
        assert clone == "/x/DoE-claude"


# ---------------------------------------------------------------------------
# run_all -- transport-failure path (addendum rule 3b, dedicated exit code)
# ---------------------------------------------------------------------------


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
    assert rc == 3  # dedicated transport code -- must NOT collide with business 0/1


def test_main_help_exits_zero(capsys):
    rc = main(["--help"])
    assert rc == 0


def test_main_unknown_argument_exits_transport_code(monkeypatch):
    monkeypatch.delenv("REPO_DOE_CLAUDE", raising=False)
    rc = main(["--totally-unknown-flag"])
    assert rc == 3


# ---------------------------------------------------------------------------
# run_all -- graceful degrade when DoE clone is unresolved (FAMILY-I contract)
# ---------------------------------------------------------------------------


def test_run_all_never_raises_when_doe_clone_unresolved(monkeypatch):
    """Mirrors the bash oracle's own graceful-skip design (lines 79-98):
    an unresolved clone must degrade every clone-dependent check to SKIP,
    never crash the harness. The single expected FAIL is the oracle's own
    'repos.doe_claude not resolved' assertion -- everything downstream must
    still complete without an uncaught exception."""
    monkeypatch.setattr(
        "coordinator_core.install.sandbox_check.resolve_doe_clone", lambda: ("", False)
    )
    r, sandbox = run_all()
    assert not os.path.isdir(sandbox)  # cleaned up (default keep_sandbox=False)
    assert r.fail_count >= 1  # the "not resolved" assertion itself
    assert any("repos.doe_claude not resolved" in line for line in r.lines)


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


# ---------------------------------------------------------------------------
# run_all -- full pass against a synthetic fake DoE clone (independent fixture)
# ---------------------------------------------------------------------------


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_doe_clone(tmp_path: Path) -> Path:
    """A minimal synthetic DoE-clone shape: .git dir, coordinator/ dir, a
    stub claude-doe wrapper that emits the exact contract line the
    validator's own checks 7/7b assert on, and a stub shim template (the
    one on-disk artifact ``gen_claude_doe_shim`` still requires, since it
    has no default template path of its own -- see that module's
    docstring). The DoE-side ``.sh`` bridges themselves (gen-doe-root-
    pointer.sh, gen-claude-doe-shim.sh, gen-settings-hooks.sh,
    resolve-coordinator-clone.sh) are deliberately ABSENT -- the port no
    longer reads any of them (native in-process calls replace the former
    subprocess-exec), so their absence must NOT block the checks that used
    to gate on ``os.path.isfile(<script>)``; only the empty ``hooks.json``
    (no hook entries to seed) and this sandbox's lack of a real
    ``machine-local``/registry keep a few checks legitimately RED."""
    clone = tmp_path / "fake-doe-clone"
    (clone / ".git").mkdir(parents=True)
    (clone / "coordinator" / "bin").mkdir(parents=True)
    (clone / "coordinator" / "lib").mkdir(parents=True)
    (clone / "coordinator" / "hooks").mkdir(parents=True)
    (clone / "coordinator" / "hooks" / "hooks.json").write_text('{"hooks": {}}', encoding="utf-8")
    # Plugin-root marker a real dev clone always carries (nested under
    # coordinator/) -- coordinator_root._resolve_plugin_root_for_machine_local
    # probes for exactly this file to decide the DoE-side content root is
    # coordinator/, not the clone root itself. Without it the resolved
    # repos.doe_claude root matches neither shape and gen_doe_root_pointer
    # fails closed before ever reaching this fixture's other stand-ins.
    (clone / "coordinator" / "templates" / "bin").mkdir(parents=True)
    (clone / "coordinator" / "templates" / "bin" / "_machine_local.py").write_text(
        "# stand-in for the real machine-local registry reader\n", encoding="utf-8"
    )
    (clone / "coordinator" / "templates" / "shell").mkdir(parents=True)
    (clone / "coordinator" / "templates" / "shell" / "claude-doe-shim.sh.tmpl").write_text(
        # Minimal stand-in for the real DoE template, in its DR-087 shape:
        # the pointer is read at CALL time and handed to claude-doe through
        # the explicit `--doe-root` argv seam, and REPO_DOE_CLAUDE is never
        # exported (DR-087 demoted the pointer mirror out of rung-1
        # authority). Variable expansion only -- no hardcoded machine path.
        "claude() {\n"
        '  _r="$(cat "${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings/machine-local/.doe-root" 2>/dev/null)"\n'
        '  if [ -z "$_r" ]; then\n'
        '    _r="$(cat "${CLAUDE_HOME:-$HOME}/.claude/.doe-root" 2>/dev/null)"\n'
        "  fi\n"
        '  command claude-doe --doe-root "$_r" "$@"\n'
        "}\n",
        encoding="utf-8",
    )

    # Python, not sh: the real claude-doe was ported from bash to python3
    # (DoE-claude commit), and sandbox_check.py invokes it via
    # ``[sys.executable, wrapper_path, ...]`` directly (not shebang-exec
    # through a shell), so this fixture must be python source for that
    # invocation to succeed.
    wrapper = clone / "coordinator" / "bin" / "claude-doe.py"
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
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(fake_doe_clone))
    coordinator_root = str(fake_doe_clone / "coordinator")

    r, sandbox = run_all(coordinator_root_override=coordinator_root)

    assert not os.path.isdir(sandbox)
    # clone + coordinator/ dir + claude-doe wrapper checks must PASS.
    assert any("clone present" in line for line in r.lines)
    assert any("clone's coordinator/ dir present" in line for line in r.lines)
    assert any("claude-doe wrapper source present" in line for line in r.lines)
    assert any("claude-doe --dry-run emitted exec line with --plugin-dir" in line for line in r.lines)
    assert any(
        "claude-doe --dry-run exec line references clone's coordinator dir" in line for line in r.lines
    )
    # Native in-process pointer/shim/resolver calls must succeed even though
    # the DoE .sh bridges are absent from this fixture (the whole point of
    # the port -- no dependency on those files existing).
    assert any("gen_doe_root_pointer.main() exited 0 against sandbox" in line for line in r.lines)
    assert any(".doe-root content matches registry repos.doe_claude" in line for line in r.lines)
    assert any("gen_claude_doe_shim.main() exited 0 against sandbox" in line for line in r.lines)
    assert any("claude-doe-shim.sh defines a claude() function" in line for line in r.lines)
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
    # AC2 is derived against the DR-087 argv seam: a conforming shim hands
    # claude-doe `--doe-root <pointer>` and exports nothing. Both rows PASS
    # here, so an AC2 re-derived against REPO_DOE_CLAUDE would go RED on a
    # correct install — which is the failure this fixture exists to catch.
    # `run_all`'s own AC2 cold-shell leg sources a POSIX `.sh` shim under a
    # hand-built `/usr/bin:/bin` PATH — not applicable on Windows (see
    # sandbox_check.py's own `os.name == "nt"` SKIP branch there), so neither
    # PASS row is ever emitted on this host.
    if os.name != "nt":
        assert any(
            "AC2 cold-shell: claude-doe --doe-root resolved from pointer alone" in line for line in r.lines
        )
        assert any("AC2 cold-shell: shim left REPO_DOE_CLAUDE unset" in line for line in r.lines)
    assert not any(line.startswith("FAIL") and "AC2" in line for line in r.lines)
    assert r.pass_count > 0  # and the fixture's own present pieces PASS


def test_ac2_fails_on_pre_dr087_shim_that_promotes_the_pointer_mirror(fake_doe_clone: Path, monkeypatch):
    """The shape DR-087 retired: export the pointer as rung-1
    ``REPO_DOE_CLAUDE`` and invoke claude-doe with no ``--doe-root``. AC2 must
    call BOTH halves out — a missing argv seam and a promoted mirror."""
    if os.name == "nt":
        pytest.skip(
            "AC2 cold-shell sources a POSIX .sh shim under a hand-built "
            "/usr/bin:/bin PATH -- run_all()'s own AC2 leg is a no-op SKIP "
            "on Windows (sandbox_check.py's os.name == 'nt' branch), so "
            "this fixture's FAIL lines can never appear here"
        )
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(fake_doe_clone))
    tmpl = fake_doe_clone / "coordinator" / "templates" / "shell" / "claude-doe-shim.sh.tmpl"
    tmpl.write_text(
        'export REPO_DOE_CLAUDE="$(cat "${CLAUDE_HOME:-$HOME}'
        '/.coordinator-claude-settings/machine-local/.doe-root" 2>/dev/null)"\n'
        "claude() {\n"
        '  command claude-doe "$@"\n'
        "}\n",
        encoding="utf-8",
    )

    r, _sandbox = run_all(coordinator_root_override=str(fake_doe_clone / "coordinator"))

    assert any(
        line.startswith("FAIL") and "DR-087 requires the explicit `--doe-root" in line for line in r.lines
    )
    assert any(
        line.startswith("FAIL") and "DR-087 demoted the pointer mirror" in line for line in r.lines
    )


def test_tier2_boot_check_names_the_sentinel_a_writer_actually_writes():
    """The Tier 2 manual gate used to send the operator to
    `~/.claude/.session-sentinel`, which nothing in this repo has ever
    written — an operator in a live session whose hooks demonstrably fired
    went looking for a file that will never appear (2026-08-22).

    Pinned against the writer's own constant rather than a copied literal:
    a rename there must break this, not silently strand the instruction
    again."""
    from coordinator_core.ops.session.guard_hook_generation_self_probe import _SENTINEL_NAME

    banner = sandbox_check._TIER2_BANNER
    assert _SENTINEL_NAME in banner
    assert ".session-sentinel" not in banner


# ---------------------------------------------------------------------------
# Host-dependence: an assertion whose subject is absent must not report a PASS
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# clone_layout — the maximalist/flat divergence, classified by positive marker
# ---------------------------------------------------------------------------


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
    """The PUBLISHED FLAT MIRROR shape — what `repos.doe_claude` resolves to on
    a marketplace-served install (every cloud container). Its surfaces sit at
    the clone root, so no `<clone>/coordinator/...` path exists by construction.
    The pre-existing `fake_doe_clone` fixture only ever built the maximalist
    shape, which is why no test could fail on this divergence."""
    clone = tmp_path / "flat-published-mirror"
    (clone / ".git").mkdir(parents=True)
    (clone / "hooks").mkdir()
    (clone / "hooks" / "hooks.json").write_text('{"hooks": {}}', encoding="utf-8")
    (clone / "skills").mkdir()
    (clone / "templates" / "shell").mkdir(parents=True)
    (clone / "templates" / "shell" / "claude-doe-shim.sh.tmpl").write_text(
        "claude() { command claude-doe --doe-root \"$_r\" \"$@\"; }\n", encoding="utf-8"
    )
    return clone


def test_flat_mirror_clone_reports_unevaluable_not_a_pass_equivalent_skip(
    flat_mirror_clone: Path, monkeypatch
):
    """The coordinator/-absent row used to be a SKIP reading "W4.2 cutover not
    yet completed" — pass-equivalent, and false on every flat clone. A run that
    evaluated almost none of its maximalist subject must not exit 0."""
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(flat_mirror_clone))

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
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(flat_mirror_clone))

    r, _sandbox = run_all()

    failures = [line for line in r.lines if line.startswith("FAIL")]
    assert failures, r.lines
    assert any("FLAT published-mirror layout" in line for line in failures), failures
    assert any("--coordinator-root" in line for line in failures), failures
    # A real FAIL must still drive rc=1, never be softened to the unevaluable code.
    assert r.fail_count > 0


def test_flat_mirror_f8_setup_names_the_missing_oracle_input_not_a_clone_build_failure(
    flat_mirror_clone: Path, monkeypatch
):
    """The F8 fixture seeds itself from `<clone>/coordinator/hooks/hooks.json`.
    When that input is absent the sandbox builder is not at fault, and the old
    single FAIL message sent the reader hunting a builder bug."""
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(flat_mirror_clone))

    r, _sandbox = run_all()

    assert any(
        line.startswith("UNEVALUABLE")
        and "F8 setup: no hooks.json to seed" in line
        and "hooks.json" in line
        for line in r.lines
    ), r.lines
    assert not any("publish-repo-shaped sandbox clone build failed" in line for line in r.lines)


# ---------------------------------------------------------------------------
# _host_home — HOME is the POSIX spelling only
# ---------------------------------------------------------------------------


def test_host_home_falls_back_to_userprofile_when_home_is_unset(monkeypatch):
    """Windows does not set HOME. `os.environ.get("HOME", "")` returned "" there,
    and the hardcoded-machine-path assertion guarded on that truthiness — a
    check that could not fail on a first-class platform."""
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", r"C:\Users\sandbox-probe")
    assert _host_home() == r"C:\Users\sandbox-probe"


def test_host_home_prefers_home_when_both_spellings_are_present(monkeypatch):
    monkeypatch.setenv("HOME", "/home/posix-probe")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\win-probe")
    assert _host_home() == "/home/posix-probe"


def test_hardcoded_home_row_is_unevaluable_when_no_home_resolves(
    fake_doe_clone: Path, monkeypatch
):
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(fake_doe_clone))
    monkeypatch.setattr(sandbox_check, "_host_home", lambda: "")

    r, _sandbox = run_all(coordinator_root_override=str(fake_doe_clone / "coordinator"))

    assert any(
        line.startswith("UNEVALUABLE") and "hardcoded-home check" in line for line in r.lines
    ), r.lines
    assert not any("no hardcoded machine path" in line and line.startswith("PASS") for line in r.lines)


def test_hardcoded_path_pattern_catches_windows_and_unc_shapes_on_every_host(
    fake_doe_clone: Path, monkeypatch
):
    """Host-INDEPENDENT on purpose: a shim generated on Windows can be read on
    Linux, so every host must catch every shape. The pattern was POSIX-only,
    so the Windows-shaped hardcoding it exists to catch could not be caught."""
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(fake_doe_clone))
    tmpl = fake_doe_clone / "coordinator" / "templates" / "shell" / "claude-doe-shim.sh.tmpl"

    for hardcoded in (r"C:\Users\alice\.claude", r"\\fileserver\homes\alice\.claude"):
        tmpl.write_text(
            "claude() {\n"
            f'  _r="{hardcoded}/.doe-root"\n'
            '  command claude-doe --doe-root "$_r" "$@"\n'
            "}\n",
            encoding="utf-8",
        )
        r, _sandbox = run_all(coordinator_root_override=str(fake_doe_clone / "coordinator"))
        assert any(
            line.startswith("FAIL") and "hardcoded home-directory path" in line for line in r.lines
        ), (hardcoded, [line for line in r.lines if "hardcoded" in line])


def test_hardcoded_path_pattern_still_passes_a_clean_variable_only_shim(
    fake_doe_clone: Path, monkeypatch
):
    """Negative control: the fix must not start failing a correct shim."""
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(fake_doe_clone))

    r, _sandbox = run_all(coordinator_root_override=str(fake_doe_clone / "coordinator"))

    assert any(
        line.startswith("PASS") and "no hardcoded home-directory paths" in line for line in r.lines
    ), [line for line in r.lines if "hardcoded" in line]


# ---------------------------------------------------------------------------
# _cold_bare_path — a PATH that resolves nothing cannot prove an absence
# ---------------------------------------------------------------------------


def test_cold_bare_path_is_host_shaped_not_posix_only(monkeypatch):
    """`/usr/bin:/bin` resolves nothing on Windows, so the cold-tier
    registry-binary absence probe was structurally incapable of finding a
    binary there — a PASS that asserted nothing on every Windows run."""
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
    """resolve_coordinator_clone's registry rung spawns `machine-local`, not
    `claude-home`. Probing only claude-home asserted the absence of a binary
    this leg never consults."""
    monkeypatch.setenv("REPO_DOE_CLAUDE", str(fake_doe_clone))

    r, _sandbox = run_all(coordinator_root_override=str(fake_doe_clone / "coordinator"))

    cold_rows = [line for line in r.lines if "cold PATH:" in line]
    assert cold_rows, r.lines
    assert all("machine-local" in line for line in cold_rows), cold_rows

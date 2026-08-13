"""Characterization + parity tests for coordinator_core.install.maximalist.

Port of: install-maximalist.sh (coordinator-claude 6fb5fb37, 2026-07-22, 622 lines).

These tests independently re-derive parity against the bash oracle's OWN
documented control-flow contract (read directly from
install-maximalist.sh's control flow, not from this port's own
transcription):
  - `run_required` phases halt the WHOLE chain on first non-zero exit
    (`exit "$rc"` inside the bash helper) -- Test: halts_on_required_failure.
  - `run_advisory` phases (Step 6 ensure-coordinator-venv, Step 7
    scaffold-canonical-structure) log a WARN and continue; the chain reaches
    every later phase and the overall exit code is 1 (FAILED), not the
    advisory phase's own rc -- Test: advisory_failure_continues_chain.
  - `--check-only` skips ONLY the phases the oracle documents as
    mutating-and-skippable (Step 3.5c gen-settings-hooks.sh has no dry-run
    mode; Step 9 platform-localize; Phase 7 Step 0 setup-state record;
    seeding repos.example_doctrine_repo) while Step 7.5 check-install-singularity
    ALWAYS runs (the oracle calls it unconditionally, no CHECK_ONLY guard)
    -- Test: check_only_skips_mutating_not_singularity.
  - A5 permission preservation: the claude-doe wrapper install must land
    executable at the destination even from a non-executable-by-default
    copy path -- Test: claude_doe_wrapper_preserves_exec_bit.

2026-07-21 (retire-all-bash C13): the ten remaining `["bash", ...]` per-phase
spawns (detect-existing-claude-home, install-health-run, gen-doe-root-pointer,
gen-claude-doe-shim, gen-claude-doe-launcher, register-coordinator-mirror,
check-install-singularity, capture-fan-out-threshold, platform-localize,
coordinator-setup-state) are now direct in-process calls into
coordinator_core.ops/.install/.hooks modules that ALREADY lived in this
package (the coordinator-claude-side .sh/.py files they used to spawn were themselves only
thin polyglot trampolines back into these same modules). The bash-stub-tree
mechanism (`_BASH_SUB_SCRIPTS` / `_write_bash_stub`) this file used to build
for those ten is retired along with them -- coverage moves to Python `_fake_*`
monkeypatches, the same pattern already used for Step 6/Step 7/Step 3.5c
(`_fake_ensure_venv` / `_fake_scaffold` / `_fake_gen_settings_hooks`). Each
real engine module has its own co-located pytest coverage (test_gen_doe_root_
pointer.py, test_capture_fan_out_threshold.py, etc.) -- this file only proves
maximalist.py reaches each one, in order, with the right argv/rc-propagation,
and that no `bash` subprocess is spawned to get there.

Step 9 (platform-localize) note: the bash oracle's "installed script not
found" WARN+FAILED-without-halting asymmetry is RETIRED, not reproduced --
calling coordinator_core.hooks.platform_localize.main in-process, there is no
longer a per-machine file whose absence is observable (see maximalist.py's
module docstring). `test_platform_localize_missing_sets_failed_without_halting`
is removed accordingly; Step 9 is now a plain run_required phase, covered by
the same order/halting tests as its siblings.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Dict

import pytest

from coordinator_core.hooks import platform_localize as _platform_localize_module
from coordinator_core.install import check_install_singularity as _singularity_module
from coordinator_core.install import ensure_venv as _ensure_venv_module
from coordinator_core.install import gen_settings_hooks as _gen_hooks_module
from coordinator_core.install import maximalist
from coordinator_core.install import scaffold_structure as _scaffold_module
from coordinator_core.install import substrate as _substrate_module
from coordinator_core.ops import capture_fan_out_threshold as _threshold_module
from coordinator_core.ops import coordinator_setup_state as _setup_state_module
from coordinator_core.ops import detect_existing_claude_home as _detect_module
from coordinator_core.ops import gen_claude_doe_launcher as _launcher_module
from coordinator_core.ops import gen_claude_doe_shim as _shim_module
from coordinator_core.ops import gen_doe_root_pointer as _doe_pointer_module
from coordinator_core.ops import install_health_run as _health_module
from coordinator_core.ops import register_coordinator_mirror as _mirror_module


# ---------------------------------------------------------------------------
# Fixtures / stub-tree builder
# ---------------------------------------------------------------------------


def _rc_env(name: str) -> str:
    return "RC_" + name.upper().replace("-", "_").replace(".", "_")


def _log_call(step_name: str, argv) -> None:
    call_log = Path(os.environ["CALL_LOG"])
    suffix = (" " + " ".join(argv)) if argv else ""
    with call_log.open("a") as f:
        f.write(f"{step_name}{suffix}\n")


def _make_fake_op_main(step_name: str):
    """Factory for a `main(argv, ...) -> int` stand-in matching the shared
    call-log + RC-env-var failure-injection convention every ops-module fake
    in this file follows -- proves ordering/halting/rc-propagation without
    touching real disk/registry/network state."""

    def _fake(argv=None, **_kwargs):
        argv = list(argv) if argv else []
        _log_call(step_name, argv)
        return int(os.environ.get(_rc_env(step_name), "0"))

    return _fake


_fake_detect_existing_claude_home = _make_fake_op_main("detect-existing-claude-home")
_fake_install_health_run = _make_fake_op_main("install-health-run")
_fake_gen_doe_root_pointer = _make_fake_op_main("gen-doe-root-pointer")
_fake_gen_claude_doe_shim = _make_fake_op_main("gen-claude-doe-shim")
_fake_gen_claude_doe_launcher = _make_fake_op_main("gen-claude-doe-launcher")
_fake_register_coordinator_mirror = _make_fake_op_main("register-coordinator-mirror")
_fake_check_install_singularity = _make_fake_op_main("check-install-singularity")
_fake_capture_fan_out_threshold = _make_fake_op_main("capture-fan-out-threshold")
_fake_platform_localize = _make_fake_op_main("platform-localize")
_fake_coordinator_setup_state = _make_fake_op_main("coordinator-setup-state")


def _fake_ensure_venv(plugin_root, settings_home_path, *, claude_home=None, check_only=False, **kwargs):
    """Stands in for coordinator_core.install.ensure_venv.ensure_coordinator_venv
    -- logs a call-log line (ordering assertions) and honors the same
    RC_ENSURE_COORDINATOR_VENV env-var failure-injection convention the
    other fakes use, without touching real disk/pip/network."""
    call_log = Path(os.environ["CALL_LOG"])
    suffix = " --check" if check_only else ""
    with call_log.open("a") as f:
        f.write(f"ensure-coordinator-venv{suffix}\n")
    rc = int(os.environ.get("RC_ENSURE_COORDINATOR_VENV", "0"))
    if rc != 0:
        raise _ensure_venv_module.EnsureVenvError(
            f"[ensure-coordinator-venv] simulated failure (rc={rc})"
        )
    return "would-rebuild" if check_only else "rebuilt"


def _fake_scaffold(root, manifest_root, *, dry_run=False):
    """Stands in for coordinator_core.install.scaffold_structure.
    scaffold_canonical_structure -- logs a call-log line (ordering
    assertions) and honors the same RC_SCAFFOLD_CANONICAL_STRUCTURE
    env-var failure-injection convention (_rc_env), without touching
    real disk or a real canonical-structure.yaml manifest."""
    call_log = Path(os.environ["CALL_LOG"])
    suffix = " --dry-run" if dry_run else ""
    with call_log.open("a") as f:
        f.write(f"scaffold-canonical-structure{suffix}\n")
    rc = int(os.environ.get("RC_SCAFFOLD_CANONICAL_STRUCTURE", "0"))
    if rc != 0:
        raise _scaffold_module.ScaffoldError(
            f"[scaffold-canonical-structure] simulated failure (rc={rc})"
        )
    return _scaffold_module.ScaffoldResult()


# Captured at import time, BEFORE the stub_env fixture swaps the module
# attribute for `_fake_gen_settings_hooks` -- the kill-switch test needs the
# genuine generator back.
_REAL_GENERATE = _gen_hooks_module.generate


def _fake_gen_settings_hooks(out_path=None, hooks_json_override=None, coordinator_root_override=None):
    """Stands in for coordinator_core.install.gen_settings_hooks.generate --
    logs a call-log line (ordering assertions) and honors the same
    RC_GEN_SETTINGS_HOOKS env-var failure-injection convention the other
    fakes use, without touching the developer's real ~/.claude/settings.json.
    The real generator has its own co-located coverage in
    test_gen_settings_hooks.py."""
    call_log = Path(os.environ["CALL_LOG"])
    with call_log.open("a") as f:
        f.write("gen-settings-hooks\n")
    rc = int(os.environ.get("RC_GEN_SETTINGS_HOOKS", "0"))
    if rc != 0:
        raise _gen_hooks_module.GenSettingsHooksError(
            f"[gen-settings-hooks] simulated failure (rc={rc})"
        )


def _build_stub_tree(tmp_path: Path) -> Dict[str, Path]:
    coord_root = tmp_path / "clone" / "coordinator"
    doe_clone = coord_root.parent
    claude_home = tmp_path / "home"

    (coord_root / "bin").mkdir(parents=True)
    (coord_root / "lib").mkdir(parents=True)
    (claude_home / ".claude" / "bin").mkdir(parents=True)

    # `claude_klabauter_root` is a SEPARATE fixture tree from `coord_root` -- the
    # executable `bin/` surface (`claude-doe`, `gen-claude-klabauter-root-pointer.py`)
    # migrated wholesale to claude-klabauter in commit `b644d5a9` (2026-07-22),
    # so production code now resolves these two paths under
    # `<claude_klabauter_root>/coordinator/bin/...`, distinct from the coordinator-claude clone's
    # `coord_root` (which still correctly houses `templates/`). Mirroring
    # that split here (rather than leaving these files under `coord_root/bin`)
    # is what lets this fixture prove the real call sites resolve the right
    # root for each -- a stub tree that put both under one root could not
    # distinguish "resolved coord_root" from "resolved claude_klabauter_root" by
    # accident.
    claude_klabauter_root = tmp_path / "claude-klabauter"
    (claude_klabauter_root / "coordinator" / "bin").mkdir(parents=True)

    # gen-claude-klabauter-root-pointer.py is invoked as `python3 <path>` via a real
    # subprocess (Step 3.5a.1b, advisory, out of C13's scope) -- must be
    # valid Python, not a shell stub.
    claude_klabauter_pointer = claude_klabauter_root / "coordinator" / "bin" / "gen-claude-klabauter-root-pointer.py"
    claude_klabauter_pointer.write_text(
        "import os, sys\n"
        'with open(os.environ["CALL_LOG"], "a") as f:\n'
        '    f.write("gen-claude-klabauter-root-pointer.py " + " ".join(sys.argv[1:]) + "\\n")\n'
        'sys.exit(int(os.environ.get("RC_GEN_CLAUDE_KLABAUTER_ROOT_POINTER_PY", "0")))\n'
    )

    # claude-doe wrapper source -- installed via pure-Python cp+chmod, not a
    # subprocess call; content is irrelevant, executability + copy fidelity is.
    wrapper_src = claude_klabauter_root / "coordinator" / "bin" / "claude-doe"
    wrapper_src.write_text("#!/bin/sh\necho fake-claude-doe\n")
    wrapper_src.chmod(0o755)

    return {
        "coord_root": coord_root,
        "doe_clone": doe_clone,
        "claude_home": claude_home,
        "claude_klabauter_root": claude_klabauter_root,
    }


def _make_stub_substrate_run(settings_bin_dir: Path):
    """Factory for the `stub_env` substrate stand-in.

    Under the OLD copy-based claude-doe wrapper install, a no-op substrate
    stub was harmless -- the wrapper was copied straight from `wrapper_src`,
    independent of substrate having run. C2 repointed the POSIX wrapper
    install to a SYMLINK onto `<settings_bin>/claude-doe`, the file the real
    `_install_bin_resolvers` (inside `substrate.run`) writes -- so a bare
    no-op stub leaves that symlink dangling. This stand-in creates the same
    `<settings_bin>/claude-doe` stand-in file (with an exec bit) the real
    resolver would have written, so the symlink under test resolves to a
    real target and the fixture stays honest about the new dependency
    instead of softening the assertion.
    """

    def _stub_substrate_run(setup_only, check_only):
        settings_bin_dir.mkdir(parents=True, exist_ok=True)
        target = settings_bin_dir / "claude-doe"
        if not check_only:
            target.write_text("#!/bin/sh\necho fake-resolved-claude-doe\n")
            target.chmod(0o755)
        return 0

    return _stub_substrate_run


@pytest.fixture
def stub_env(tmp_path, monkeypatch):
    paths = _build_stub_tree(tmp_path)
    call_log = tmp_path / "call.log"
    call_log.write_text("")
    monkeypatch.setenv("CALL_LOG", str(call_log))
    # Neutralize the real install-substrate implementation -- it has its own
    # co-located test coverage; this module only needs to prove it calls
    # `.run()` and propagates rc correctly. It must still create the
    # `<settings_bin>/claude-doe` file the real resolver would have written
    # (see `_make_stub_substrate_run`), since C2's symlink-based claude-doe
    # wrapper install now depends on it.
    settings_home = tmp_path / "settings-home"
    settings_home.mkdir(parents=True, exist_ok=True)
    settings_bin_dir = settings_home / "bin"
    monkeypatch.setattr(_substrate_module, "run", _make_stub_substrate_run(settings_bin_dir))
    # Step 6 is native (Port B) -- stand in for the real venv-ensure logic,
    # which has its own co-located coverage in test_ensure_venv.py.
    monkeypatch.setattr(_ensure_venv_module, "ensure_coordinator_venv", _fake_ensure_venv)
    # Step 7 is native (Port D) -- stand in for the real scaffold logic,
    # which has its own co-located coverage in test_scaffold_structure.py.
    monkeypatch.setattr(_scaffold_module, "scaffold_canonical_structure", _fake_scaffold)
    # Step 3.5c is native (DR-059 bash kill) -- stand in for the real
    # generator, which would otherwise resolve the DEVELOPER'S real
    # ~/.claude/settings.json as its default --out.
    monkeypatch.setattr(_gen_hooks_module, "generate", _fake_gen_settings_hooks)
    # The ten C13 bridges are native -- stand in for each real engine module's
    # `main`, which would otherwise touch the developer's real registry,
    # ~/.claude, and machine-local state. Each real module has its own
    # co-located pytest coverage.
    monkeypatch.setattr(_detect_module, "main", _fake_detect_existing_claude_home)
    monkeypatch.setattr(_health_module, "main", _fake_install_health_run)
    monkeypatch.setattr(_doe_pointer_module, "main", _fake_gen_doe_root_pointer)
    monkeypatch.setattr(_shim_module, "main", _fake_gen_claude_doe_shim)
    monkeypatch.setattr(_launcher_module, "main", _fake_gen_claude_doe_launcher)
    monkeypatch.setattr(_mirror_module, "main", _fake_register_coordinator_mirror)
    monkeypatch.setattr(_singularity_module, "main", _fake_check_install_singularity)
    monkeypatch.setattr(_threshold_module, "main", _fake_capture_fan_out_threshold)
    monkeypatch.setattr(_platform_localize_module, "main", _fake_platform_localize)
    monkeypatch.setattr(_setup_state_module, "main", _fake_coordinator_setup_state)
    # register-coordinator-mirror's own coordinator-claude-local "coordinator live path"
    # resolution (native resolve_content_root() / claude-home plugins
    # fallback, out of C13's scope) is stubbed to a fixed value so the phase
    # under test never resolves either.
    monkeypatch.setattr(
        maximalist, "_resolve_coordinator_live_path", lambda: "/fake/coordinator/live"
    )
    # The real `machine-local` CLI IS on PATH during tests (it's an installed
    # binary, not sandboxed away) -- Step 7 Phase-2's repos.example_doctrine_repo seed
    # (maximalist.py ~line 519-524) shells out to it whenever
    # shutil.which("machine-local") resolves. If COORDINATOR_SETTINGS_HOME
    # were merely unset, the real CLI would fall back to resolving the
    # developer's REAL settings-home (~/.coordinator-claude-settings) and
    # overwrite repos.example_doctrine_repo with this test's tmp_path, corrupting the
    # real machine-local registry once tmp_path is reaped. Redirect it to an
    # isolated tmp settings-home instead so any write the real CLI performs
    # lands in the sandbox, never the developer's real registry. (`settings_home`
    # was already created above, ahead of the substrate stub install, since
    # `_make_stub_substrate_run` needs `settings_bin_dir` to exist under it.)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    return {**paths, "call_log": call_log, "settings_bin": settings_bin_dir}


def _log_lines(call_log: Path):
    return [l for l in call_log.read_text().splitlines() if l.strip()]


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def test_help_prints_usage_and_exits_zero(capsys):
    rc = maximalist.main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "install-maximalist.sh" in out
    assert "--check-only" in out


def test_unrecognized_argument_exits_two(capsys):
    rc = maximalist.main(["--bogus-flag"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unrecognized argument: --bogus-flag" in err


def test_main_requires_plugin_root_and_doe_clone_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("REPO_EXAMPLE_DOCTRINE_REPO", raising=False)
    rc = maximalist.main([])
    assert rc == 1


# ---------------------------------------------------------------------------
# Core orchestration semantics
# ---------------------------------------------------------------------------

def test_full_success_returns_zero_and_calls_every_phase_in_order(stub_env):
    rc = maximalist.run(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    assert rc == 0
    names = [line.split(" ", 1)[0] for line in _log_lines(stub_env["call_log"])]
    expected_order = [
        "detect-existing-claude-home",
        "install-health-run",
        "gen-doe-root-pointer",
        "gen-claude-klabauter-root-pointer.py",
        "gen-claude-doe-shim",
        "gen-claude-doe-launcher",
        "gen-settings-hooks",
        "register-coordinator-mirror",
        "ensure-coordinator-venv",
        "scaffold-canonical-structure",
        "check-install-singularity",
        "capture-fan-out-threshold",
        "platform-localize",
        "coordinator-setup-state",
    ]
    # Subsequence, not exact-equality: the oracle's own order is what's
    # asserted here; unrelated interleavings (e.g. no calls at all between
    # two adjacent expected names) aren't expected given the stub tree.
    assert names == expected_order


def test_halts_on_required_failure(stub_env, monkeypatch):
    monkeypatch.setenv(_rc_env("gen-doe-root-pointer"), "1")
    rc = maximalist.run(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    assert rc == 1
    names = [line.split(" ", 1)[0] for line in _log_lines(stub_env["call_log"])]
    assert "gen-doe-root-pointer" in names
    # Nothing after the halted required phase ran.
    assert "gen-claude-doe-shim" not in names
    assert "register-coordinator-mirror" not in names
    assert "coordinator-setup-state" not in names


def test_advisory_failure_continues_chain_to_completion(stub_env, monkeypatch):
    monkeypatch.setenv("RC_ENSURE_COORDINATOR_VENV", "1")
    rc = maximalist.run(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    # Overall run reports FAILED (1) even though no phase halted early.
    assert rc == 1
    names = [line.split(" ", 1)[0] for line in _log_lines(stub_env["call_log"])]
    assert "ensure-coordinator-venv" in names
    # Every later phase, including the final receipt, still ran.
    assert "scaffold-canonical-structure" in names
    assert "check-install-singularity" in names
    assert "coordinator-setup-state" in names


def test_step7_scaffold_raw_oserror_is_advisory_not_fatal(stub_env, monkeypatch):
    # Review: code-reviewer -- Finding 1 (P1, AC D3): scaffold_structure's raw
    # filesystem writes (mkdir/touch/write_text/copyfile) can raise an
    # unwrapped OSError/PermissionError, not just ScaffoldError. Step 7 must
    # degrade this to the same WARN...continuing (advisory, not fatal) path,
    # not let it propagate and abort the whole install.
    def _raise_raw_oserror(root, manifest_root, *, dry_run=False):
        raise PermissionError("simulated raw fs error, not a ScaffoldError")

    monkeypatch.setattr(_scaffold_module, "scaffold_canonical_structure", _raise_raw_oserror)
    rc = maximalist.run(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    # Advisory disposition: overall run reports FAILED (1), but the chain was
    # NOT halted -- every later required phase still ran.
    assert rc == 1
    names = [line.split(" ", 1)[0] for line in _log_lines(stub_env["call_log"])]
    assert "check-install-singularity" in names
    assert "capture-fan-out-threshold" in names
    assert "coordinator-setup-state" in names


def test_check_only_skips_mutating_not_singularity(stub_env):
    rc = maximalist.run(
        check_only=True,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    assert rc == 0
    names = [line.split(" ", 1)[0] for line in _log_lines(stub_env["call_log"])]
    # gen-settings-hooks has no dry-run mode -- skipped entirely.
    assert "gen-settings-hooks" not in names
    # Phase 7 Step 0 receipt is mutating -- skipped.
    assert "coordinator-setup-state" not in names
    # platform-localize skipped entirely under check-only.
    assert "platform-localize" not in names
    # check-install-singularity has NO check-only guard in the oracle --
    # it always runs.
    assert "check-install-singularity" in names
    # Read-only phases still ran with --check-only forwarded.
    log_text = stub_env["call_log"].read_text()
    assert "gen-doe-root-pointer --check-only" in log_text
    assert "ensure-coordinator-venv --check" in log_text
    assert "scaffold-canonical-structure --dry-run" in log_text


def test_compileall_failure_is_advisory_not_fatal(stub_env, monkeypatch):
    """A compileall failure under one interpreter must WARN and continue --
    never halt the chain or fail the underlying phase's own required-ness."""
    monkeypatch.setattr(maximalist, "_compileall_interpreters", lambda: ["/fake/python3"])

    def fake_run_compileall(interp, pkg_root):
        import subprocess as _sp

        return _sp.CompletedProcess([interp], 1, stdout="", stderr="SyntaxError: boom")

    monkeypatch.setattr(maximalist, "_run_compileall", fake_run_compileall)

    rc = maximalist.run(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    # Advisory disposition: overall run reports FAILED (1), chain not halted.
    assert rc == 1
    names = [line.split(" ", 1)[0] for line in _log_lines(stub_env["call_log"])]
    assert "scaffold-canonical-structure" in names
    assert "coordinator-setup-state" in names


def test_compileall_skipped_under_check_only(stub_env, monkeypatch):
    """--check-only must write no .pyc -- compileall must not even be invoked."""
    calls = []
    monkeypatch.setattr(
        maximalist, "_run_compileall", lambda interp, pkg_root: calls.append(interp)
    )

    rc = maximalist.run(
        check_only=True,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    assert rc == 0
    assert calls == []


def test_compileall_runs_under_each_resolved_interpreter(stub_env, monkeypatch):
    monkeypatch.setattr(
        maximalist, "_compileall_interpreters", lambda: ["/fake/base-python3", "/fake/venv-python"]
    )
    calls = []

    def fake_run_compileall(interp, pkg_root):
        import subprocess as _sp

        calls.append(interp)
        return _sp.CompletedProcess([interp], 0, stdout="", stderr="")

    monkeypatch.setattr(maximalist, "_run_compileall", fake_run_compileall)

    rc = maximalist.run(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    assert rc == 0
    assert calls == ["/fake/base-python3", "/fake/venv-python"]


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX-only invariant: on Windows the wrapper install stays the "
    "unchanged shutil.copy2 + chmod path (byte-for-byte AC10), which has no "
    "symlink to assert against -- os.readlink()/os.path.islink() are POSIX-"
    "only concepts, and NTFS regular files always report rw-rw-rw- "
    "(0o666) regardless of os.chmod(..., 0o111), so there is no "
    "platform-appropriate substitute assertion to make here (A5/C2).",
)
def test_claude_doe_wrapper_symlinks_to_settings_bin_target(stub_env):
    """C2 invariant: on POSIX, ``~/.local/bin/claude-doe`` is a SYMLINK onto
    ``<settings_bin>/claude-doe`` (the file the real ``_install_bin_resolvers``
    -- invoked earlier in the same install pass via ``substrate.run`` --
    writes), not an independent copy. The exec bit under a symlink lives on
    the TARGET, not the link itself, so this asserts the resolved target is
    executable rather than checking the link's own (irrelevant) mode."""
    rc = maximalist.run(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    assert rc == 0
    dst = stub_env["claude_home"] / ".local" / "bin" / "claude-doe"
    expected_target = stub_env["settings_bin"] / "claude-doe"
    assert dst.is_symlink(), "claude-doe wrapper must be a symlink under C2, not a copy"
    assert Path(os.readlink(dst)) == expected_target
    resolved = dst.resolve()
    assert resolved.is_file()
    mode = resolved.stat().st_mode
    assert mode & stat.S_IXUSR, "resolved claude-doe wrapper target must stay executable (A5)"


# ---------------------------------------------------------------------------
# C2 -- claude-doe wrapper symlink idempotency (must no-op when already
# correct, and must replace a stale REGULAR FILE, a broken link, or a
# wrong-target link). These call `_install_claude_doe_wrapper` directly
# rather than through the full `maximalist.run()` chain, since only the
# wrapper-install step's own idempotency contract is under test here.
# ---------------------------------------------------------------------------


def _run_install_claude_doe_wrapper(stub_env):
    orch = maximalist._Orchestrator()
    settings_bin = str(stub_env["settings_bin"])
    maximalist._install_claude_doe_wrapper(
        str(stub_env["coord_root"]),
        str(stub_env["claude_home"]),
        False,
        orch,
        str(stub_env["claude_klabauter_root"]),
        settings_bin,
    )
    return stub_env["claude_home"] / ".local" / "bin" / "claude-doe"


# AC6 (docs/plans/2026-08-13-one-shared-symlink-capability-guard.md): gates a POSIX-only C2 symlink CONTRACT, not host symlink capability -- leave as os.name, do not swap for the shared probe.
@pytest.mark.skipif(os.name == "nt", reason="POSIX-only C2 symlink contract; see test above.")
def test_claude_doe_wrapper_symlink_noops_when_already_correct(stub_env):
    """Directly re-running the install step against an already-correct
    symlink must not unlink/relink it -- the existing inode identity (and
    mtime) is preserved."""
    # Prime the real link target the same way substrate.run's stub would.
    settings_bin = stub_env["settings_bin"]
    settings_bin.mkdir(parents=True, exist_ok=True)
    target = settings_bin / "claude-doe"
    target.write_text("#!/bin/sh\necho fake\n")
    target.chmod(0o755)

    dst = _run_install_claude_doe_wrapper(stub_env)
    assert dst.is_symlink()
    before_inode = dst.lstat().st_ino

    dst2 = _run_install_claude_doe_wrapper(stub_env)
    assert dst2.is_symlink()
    assert dst2.lstat().st_ino == before_inode
    assert Path(os.readlink(dst2)) == target


# AC6 (docs/plans/2026-08-13-one-shared-symlink-capability-guard.md): gates a POSIX-only C2 symlink CONTRACT, not host symlink capability -- leave as os.name, do not swap for the shared probe.
@pytest.mark.skipif(os.name == "nt", reason="POSIX-only C2 symlink contract; see test above.")
def test_claude_doe_wrapper_symlink_replaces_stale_regular_file(stub_env):
    """The live-machine state right now: `~/.local/bin/claude-doe` is a
    stale REGULAR FILE left over from the pre-C2 shutil.copy2 install. The
    install step must replace it with a symlink onto <settings_bin>/claude-doe,
    not skip it or fail because something already exists at the destination."""
    settings_bin = stub_env["settings_bin"]
    settings_bin.mkdir(parents=True, exist_ok=True)
    target = settings_bin / "claude-doe"
    target.write_text("#!/bin/sh\necho fake\n")
    target.chmod(0o755)

    dst = stub_env["claude_home"] / ".local" / "bin" / "claude-doe"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("#!/bin/sh\necho stale-pre-C2-copy\n")
    dst.chmod(0o755)
    assert dst.is_file() and not dst.is_symlink()

    result = _run_install_claude_doe_wrapper(stub_env)
    assert result.is_symlink(), "stale regular file must be replaced with a symlink"
    assert Path(os.readlink(result)) == target


# AC6 (docs/plans/2026-08-13-one-shared-symlink-capability-guard.md): gates a POSIX-only C2 symlink CONTRACT, not host symlink capability -- leave as os.name, do not swap for the shared probe.
@pytest.mark.skipif(os.name == "nt", reason="POSIX-only C2 symlink contract; see test above.")
def test_claude_doe_wrapper_symlink_replaces_broken_link(stub_env):
    """A dangling symlink (target since deleted) must be replaced, not left
    broken or treated as fatal."""
    settings_bin = stub_env["settings_bin"]
    settings_bin.mkdir(parents=True, exist_ok=True)
    target = settings_bin / "claude-doe"
    target.write_text("#!/bin/sh\necho fake\n")
    target.chmod(0o755)

    dst = stub_env["claude_home"] / ".local" / "bin" / "claude-doe"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.symlink_to(settings_bin / "nonexistent-target")
    assert os.path.islink(dst) and not dst.exists()

    result = _run_install_claude_doe_wrapper(stub_env)
    assert result.is_symlink()
    assert Path(os.readlink(result)) == target
    assert result.resolve().is_file()


# AC6 (docs/plans/2026-08-13-one-shared-symlink-capability-guard.md): gates a POSIX-only C2 symlink CONTRACT, not host symlink capability -- leave as os.name, do not swap for the shared probe.
@pytest.mark.skipif(os.name == "nt", reason="POSIX-only C2 symlink contract; see test above.")
def test_claude_doe_wrapper_symlink_replaces_wrong_target_link(stub_env):
    """A symlink pointing at the WRONG target (e.g. a stale settings-home
    path) must be re-pointed at the current <settings_bin>/claude-doe."""
    settings_bin = stub_env["settings_bin"]
    settings_bin.mkdir(parents=True, exist_ok=True)
    target = settings_bin / "claude-doe"
    target.write_text("#!/bin/sh\necho fake\n")
    target.chmod(0o755)

    wrong_target_dir = stub_env["claude_home"] / "old-settings-home" / "bin"
    wrong_target_dir.mkdir(parents=True, exist_ok=True)
    wrong_target = wrong_target_dir / "claude-doe"
    wrong_target.write_text("#!/bin/sh\necho old\n")
    wrong_target.chmod(0o755)

    dst = stub_env["claude_home"] / ".local" / "bin" / "claude-doe"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.symlink_to(wrong_target)
    assert Path(os.readlink(dst)) == wrong_target

    result = _run_install_claude_doe_wrapper(stub_env)
    assert result.is_symlink()
    assert Path(os.readlink(result)) == target


def test_claude_doe_wrapper_missing_source_is_fatal(stub_env):
    (stub_env["claude_klabauter_root"] / "coordinator" / "bin" / "claude-doe").unlink()
    rc = maximalist.run(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    assert rc == 1


def test_defender_offer_skips_cleanly_on_non_windows_host(stub_env, monkeypatch):
    monkeypatch.delenv("OS", raising=False)
    rc = maximalist.run(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    # Defender offer must not be fatal / must not block the chain on a
    # non-Windows host (the overwhelming majority of CI/dev machines).
    assert rc == 0


# ---------------------------------------------------------------------------
# Step 3.5c -- gen-settings-hooks (DR-059 bash kill, 2026-07-20)
#
# Step 3.5c used to run `bash <coord_root>/bin/gen-settings-hooks.sh`. That .sh
# is itself only an sh/python polyglot trampoline that re-execs python and
# imports THIS repo's coordinator_core.install.gen_settings_hooks -- so claude-klabauter
# was spawning a cold bash.exe (break-class on Windows, CLAUDE.md § Runtime
# conventions) purely to round-trip back into its own module. The step now
# calls generate() in-process.
# ---------------------------------------------------------------------------

def test_step35c_calls_python_generator_and_never_spawns_bash(stub_env, monkeypatch):
    """The rewired Step 3.5c must reach the in-package Python generator and
    must not put gen-settings-hooks (or bash) on any subprocess argv."""
    argvs = []
    real_run = maximalist._run

    def _recording_run(cmd, env=None):
        argvs.append(list(cmd))
        return real_run(cmd, env=env)

    monkeypatch.setattr(maximalist, "_run", _recording_run)

    rc = maximalist.run(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    assert rc == 0
    # The Python generator ran (fake stand-in logs the native, suffix-less name).
    assert "gen-settings-hooks" in _log_lines(stub_env["call_log"])
    # ... and nothing was subprocess-spawned for it, under bash or otherwise.
    offenders = [a for a in argvs if any("gen-settings-hooks" in str(part) for part in a)]
    assert offenders == [], f"Step 3.5c still subprocess-spawns: {offenders}"


def test_step35c_generator_failure_is_still_fatal(stub_env, monkeypatch):
    """run_required semantics preserved: a generator business error must halt
    the install loudly, exactly as the bash exit-1 did."""
    monkeypatch.setenv(_rc_env("gen-settings-hooks"), "1")
    rc = maximalist.run(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    assert rc == 1
    names = [line.split(" ", 1)[0] for line in _log_lines(stub_env["call_log"])]
    assert "gen-settings-hooks" in names
    # Nothing after the halted required phase ran.
    assert "register-coordinator-mirror" not in names
    assert "check-install-singularity" not in names
    assert "coordinator-setup-state" not in names


def test_step35c_honours_operator_kill_switch_through_new_call_path(stub_env, monkeypatch, capsys):
    """LOAD-BEARING: the operator kill-switch sentinel
    (<HOME>/.claude/.coordinator-hooks-disabled) must still no-op hook
    generation now that the step runs in-process instead of via bash.

    This exercises the REAL generate() (the stub_env fake is deliberately
    undone), proving the sentinel is resolved as a sibling of the resolved
    --out path and that the no-op is reported to the caller as success --
    identical to the subprocess form's rc=0.
    """
    monkeypatch.setattr(_gen_hooks_module, "generate", _REAL_GENERATE)

    sandbox_home = stub_env["claude_home"]
    (sandbox_home / ".claude").mkdir(parents=True, exist_ok=True)
    marker = sandbox_home / ".claude" / ".coordinator-hooks-disabled"
    marker.write_text("armed by test\n")
    settings = sandbox_home / ".claude" / "settings.json"
    assert not settings.exists()

    # generate() resolves its default --out from HOME; point it at the sandbox
    # so the developer's real ~/.claude/settings.json is never a candidate.
    monkeypatch.setenv("HOME", str(sandbox_home))
    monkeypatch.setenv("USERPROFILE", str(sandbox_home))

    rc = maximalist.run(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(sandbox_home),
    )

    # No-op reported as success -- the install continued past Step 3.5c.
    assert rc == 0
    assert not settings.exists(), "kill-switch armed but hooks were regenerated"
    err = capsys.readouterr().err
    assert "DISABLED by operator marker" in err
    assert str(marker) in err


def test_step35c_marker_skip_surfaces_in_orchestrator_output_and_summary(stub_env, monkeypatch, capsys):
    """F9 regression: a marker-disabled Step 3.5c must be VISIBLE in the
    orchestrator's own output, not only in generate()'s own stderr line --
    the prior code discarded generate()'s return status entirely, so a run
    with hooks disabled printed nothing but the (misleading, success-shaped)
    SessionStart NOTE. Assert the phase-local SKIPPED notice, the marker
    path, and the end-of-run "SKIPPED PHASES" summary all name this phase --
    an operator scanning only the tail of a long run must still see it.
    """
    monkeypatch.setattr(_gen_hooks_module, "generate", _REAL_GENERATE)

    sandbox_home = stub_env["claude_home"]
    (sandbox_home / ".claude").mkdir(parents=True, exist_ok=True)
    marker = sandbox_home / ".claude" / ".coordinator-hooks-disabled"
    marker.write_text("armed by test\n")

    monkeypatch.setenv("HOME", str(sandbox_home))
    monkeypatch.setenv("USERPROFILE", str(sandbox_home))

    rc = maximalist.run(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(sandbox_home),
    )

    assert rc == 0  # kill-switch no-op is still success, not advisory-failed
    out = capsys.readouterr().out
    assert "gen-settings-hooks: skipped (disabled by operator marker)" in out
    assert "--- SKIPPED: gen-settings-hooks (Step 3.5c" in out
    assert "DISABLED by operator marker" in out
    assert str(marker) in out
    assert "Delete that file to re-enable" in out
    assert "SKIPPED PHASES" in out
    # The final summary block must also carry this phase, not just the
    # inline notice -- that's the whole point of collecting `orch.skipped`.
    summary = out.split("SKIPPED PHASES", 1)[1]
    assert "gen-settings-hooks (Step 3.5c" in summary
    assert str(marker) in summary


# ---------------------------------------------------------------------------
# C13 -- the ten remaining bash bridges retired to in-process calls
#
# Each phase used to spawn `["bash", <coord_root or claude_home>/.../<name>]`.
# That coordinator-claude-side script was, in every one of the ten cases, only a thin
# polyglot trampoline back into a coordinator_core module already living in
# THIS package. These tests pin the observable contract (argv shape, rc
# propagation, halting semantics, and "no bash subprocess is spawned to get
# there") that the oracle's own subprocess-per-phase shape guaranteed for
# free -- "pytest green" on the pre-existing suite alone would not have
# caught a phase silently regressing to a no-op or a wrong-argv call.
# ---------------------------------------------------------------------------

def test_c13_no_phase_spawns_bash(stub_env, monkeypatch):
    """None of the ten retired phases may put "bash" on any subprocess argv
    -- the single strongest parity assertion for this chunk: whatever else
    changed internally, the Windows-hostile bash spawn must be gone."""
    argvs = []
    real_run = maximalist._run

    def _recording_run(cmd, env=None):
        argvs.append(list(cmd))
        return real_run(cmd, env=env)

    monkeypatch.setattr(maximalist, "_run", _recording_run)

    rc = maximalist.run(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    assert rc == 0
    bash_offenders = [a for a in argvs if a and a[0] == "bash"]
    assert bash_offenders == [], f"a phase still spawns bash: {bash_offenders}"


def test_c13_check_only_forwarded_to_each_native_phase(stub_env):
    """--check-only must reach every phase that accepts it as a native argv
    flag now, exactly as it did as a subprocess argv element."""
    rc = maximalist.run(
        check_only=True,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    assert rc == 0
    log_text = stub_env["call_log"].read_text()
    assert "gen-doe-root-pointer --check-only" in log_text
    assert "gen-claude-doe-shim --check-only" in log_text
    assert "gen-claude-doe-launcher --check-only" in log_text
    assert "capture-fan-out-threshold --check-only" in log_text
    assert "register-coordinator-mirror --check-only" in log_text


def test_c13_register_coordinator_mirror_receives_resolved_live_path(stub_env, monkeypatch):
    """register-coordinator-mirror is the one C13 phase carrying real (if
    coordinator-claude-owned) resolution logic: the maximalist-local
    `_resolve_coordinator_live_path` result must be threaded through to the
    engine module as `--live-path <value>`, matching the retired
    trampoline's own `--live-path` handoff exactly."""
    monkeypatch.setattr(
        maximalist, "_resolve_coordinator_live_path", lambda: "/resolved/live/path"
    )
    rc = maximalist.run(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    assert rc == 0
    log_text = stub_env["call_log"].read_text()
    assert "register-coordinator-mirror --live-path /resolved/live/path" in log_text


def test_c13_register_coordinator_mirror_unresolvable_live_path_is_fatal(stub_env, monkeypatch):
    """An unresolvable coordinator live path (both resolution tiers
    exhausted) must halt the chain -- matching the retired trampoline's own
    `sys.exit(1)` on the identical condition -- rather than silently
    registering an empty/wrong value."""
    monkeypatch.setattr(maximalist, "_resolve_coordinator_live_path", lambda: "")
    rc = maximalist.run(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    assert rc == 1
    names = [line.split(" ", 1)[0] for line in _log_lines(stub_env["call_log"])]
    assert "register-coordinator-mirror" not in names
    assert "coordinator-setup-state" not in names


def test_c13_coordinator_setup_state_record_argv(stub_env):
    """Phase 7 Step 0 must call coordinator_setup_state with the exact
    `["record", "setup_concluded"]` argv the retired subprocess form used."""
    rc = maximalist.run(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    assert rc == 0
    log_text = stub_env["call_log"].read_text()
    assert "coordinator-setup-state record setup_concluded" in log_text


def test_c13_platform_localize_failure_is_fatal_not_advisory(stub_env, monkeypatch):
    """Step 9 is a plain run_required phase now (the old "installed script
    not found" WARN+FAILED-without-halting asymmetry is retired, not
    reproduced -- see module docstring) -- a business failure must halt the
    chain like every other required phase."""
    monkeypatch.setenv(_rc_env("platform-localize"), "1")
    rc = maximalist.run(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    assert rc == 1
    names = [line.split(" ", 1)[0] for line in _log_lines(stub_env["call_log"])]
    assert "platform-localize" in names


# ---------------------------------------------------------------------------
# Review: code-reviewer (Lane B install F3) -- direct coverage for
# `_resolve_coordinator_live_path` and the `claude-home` Windows-argv helper.
# Every `test_c13_*` test above monkeypatches `_resolve_coordinator_live_path`
# itself, leaving its own internals -- the Tier-1 (native call)/Tier-2
# (claude-home fallback) branching and the Windows-branching argv helper --
# with zero direct coverage.
#
# DR-079 (2026-07-21): Tier 1 repointed from a `bash resolve-coordinator-
# clone.sh --for-content` subprocess spawn to a direct in-process call of
# `coordinator_core.resolve_coordinator_clone.resolve_content_root()` (the
# verified native drop-in -- `--for-content` is a retained legacy alias of
# `--content-root`, identical resolution ladder). The former
# `_resolve_coordinator_clone_script_argv` Windows-shebang-exec helper and
# its two direct-coverage tests are retired along with the subprocess spawn
# they existed to build an argv for -- a native in-process call has no argv
# to marshal.
# ---------------------------------------------------------------------------

def test_claude_home_cli_argv_posix_uses_bare_name(monkeypatch):
    monkeypatch.setattr(maximalist.os, "name", "posix")
    assert maximalist._claude_home_cli_argv("plugins") == ["claude-home", "plugins"]


def test_claude_home_cli_argv_windows_uses_mirror_candidate_when_settings_home_absent(tmp_path, monkeypatch):
    """Fallback-when-settings-home-absent case: only the retired compat mirror
    candidate exists on disk, so it's the one resolved. Renamed from
    `..._prefers_claude_home_candidate` (review: code-reviewer F1) — after the
    settings-home-first precedence fix, that name no longer describes what
    this test asserts (it creates only one candidate, so it can't and
    doesn't pin precedence; see the `..._both_candidates_present` test below
    for the actual precedence pin)."""
    monkeypatch.setattr(maximalist.os, "name", "nt")
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    cand = tmp_path / ".claude" / "bin" / "claude-home.cmd"
    cand.parent.mkdir(parents=True, exist_ok=True)
    cand.write_text("", encoding="utf-8")
    monkeypatch.setattr(maximalist.shutil, "which", lambda name: None)
    assert maximalist._claude_home_cli_argv("plugins") == [str(cand), "plugins"]


def test_claude_home_cli_argv_windows_falls_back_to_settings_home_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(maximalist.os, "name", "nt")
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    cand = tmp_path / ".coordinator-claude-settings" / "bin" / "claude-home.cmd"
    cand.parent.mkdir(parents=True, exist_ok=True)
    cand.write_text("", encoding="utf-8")
    monkeypatch.setattr(maximalist.shutil, "which", lambda name: None)
    assert maximalist._claude_home_cli_argv("plugins") == [str(cand), "plugins"]


def test_claude_home_cli_argv_windows_both_candidates_present_settings_home_wins(tmp_path, monkeypatch):
    """Precedence pin (review: code-reviewer F1, P1): when BOTH the
    settings-home and retired compat mirror `.cmd` candidates exist on disk,
    settings-home must win — this is the case the pre-existing pair of tests
    above never exercised (each creates exactly one candidate, so neither
    can detect an inverted precedence)."""
    monkeypatch.setattr(maximalist.os, "name", "nt")
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    settings_home_cand = tmp_path / ".coordinator-claude-settings" / "bin" / "claude-home.cmd"
    mirror_cand = tmp_path / ".claude" / "bin" / "claude-home.cmd"
    for cand in (settings_home_cand, mirror_cand):
        cand.parent.mkdir(parents=True, exist_ok=True)
        cand.write_text("", encoding="utf-8")
    monkeypatch.setattr(maximalist.shutil, "which", lambda name: None)
    assert maximalist._claude_home_cli_argv("plugins") == [str(settings_home_cand), "plugins"]


def test_claude_home_cli_argv_windows_falls_back_to_path_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(maximalist.os, "name", "nt")
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(maximalist.shutil, "which", lambda name: "C:\\PATH\\claude-home.cmd")
    assert maximalist._claude_home_cli_argv("plugins") == ["C:\\PATH\\claude-home.cmd", "plugins"]


def test_claude_home_cli_argv_windows_falls_back_to_bare_name_when_unresolvable(tmp_path, monkeypatch):
    monkeypatch.setattr(maximalist.os, "name", "nt")
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.setattr(maximalist.shutil, "which", lambda name: None)
    assert maximalist._claude_home_cli_argv("plugins") == ["claude-home", "plugins"]


def test_resolve_coordinator_live_path_tier1_success(monkeypatch):
    """Tier 1 (native `resolve_content_root()` call) succeeds -- Tier 2
    (`claude-home plugins`) must never be invoked."""
    import coordinator_core.resolve_coordinator_clone as rcc

    monkeypatch.setattr(rcc, "resolve_content_root", lambda: "/tier1/live")

    def fail_run(argv, **kwargs):  # pragma: no cover -- must not be reached
        raise AssertionError(f"Tier 2 subprocess should not run, got {argv!r}")

    monkeypatch.setattr(maximalist.subprocess, "run", fail_run)
    assert maximalist._resolve_coordinator_live_path() == "/tier1/live"


def test_resolve_coordinator_live_path_falls_back_to_tier2_on_tier1_error(monkeypatch, tmp_path):
    """Tier 1 raising `ResolveCoordinatorCloneError` (native peer's
    unresolvable-failure contract) falls through to the `claude-home
    plugins` Tier 2 fallback."""
    import coordinator_core.resolve_coordinator_clone as rcc

    def raise_unresolvable():
        raise rcc.ResolveCoordinatorCloneError("no readable coordinator content root found")

    monkeypatch.setattr(rcc, "resolve_content_root", raise_unresolvable)

    # `_claude_home_cli_argv` (called by the Tier-2 fallback under test)
    # probes real machine locations for a delivered `claude-home.cmd`
    # (settings-home first, then the retired compat mirror, then PATH)
    # before falling back to the bare `"claude-home"` name -- a real
    # provisioned dev box (this one included, post-installer-run) has one
    # at `~/.coordinator-claude-settings/bin/claude-home.CMD`, so the bare-
    # name assumption below only held on a machine with no such install.
    # Point every probed env var at an empty tmp_path so this test doesn't
    # depend on the host's real install state.
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.setattr(maximalist.shutil, "which", lambda _name: None)

    def fake_run(argv, **kwargs):
        assert argv == ["claude-home", "plugins"]
        return subprocess.CompletedProcess(argv, 0, stdout="/plugins/root\n", stderr="")

    monkeypatch.setattr(maximalist.subprocess, "run", fake_run)
    result = maximalist._resolve_coordinator_live_path()
    assert result == os.path.join("/plugins/root", "coordinator-claude", "coordinator")


# ---------------------------------------------------------------------------
# Phase 3 Step 3 (partial) -- best-effort seed repos.claude_klabauter
#
# Cross-repo ask (claude-central-em, 2026-07-22, "Ask 1"): neither install
# ordering ever wrote this registry key, so it has only ever resolved on this
# machine because it was hand-set once on 2026-07-03. These tests pin the
# seeding block's own contract in isolation from the example_doctrine_repo sibling it
# mirrors -- the subprocess/registry seam is mocked throughout; none of these
# tests may mutate the real machine-local registry.
# ---------------------------------------------------------------------------

from coordinator_core.install import _shared as _shared_module


def _real_claude_klabauter_clone_root() -> Path:
    """The repo root maximalist.py's own in-process path derivation resolves
    to -- computed the same way the production code does, so tests assert
    against the real invariant rather than a hardcoded guess."""
    return Path(maximalist.__file__).resolve().parents[2]


def test_seed_claude_klabauter_argv_composed_with_key_and_derived_path(stub_env, monkeypatch):
    """(a) The seed argv is composed with the right registry key and a path
    whose coordinator_core/ subdir exists (this repo itself, since the
    derivation is based on maximalist.py's own on-disk location)."""
    monkeypatch.setattr(_shared_module, "resolve_machine_local_cli", lambda plugin_root: ["fake-ml-cli"])

    captured = []
    real_run = maximalist._run

    def _recording_run(cmd, env=None):
        captured.append(list(cmd))
        return real_run(cmd, env=env)

    monkeypatch.setattr(maximalist, "_run", _recording_run)

    rc = maximalist.run(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    assert rc == 0

    expected_root = _real_claude_klabauter_clone_root()
    assert (expected_root / "coordinator_core").is_dir()
    claude_klabauter_seed_calls = [c for c in captured if "repos.claude_klabauter" in c]
    assert claude_klabauter_seed_calls == [
        ["fake-ml-cli", "set", "repos.claude_klabauter", str(expected_root)]
    ]


def test_seed_claude_klabauter_check_only_does_not_invoke_machine_local(stub_env, monkeypatch):
    """(b) --check-only must not shell out to machine-local for this seed --
    resolve_machine_local_cli must never even be called."""
    resolve_calls = []
    monkeypatch.setattr(
        _shared_module,
        "resolve_machine_local_cli",
        lambda plugin_root: resolve_calls.append(plugin_root) or ["fake-ml-cli"],
    )

    captured = []
    real_run = maximalist._run

    def _recording_run(cmd, env=None):
        captured.append(list(cmd))
        return real_run(cmd, env=env)

    monkeypatch.setattr(maximalist, "_run", _recording_run)

    rc = maximalist.run(
        check_only=True,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    assert rc == 0
    assert resolve_calls == []
    assert [c for c in captured if "repos.claude_klabauter" in c] == []


def test_seed_claude_klabauter_machine_local_absent_degrades_to_note(stub_env, monkeypatch, capsys):
    """(c) When machine-local cannot be resolved at all (no sibling CLI, no
    PATH fallback), the block must print a NOTE and continue -- never raise.
    `shutil.which` is faked to miss ONLY "machine-local" (delegating every
    other lookup, e.g. gen-claude-klabauter-root-pointer.py's own python3/python probe,
    to the real resolver) so this test isolates the seed block's own
    degrade-to-NOTE path from unrelated advisory phases elsewhere in the chain."""
    monkeypatch.setattr(_shared_module, "resolve_machine_local_cli", lambda plugin_root: None)
    real_which = maximalist.shutil.which
    monkeypatch.setattr(
        maximalist.shutil, "which", lambda name: None if name == "machine-local" else real_which(name)
    )

    rc = maximalist.run(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "NOTE: machine-local not yet on PATH" in err
    assert "repos.claude_klabauter" in err


def test_seed_claude_klabauter_missing_coordinator_core_warns_and_skips(stub_env, monkeypatch, tmp_path, capsys):
    """Sanity guard: if the derived clone root has no coordinator_core/
    subdir (the wrong-directory trap named in the cross-repo ask -- an
    unrelated bin/ at repo root cannot be told apart from the real one by a
    bare existence check), the block WARNs and skips rather than seeding a
    bogus path. Faking maximalist's own `__file__` makes the in-process
    derivation resolve to a directory with no coordinator_core/ sibling."""
    # Deliberately NOT nested as <root>/coordinator_core/install/maximalist.py --
    # that shape is self-referential (parents[2]/coordinator_core is always the
    # very directory maximalist.py already lives inside, so the guard could
    # never observe a miss). "some-unrelated-bin/install/maximalist.py" has no
    # coordinator_core ancestor at all, matching the wrong-directory trap this
    # guard exists to catch (an unrelated bin/ that isn't the real tree).
    fake_file = tmp_path / "not-claude-klabauter" / "some-unrelated-bin" / "install" / "maximalist.py"
    fake_file.parent.mkdir(parents=True)
    monkeypatch.setattr(maximalist, "__file__", str(fake_file))

    # NOTE: resolve_machine_local_cli is also called by the sibling
    # repos.example_doctrine_repo seed block above this one in the same phase, so the
    # discriminating assertion is "no repos.claude_klabauter _run call" (below),
    # not "resolve_machine_local_cli was never called at all".
    captured = []
    real_run = maximalist._run

    def _recording_run(cmd, env=None):
        captured.append(list(cmd))
        return real_run(cmd, env=env)

    monkeypatch.setattr(maximalist, "_run", _recording_run)

    rc = maximalist.run(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )
    assert rc == 0
    assert [c for c in captured if "repos.claude_klabauter" in c] == []
    err = capsys.readouterr().err
    assert "no coordinator_core/ subdir" in err


def test_resolve_coordinator_live_path_both_tiers_fail_returns_empty(monkeypatch):
    import coordinator_core.resolve_coordinator_clone as rcc

    def raise_unresolvable():
        raise rcc.ResolveCoordinatorCloneError("no readable coordinator content root found")

    monkeypatch.setattr(rcc, "resolve_content_root", raise_unresolvable)

    def fake_run(argv, **kwargs):
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr(maximalist.subprocess, "run", fake_run)
    assert maximalist._resolve_coordinator_live_path() == ""


# ---------------------------------------------------------------------------
# claude_home_dir resolution (_run_body) -- the root of the resolver bug class
#
# `main()`, the real-install entry point, never passes claude_home_dir, so the
# env ladder inside `_run_body` IS the production path. It previously read
# `CLAUDE_HOME or HOME or ""` -- no USERPROFILE rung, and an empty-string HOME
# defeated the "" default -- so a native HOME-less Windows shell resolved "".
# Three consumers derive real filesystem targets from that one value (the
# settings-home/PATH prepend, coordinator-identity.yaml, and the
# ~/.local/bin/claude-doe wrapper destination), so "" silently anchored all
# three at the filesystem root instead of failing.
#
# These assert through the WRAPPER DESTINATION rather than on the local: it is
# a real consumer of the resolved value, so a test that passes here cannot pass
# against a resolution that only looks right in isolation. Each chdirs into
# tmp_path so that a regression (which relativizes the destination against the
# cwd) writes its stray `.local/bin/claude-doe` into the sandbox, not the repo.


def _run_with_env_resolved_home(stub_env, tmp_path, monkeypatch, **env):
    """Invoke the full install with claude_home_dir UNSET (the `main()` shape),
    letting `_run_body` resolve it from the environment alone."""
    monkeypatch.chdir(tmp_path)
    for var in ("CLAUDE_HOME", "HOME", "USERPROFILE"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    return maximalist.run(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
    )


def test_run_body_resolves_home_from_userprofile_when_posix_home_is_unset(
    stub_env, tmp_path, monkeypatch
):
    # Native Windows shells (PowerShell, cmd.exe) set USERPROFILE and not HOME.
    rc = _run_with_env_resolved_home(
        stub_env, tmp_path, monkeypatch, USERPROFILE=str(stub_env["claude_home"])
    )

    assert rc == 0
    assert (stub_env["claude_home"] / ".local" / "bin" / "claude-doe").exists()


def test_run_body_treats_empty_home_as_unset_and_falls_through(
    stub_env, tmp_path, monkeypatch
):
    # An exported-but-empty HOME is the shape that defeated the `or ""` default:
    # empty env vars are treated as unset and fall through, matching
    # `_shared.require_home`'s deliberate precedent.
    rc = _run_with_env_resolved_home(
        stub_env, tmp_path, monkeypatch,
        HOME="",
        USERPROFILE=str(stub_env["claude_home"]),
    )

    assert rc == 0
    assert (stub_env["claude_home"] / ".local" / "bin" / "claude-doe").exists()


def test_run_body_prefers_claude_home_over_the_lower_rungs(
    stub_env, tmp_path, monkeypatch
):
    decoy = tmp_path / "decoy-home"
    rc = _run_with_env_resolved_home(
        stub_env, tmp_path, monkeypatch,
        CLAUDE_HOME=str(stub_env["claude_home"]),
        HOME=str(decoy),
        USERPROFILE=str(decoy),
    )

    assert rc == 0
    assert (stub_env["claude_home"] / ".local" / "bin" / "claude-doe").exists()
    assert not (decoy / ".local" / "bin" / "claude-doe").exists()


def test_run_body_fails_loud_when_no_home_var_resolves(
    stub_env, tmp_path, monkeypatch, capsys
):
    # A stripped environment must abort rather than resolve "" and quietly
    # anchor every derived target at the filesystem root.
    rc = _run_with_env_resolved_home(stub_env, tmp_path, monkeypatch)

    assert rc == 1
    assert "install-maximalist" in capsys.readouterr().err
    assert not (tmp_path / ".local" / "bin" / "claude-doe").exists()


# ---------------------------------------------------------------------------
# C4 -- install receipt persistence: resolution-journal wiring + receipt
# build/persist at run end (docs/research/2026-08-06-install-receipt-
# persistence-design.md).
# ---------------------------------------------------------------------------

from coordinator_core.install import receipt as _receipt_module
from coordinator_core.install import resolution_journal as _resolution_journal_module
from coordinator_core.install.write_surface import ShapedClause as _ShapedClause
from coordinator_core.install.write_surface import StaticClause as _StaticClause
from coordinator_core.install.write_surface import WriteSurfaceDeclaration as _WSD
from coordinator_core.install.write_surface import WriteSurfaceEntry as _WSE


def _run_kwargs(stub_env):
    return dict(
        check_only=False,
        non_interactive=True,
        coord_root=str(stub_env["coord_root"]),
        claude_klabauter_root=str(stub_env["claude_klabauter_root"]),
        doe_clone=str(stub_env["doe_clone"]),
        claude_home_dir=str(stub_env["claude_home"]),
    )


def test_journal_cleared_at_run_start(stub_env, monkeypatch):
    calls = []
    monkeypatch.setattr(
        _resolution_journal_module, "clear_journal", lambda: calls.append("cleared")
    )
    rc = maximalist.run(**_run_kwargs(stub_env))
    assert rc == 0
    assert calls == ["cleared"]


def test_env_var_propagated_to_subprocess_phase(stub_env, monkeypatch, tmp_path):
    # `clear_journal`/`persist_receipt` real writes are otherwise refused by
    # the test-suite belt-and-braces opt-out (see resolution_journal.py /
    # receipt.py module docstrings) -- lift it for this test, same pattern
    # `test_resolution_journal.py` uses, since this test's assertion is
    # about the env var reaching a real subprocess, not about the mutation
    # guard itself.
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    # Rewrite the real subprocess-invoked phase (gen-claude-klabauter-root-pointer.py)
    # to record the resolution-journal env var it inherits, proving it
    # reaches a subprocess phase's environment, not only in-process ones.
    seen_log = tmp_path / "journal-env-seen.log"
    claude_klabauter_pointer = stub_env["claude_klabauter_root"] / "coordinator" / "bin" / "gen-claude-klabauter-root-pointer.py"
    claude_klabauter_pointer.write_text(
        "import os, sys\n"
        f'with open({str(seen_log)!r}, "w") as f:\n'
        '    f.write(os.environ.get("COORDINATOR_INSTALL_RESOLUTION_JOURNAL", ""))\n'
        'with open(os.environ["CALL_LOG"], "a") as f:\n'
        '    f.write("gen-claude-klabauter-root-pointer.py " + " ".join(sys.argv[1:]) + "\\n")\n'
        'sys.exit(int(os.environ.get("RC_GEN_CLAUDE_KLABAUTER_ROOT_POINTER_PY", "0")))\n'
    )

    rc = maximalist.run(**_run_kwargs(stub_env))
    assert rc == 0
    seen = seen_log.read_text()
    assert seen, "subprocess phase did not see COORDINATOR_INSTALL_RESOLUTION_JOURNAL at all"
    assert seen == str(_resolution_journal_module._journal_path())


def test_receipt_built_and_persisted_after_successful_run(stub_env, monkeypatch):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    decl = _WSD(
        writer_id="c4-test-static-writer",
        source_module="c4.test.static_writer",
        clauses=(
            _StaticClause(
                entries=(_WSE(kind="file-path", path="/tmp/whatever", reason="test"),),
            ),
        ),
    )
    monkeypatch.setattr(
        maximalist,
        "_collect_writer_declarations",
        lambda repo_root: ({"c4-test-static-writer": decl}, []),
    )

    rc = maximalist.run(**_run_kwargs(stub_env))
    assert rc == 0

    settings_home = os.environ.get("COORDINATOR_SETTINGS_HOME")
    persisted = _receipt_module.load_receipt(settings_home_override=settings_home)
    assert persisted is not None
    assert persisted.reported("c4-test-static-writer") is True
    assert len(persisted.for_writer("c4-test-static-writer")) == 1


def test_unreported_writer_set_distinguishes_shaped_from_static_only(stub_env, monkeypatch):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    # A shaped-clause writer with no journal rows: unreported.
    shaped_decl = _WSD(
        writer_id="c4-test-shaped-writer",
        source_module="c4.test.shaped_writer",
        clauses=(
            _ShapedClause(
                entry_template=_WSE(kind="file-path", path="/tmp/<discovered>"),
                discovered_by="test discovery",
            ),
        ),
    )
    # A static-only writer: derives directly, never unreported for lack of a
    # journal row.
    static_decl = _WSD(
        writer_id="c4-test-static-only-writer",
        source_module="c4.test.static_only_writer",
        clauses=(
            _StaticClause(
                entries=(_WSE(kind="file-path", path="/tmp/static-only", reason="test"),),
            ),
        ),
    )
    monkeypatch.setattr(
        maximalist,
        "_collect_writer_declarations",
        lambda repo_root: (
            {
                "c4-test-shaped-writer": shaped_decl,
                "c4-test-static-only-writer": static_decl,
            },
            [],
        ),
    )

    rc = maximalist.run(**_run_kwargs(stub_env))
    assert rc == 0

    settings_home = os.environ.get("COORDINATOR_SETTINGS_HOME")
    persisted = _receipt_module.load_receipt(settings_home_override=settings_home)
    assert persisted is not None
    assert persisted.reported("c4-test-shaped-writer") is False
    assert persisted.reported("c4-test-static-only-writer") is True


def test_failing_receipt_persist_does_not_fail_the_install(stub_env, monkeypatch, capsys):
    def _boom(*args, **kwargs):
        raise _receipt_module.ReceiptPersistenceError("simulated persist failure")

    monkeypatch.setattr(_receipt_module, "persist_receipt", _boom)

    rc = maximalist.run(**_run_kwargs(stub_env))
    assert rc == 0
    assert "install-receipt build/persist failed" in capsys.readouterr().err


def test_writer_discovery_failure_is_loud_and_recorded_unreported(stub_env, monkeypatch, capsys):
    """Review: code-reviewer (P2) -- a module that fails to import during
    writer discovery must WARN loudly and be folded into
    `unreported_writer_ids`, never vanish from receipt coverage silently."""
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    from coordinator_core.ops import write_surface_manifest as _wsm_module

    monkeypatch.setattr(
        _wsm_module,
        "discover_declarations",
        lambda repo_root: ({}, [("some/broken_writer.py", "ImportError: boom")]),
    )

    rc = maximalist.run(**_run_kwargs(stub_env))
    assert rc == 0

    stderr = capsys.readouterr().err
    assert "some/broken_writer.py" in stderr
    assert "unreported" in stderr

    settings_home = os.environ.get("COORDINATOR_SETTINGS_HOME")
    persisted = _receipt_module.load_receipt(settings_home_override=settings_home)
    assert persisted is not None
    assert "<discovery-failed:some/broken_writer.py>" in persisted.unreported_writer_ids

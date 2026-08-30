"""
Co-located pytest for coordinator_core.install.first_run (BIG_PORT Wave C,
id: first-run). Independently re-derives parity against the retired bash
oracle's documented behavior rather than transcribing the port's own logic
back at itself -- see individual test docstrings for the oracle line/comment
each test is anchored to.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core.install import first_run as fr
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


# ---------------------------------------------------------------------------
# unit1 -- arg parsing (oracle L38-71).
# ---------------------------------------------------------------------------


def test_parse_args_no_flags_defaults_off():
    args = fr.parse_args([])
    assert args.dry_run is False
    assert args.confirm is False
    assert args.no_git_lfs is False
    assert args.non_interactive is False


@pytest.mark.parametrize("flag", ["--plan", "--dry-run"])
def test_parse_args_dry_run_aliases(flag):
    assert fr.parse_args([flag]).dry_run is True


@pytest.mark.parametrize("flag", ["--confirm", "--yes"])
def test_parse_args_confirm_aliases(flag):
    assert fr.parse_args([flag]).confirm is True


def test_parse_args_no_git_lfs():
    assert fr.parse_args(["--no-git-lfs"]).no_git_lfs is True


def test_parse_args_non_interactive_flag():
    assert fr.parse_args(["--non-interactive"]).non_interactive is True


def test_parse_args_non_interactive_env_var(monkeypatch):
    """Oracle L38-40: COORDINATOR_NON_INTERACTIVE env var maps to
    non-interactive mode even with zero CLI args."""
    monkeypatch.setenv("COORDINATOR_NON_INTERACTIVE", "1")
    assert fr.parse_args([]).non_interactive is True


def test_parse_args_combined_flags():
    args = fr.parse_args(["--no-git-lfs", "--confirm", "--non-interactive"])
    assert args.no_git_lfs and args.confirm and args.non_interactive


def test_parse_args_unknown_arg_raises():
    """Oracle L59-63: unknown arg prints usage and exits 1 — ported as a
    typed exception the caller (main) converts to EXIT_FAIL."""
    with pytest.raises(fr._UsageError) as exc_info:
        fr.parse_args(["--bogus-flag"])
    assert exc_info.value.unknown_arg == "--bogus-flag"


def test_main_unknown_arg_exits_fail(capsys):
    rc = fr.main(["--totally-unknown"])
    assert rc == fr.EXIT_FAIL
    err = capsys.readouterr().err
    assert "unknown argument: --totally-unknown" in err
    assert "Usage:" in err


# ---------------------------------------------------------------------------
# _derive_repo_key -- byte-parity against the oracle's own `tr` pipeline
# (L150-154): tr upper->lower, tr -cs 'a-z0-9' '_', sed strip leading/
# trailing underscore. Independently re-derived here by actually invoking
# the equivalent shell pipeline via subprocess, not by re-reading the port's
# source.
# ---------------------------------------------------------------------------


def _oracle_derive_key(repo_base: str) -> str:
    proc = subprocess.run(
        ["bash", "-c", "printf '%s' \"$1\" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '_' | sed 's/^_//;s/_$//'", "_", repo_base],
        capture_output=True,
        text=True,
        timeout=10,
        **no_console_creationflags(),
    )
    return proc.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
@pytest.mark.parametrize(
    "repo_base",
    [
        "claude-klabauter",
        "DoE-claude",
        "example_retrieval_repo-ue-addon",
        "My.Weird--Repo!!Name",
        "_leading_underscore",
        "trailing_underscore_",
        "UPPER-CASE-REPO",
        "single",
    ],
)
def test_derive_repo_key_matches_oracle_shell_pipeline(repo_base):
    assert fr._derive_repo_key(repo_base) == _oracle_derive_key(repo_base)


# ---------------------------------------------------------------------------
# build_plan -- text/step-shape parity (oracle L325-356).
# ---------------------------------------------------------------------------


def _all_missing_env() -> fr._Env:
    env = fr._Env()
    env.bash_ok = env.python_ok = env.node_ok = env.uv_ok = env.git_lfs_ok = env.brew_ok = False
    return env


def _all_present_env() -> fr._Env:
    env = fr._Env()
    env.bash_ok = env.python_ok = env.node_ok = env.uv_ok = env.git_lfs_ok = env.brew_ok = True
    return env


def test_build_plan_all_missing_lists_every_install_step():
    steps = fr.build_plan(_all_missing_env(), no_git_lfs=False)
    assert steps[0] == "install Homebrew (absent on this machine)"
    assert "brew install bash  (>=4.3 required; stock macOS is 3.2)" in steps
    assert "brew install python@3.12  (Python 3.11+ required)" in steps
    assert "brew install node" in steps
    assert "brew install uv" in steps
    assert "brew install git-lfs  then  git lfs install  (global, idempotent)" in steps
    assert steps[-1] == "tell you to /reload-plugins"
    assert "seed machine-local registry  (post-toolchain, C1b, Step 3)" in steps
    # Step 4b (ensure-coordinator-venv) is retired outright from this chain
    # (docs/plans/2026-08-18-retire-coordinator-venv.md chunk C4, AC5) -- the
    # documented plan line no longer names it.
    assert (
        "run install-substrate -> platform-localize  (post-toolchain, C1b, Step 4)"
        in steps
    )


def test_build_plan_all_present_only_lists_orchestration_tail():
    steps = fr.build_plan(_all_present_env(), no_git_lfs=False)
    assert not any("install Homebrew" in s for s in steps)
    assert not any("brew install" in s for s in steps)
    assert len(steps) == 3  # seed registry, run chain, reload-plugins


def test_build_plan_no_git_lfs_flag_emits_skip_line_even_when_present():
    """Review F1 regression (cited in the oracle's own comment L68-70):
    --no-git-lfs must show the SKIPPED line even when git-lfs happens to
    already be installed."""
    steps = fr.build_plan(_all_present_env(), no_git_lfs=True)
    assert "git-lfs SKIPPED (--no-git-lfs passed; LFS-backed clones will be pointer-only)" in steps
    assert not any("brew install git-lfs" in s for s in steps)


def test_build_plan_no_git_lfs_and_missing_prefers_skip_line():
    env = _all_missing_env()
    steps = fr.build_plan(env, no_git_lfs=True)
    assert "git-lfs SKIPPED (--no-git-lfs passed; LFS-backed clones will be pointer-only)" in steps
    assert not any("brew install git-lfs" in s for s in steps)


# ---------------------------------------------------------------------------
# main() — dry-run / confirm-gate branches (oracle L362-419).
# ---------------------------------------------------------------------------


def _stub_env(monkeypatch, **overrides):
    env = _all_present_env()
    for k, v in overrides.items():
        setattr(env, k, v)
    monkeypatch.setattr(fr, "detect_environment", lambda: env)
    return env


def test_main_dry_run_prints_plan_and_exits_ok(monkeypatch, capsys):
    _stub_env(monkeypatch)
    rc = fr.main(["--plan"])
    assert rc == fr.EXIT_OK
    out = capsys.readouterr().out
    assert "dry run (no changes will be made)" in out
    assert "about to:" in out
    assert "Exiting (--dry-run / --plan)" in out


def test_main_non_interactive_without_confirm_prints_and_exits_ok(monkeypatch, capsys):
    _stub_env(monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    rc = fr.main(["--non-interactive"])
    assert rc == fr.EXIT_OK
    out = capsys.readouterr().out
    assert "Non-interactive mode" in out
    assert "Re-run with --confirm" in out


def test_main_raw_non_tty_without_confirm_exits_fail(monkeypatch, capsys):
    """Oracle L410-414: raw non-TTY, neither NI nor --confirm => exit 1
    (genuinely unsafe to proceed)."""
    _stub_env(monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    rc = fr.main([])
    assert rc == fr.EXIT_FAIL
    err = capsys.readouterr().err
    assert "non-interactive shell detected and --confirm not passed" in err


def test_main_confirm_flag_proceeds_to_post_toolchain(monkeypatch, capsys):
    _stub_env(monkeypatch)
    monkeypatch.setattr(fr, "run_post_toolchain", lambda plugin_root, args: fr.EXIT_OK)
    rc = fr.main(["--confirm"])
    assert rc == fr.EXIT_OK
    out = capsys.readouterr().out
    assert "Proceeding (--confirm / --yes)." in out


def test_main_interactive_no_reply_defaults_to_yes(monkeypatch, capsys):
    """Oracle L396: bare Enter (empty reply) on the interactive prompt
    defaults to proceed, matching the case pattern `''|[Yy]|[Yy][Ee][Ss]`."""
    _stub_env(monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "")
    monkeypatch.setattr(fr, "run_post_toolchain", lambda plugin_root, args: fr.EXIT_OK)
    rc = fr.main([])
    assert rc == fr.EXIT_OK


def test_main_interactive_no_reply_aborts(monkeypatch, capsys):
    _stub_env(monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    rc = fr.main([])
    assert rc == fr.EXIT_OK
    assert "Aborted." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _bash_version_ok -- native reimplementation (2026-07-21 pure-Python-shop
# cutover): parses `bash --version`'s banner line instead of spawning
# `bash -c '<embedded BASH_VERSINFO probe>'`.
# ---------------------------------------------------------------------------


def test_bash_version_ok_parses_gnu_bash_banner(monkeypatch):
    fake = mock.MagicMock(returncode=0, stdout="GNU bash, version 5.2.15(1)-release (aarch64-apple-darwin23)\n")
    monkeypatch.setattr(fr, "_run", lambda *a, **k: fake)
    assert fr._bash_version_ok("/opt/homebrew/bin/bash") is True


def test_bash_version_ok_rejects_stock_macos_bash_3_2(monkeypatch):
    fake = mock.MagicMock(returncode=0, stdout="GNU bash, version 3.2.57(1)-release (arm64-apple-darwin23)\n")
    monkeypatch.setattr(fr, "_run", lambda *a, **k: fake)
    assert fr._bash_version_ok("/bin/bash") is False


def test_bash_version_ok_boundary_4_3_is_ok(monkeypatch):
    fake = mock.MagicMock(returncode=0, stdout="GNU bash, version 4.3.0(1)-release\n")
    monkeypatch.setattr(fr, "_run", lambda *a, **k: fake)
    assert fr._bash_version_ok("/bin/bash") is True


def test_bash_version_ok_nonzero_exit_is_false(monkeypatch):
    fake = mock.MagicMock(returncode=1, stdout="")
    monkeypatch.setattr(fr, "_run", lambda *a, **k: fake)
    assert fr._bash_version_ok("/bin/bash") is False


def test_bash_version_ok_unparseable_output_is_false(monkeypatch):
    fake = mock.MagicMock(returncode=0, stdout="not a version string\n")
    monkeypatch.setattr(fr, "_run", lambda *a, **k: fake)
    assert fr._bash_version_ok("/bin/bash") is False


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
def test_bash_version_ok_matches_live_bash_on_path():
    """Parity smoke against the real bash binary, if present — confirms the
    `--version` banner regex actually matches this machine's bash, not just
    synthetic fixtures."""
    live_path = shutil.which("bash")
    result = fr._bash_version_ok(live_path)
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# run_post_toolchain — step-failure propagation (oracle L169-241, fail-loud
# on every step per the module docstring's negative-spec note re: Step 4c).
# ---------------------------------------------------------------------------


def test_run_post_toolchain_never_calls_ensure_venv(tmp_path, monkeypatch):
    """Step 4b (ensure-coordinator-venv) is retired outright from this
    module (docs/plans/2026-08-18-retire-coordinator-venv.md chunk C4,
    AC5): `ensure_coordinator_venv` is reachable only via the explicit
    `--allow-venv-fallback` opt-in elsewhere in the install chain
    (`scripts/setup.py`, `substrate.py`'s Step C10a-3) — first-run.py's own
    CLI carries no such flag, so a failing (or even present) implementation
    of `ensure_coordinator_venv` must never be reached from here, and the
    chain completes successfully regardless of it."""
    plugin_root = tmp_path / "coordinator"
    (plugin_root / "bin").mkdir(parents=True)
    (plugin_root / "scripts").mkdir(parents=True)
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(claude_klabauter_root))
    monkeypatch.setattr(
        fr, "_seed_machine_local_registry", lambda *a, **k: None
    )
    # Stub install-substrate to succeed.
    fake_substrate = mock.MagicMock()
    fake_substrate.main.return_value = 0
    monkeypatch.setitem(sys.modules, "coordinator_core.install.substrate", fake_substrate)

    import coordinator_core.install.ensure_venv as ev

    def _raise(*a, **k):
        raise ev.EnsureVenvError("[ensure-coordinator-venv] ERROR: simulated failure")

    monkeypatch.setattr(ev, "ensure_coordinator_venv", _raise)

    args = fr._Args()
    rc = fr.run_post_toolchain(plugin_root, args)
    assert rc == fr.EXIT_OK


def test_run_post_toolchain_platform_localize_nonzero_fails(tmp_path, monkeypatch):
    """Step 4c (native in-process call, 2026-07-21 pure-Python-shop cutover):
    a non-zero return from coordinator_core.hooks.platform_localize.main
    propagates as EXIT_FAIL. Supersedes the pre-cutover
    'platform-localize.sh resolved at the wrong path' guard test — that
    guard's premise (a bash-spawned .sh file on disk) no longer exists
    post-port; see the module docstring's retired-bug note."""
    plugin_root = tmp_path / "coordinator"
    (plugin_root / "bin").mkdir(parents=True)
    (plugin_root / "scripts").mkdir(parents=True)
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(claude_klabauter_root))

    monkeypatch.setattr(fr, "_seed_machine_local_registry", lambda *a, **k: None)
    fake_substrate = mock.MagicMock()
    fake_substrate.main.return_value = 0
    monkeypatch.setitem(sys.modules, "coordinator_core.install.substrate", fake_substrate)

    import coordinator_core.install.ensure_venv as ev

    monkeypatch.setattr(ev, "ensure_coordinator_venv", lambda *a, **k: "ready")

    fake_platform_localize = mock.MagicMock()
    fake_platform_localize.main.return_value = 1
    monkeypatch.setitem(sys.modules, "coordinator_core.hooks.platform_localize", fake_platform_localize)

    args = fr._Args()
    rc = fr.run_post_toolchain(plugin_root, args)
    assert rc == fr.EXIT_FAIL
    fake_platform_localize.main.assert_called_once_with([])


def test_run_post_toolchain_platform_localize_import_error_fails(tmp_path, monkeypatch):
    plugin_root = tmp_path / "coordinator"
    (plugin_root / "bin").mkdir(parents=True)
    (plugin_root / "scripts").mkdir(parents=True)
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(claude_klabauter_root))

    monkeypatch.setattr(fr, "_seed_machine_local_registry", lambda *a, **k: None)
    fake_substrate = mock.MagicMock()
    fake_substrate.main.return_value = 0
    monkeypatch.setitem(sys.modules, "coordinator_core.install.substrate", fake_substrate)

    import coordinator_core.install.ensure_venv as ev

    monkeypatch.setattr(ev, "ensure_coordinator_venv", lambda *a, **k: "ready")

    monkeypatch.delitem(sys.modules, "coordinator_core.hooks.platform_localize", raising=False)

    import builtins
    real_import = builtins.__import__

    def _blocking_import(name, *a, **k):
        if name == "coordinator_core.hooks.platform_localize":
            raise ImportError("simulated transport failure")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)

    args = fr._Args()
    rc = fr.run_post_toolchain(plugin_root, args)
    assert rc == fr.EXIT_FAIL


def test_run_post_toolchain_install_substrate_import_error(tmp_path, monkeypatch):
    plugin_root = tmp_path / "coordinator"
    (plugin_root / "bin").mkdir(parents=True)
    (plugin_root / "scripts").mkdir(parents=True)
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(claude_klabauter_root))
    monkeypatch.setattr(fr, "_seed_machine_local_registry", lambda *a, **k: None)
    monkeypatch.delitem(sys.modules, "coordinator_core.install.substrate", raising=False)

    import builtins
    real_import = builtins.__import__

    def _blocking_import(name, *a, **k):
        if name == "coordinator_core.install.substrate":
            raise ImportError("simulated transport failure")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)
    args = fr._Args()
    rc = fr.run_post_toolchain(plugin_root, args)
    assert rc == fr.EXIT_FAIL


def test_run_post_toolchain_success_runs_all_steps_in_order(tmp_path, monkeypatch, capsys):
    """AC5-parity (retired coordinator/scripts/tests/first-run-regeneration.bats
    Test 1): asserts install-substrate -> platform-localize call order via
    output-marker ordering, plus the closing /reload-plugins instruction.
    Step 4b (ensure-coordinator-venv) is retired outright from this chain
    (docs/plans/2026-08-18-retire-coordinator-venv.md chunk C4, AC5) and no
    longer sits between the two. See
    coordinator/scripts/tests/first-run-regeneration.bats's retirement
    pointer (2026-07-17 BIG_PORT Wave C sidecar, EM directive) -- this test
    is the Python-native replacement for that bash-fixture E2E coverage."""
    plugin_root = tmp_path / "coordinator"
    (plugin_root / "bin").mkdir(parents=True)
    (plugin_root / "scripts").mkdir(parents=True)
    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(claude_klabauter_root))

    monkeypatch.setattr(fr, "_seed_machine_local_registry", lambda *a, **k: None)
    fake_substrate = mock.MagicMock()
    fake_substrate.main.return_value = 0
    monkeypatch.setitem(sys.modules, "coordinator_core.install.substrate", fake_substrate)

    import coordinator_core.install.ensure_venv as ev

    def _fail_if_called(*a, **k):
        raise AssertionError(
            "ensure_coordinator_venv must not be reached from first_run.py "
            "without --allow-venv-fallback (chunk C4, AC5)"
        )

    monkeypatch.setattr(ev, "ensure_coordinator_venv", _fail_if_called)

    fake_platform_localize = mock.MagicMock()
    fake_platform_localize.main.return_value = 0
    monkeypatch.setitem(sys.modules, "coordinator_core.hooks.platform_localize", fake_platform_localize)

    args = fr._Args()
    args.no_git_lfs = True  # skip real `git lfs install` mutation in CI
    rc = fr.run_post_toolchain(plugin_root, args)
    assert rc == fr.EXIT_OK
    fake_substrate.main.assert_called_once_with([])
    fake_platform_localize.main.assert_called_once_with([])

    out = capsys.readouterr().out
    idx_substrate = out.index("install-substrate: done.")
    idx_localize = out.index("platform-localize: done.")
    assert idx_substrate < idx_localize
    assert "ensure-coordinator-venv" not in out
    assert "/reload-plugins" in out


def test_run_post_toolchain_preflight_calls_setup_chain_walker_in_process(tmp_path, monkeypatch):
    """Step 2 (native in-process call, 2026-07-21 pure-Python-shop cutover):
    when the claude-klabauter-side `coordinator/` tree is present (post b644d5a9
    executable-surface relocation), run_post_toolchain must call
    coordinator_core.ops.setup_chain_walker.main(["--preflight"]) in-process
    with COORDINATOR_SETUP_REPO_ROOT/LIB_DIR resolved off
    coordinator_claude_klabauter_root() -- never plugin_root -- and must NOT spawn
    bash. Formerly `bash <plugin_root>/scripts/setup.sh --preflight`."""
    plugin_root = tmp_path / "coordinator"
    (plugin_root / "bin").mkdir(parents=True)
    (plugin_root / "scripts").mkdir(parents=True)
    claude_klabauter_root = tmp_path / "claude-klabauter"
    coordinator_tree_root = claude_klabauter_root / "coordinator"
    coordinator_tree_root.mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(claude_klabauter_root))

    monkeypatch.setattr(fr, "_seed_machine_local_registry", lambda *a, **k: None)
    fake_substrate = mock.MagicMock()
    fake_substrate.main.return_value = 0
    monkeypatch.setitem(sys.modules, "coordinator_core.install.substrate", fake_substrate)

    import coordinator_core.install.ensure_venv as ev

    monkeypatch.setattr(ev, "ensure_coordinator_venv", lambda *a, **k: "ready")

    fake_platform_localize = mock.MagicMock()
    fake_platform_localize.main.return_value = 0
    monkeypatch.setitem(sys.modules, "coordinator_core.hooks.platform_localize", fake_platform_localize)

    captured_env = {}
    fake_walker = mock.MagicMock()

    def _fake_walker_main(argv):
        captured_env["repo_root"] = fr.os.environ.get("COORDINATOR_SETUP_REPO_ROOT")
        captured_env["lib_dir"] = fr.os.environ.get("COORDINATOR_SETUP_LIB_DIR")
        captured_env["argv"] = list(argv)
        return 0

    fake_walker.main.side_effect = _fake_walker_main
    monkeypatch.setitem(sys.modules, "coordinator_core.ops.setup_chain_walker", fake_walker)

    args = fr._Args()
    args.no_git_lfs = True
    rc = fr.run_post_toolchain(plugin_root, args)
    assert rc == fr.EXIT_OK
    assert captured_env["argv"] == ["--preflight"]
    assert captured_env["repo_root"] == str(coordinator_tree_root)
    assert captured_env["lib_dir"] == str(coordinator_tree_root / "scripts" / "lib")


def test_run_post_toolchain_preflight_failure_is_non_fatal(tmp_path, monkeypatch, capsys):
    """Mirrors the retired oracle's `|| true`: a raised exception from the
    preflight walker must not fail run_post_toolchain."""
    plugin_root = tmp_path / "coordinator"
    (plugin_root / "bin").mkdir(parents=True)
    (plugin_root / "scripts").mkdir(parents=True)
    claude_klabauter_root = tmp_path / "claude-klabauter"
    (claude_klabauter_root / "coordinator").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(claude_klabauter_root))

    monkeypatch.setattr(fr, "_seed_machine_local_registry", lambda *a, **k: None)
    fake_substrate = mock.MagicMock()
    fake_substrate.main.return_value = 0
    monkeypatch.setitem(sys.modules, "coordinator_core.install.substrate", fake_substrate)

    import coordinator_core.install.ensure_venv as ev

    monkeypatch.setattr(ev, "ensure_coordinator_venv", lambda *a, **k: "ready")

    fake_platform_localize = mock.MagicMock()
    fake_platform_localize.main.return_value = 0
    monkeypatch.setitem(sys.modules, "coordinator_core.hooks.platform_localize", fake_platform_localize)

    fake_walker = mock.MagicMock()

    def _raise(argv):
        raise RuntimeError("simulated preflight walker crash")

    fake_walker.main.side_effect = _raise
    monkeypatch.setitem(sys.modules, "coordinator_core.ops.setup_chain_walker", fake_walker)

    args = fr._Args()
    args.no_git_lfs = True
    rc = fr.run_post_toolchain(plugin_root, args)
    assert rc == fr.EXIT_OK


# ---------------------------------------------------------------------------
# _seed_machine_local_registry — Step 3's own body (Review: code-reviewer --
# Finding 5, 2026-07-17 BIG_PORT Wave C sidecar: every run_post_toolchain
# test stubs this function out entirely; nothing exercised its prompt-gate,
# not-found WARNING branches, repo-discovery loop, or subprocess-failure
# paths. These tests target the function directly instead.
# ---------------------------------------------------------------------------


def test_seed_registry_missing_claude_klabauter_root_warns_and_returns(monkeypatch, capsys):
    """CLAUDE_KLABAUTER_ROOT unresolvable -> WARNING + manual-registration hint
    (never a raised exception -- Step 3 is a convenience, not load-bearing;
    see the function's own docstring)."""
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)

    def _raise(*a, **k):
        raise RuntimeError("simulated CLAUDE_KLABAUTER_ROOT resolution failure")

    monkeypatch.setattr(fr, "coordinator_engine_root_with_class", _raise)

    fr._seed_machine_local_registry(confirm=True, non_interactive=True)

    err = capsys.readouterr().err
    assert "cannot resolve CLAUDE_KLABAUTER_ROOT to locate machine-local" in err


def test_seed_registry_confirm_gate_skip_declines(monkeypatch, tmp_path, capsys):
    """Interactive prompt, reply not in ('', 'y', 'yes') -> seeding skipped,
    discovery/registration never attempted."""
    claude_klabauter_root = tmp_path / "claude-klabauter"
    (claude_klabauter_root / "coordinator" / "bin").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(claude_klabauter_root))

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "n")

    def _unexpected_discover(argv):
        raise AssertionError("discovery should not run on the declined-confirm path")

    monkeypatch.setattr(fr, "_discover_working_repos_main", _unexpected_discover)

    def _unexpected_registry_set(*a, **k):
        raise AssertionError("registry_set should not be called on the declined-confirm path")

    monkeypatch.setattr(fr, "registry_set", _unexpected_registry_set)

    fr._seed_machine_local_registry(confirm=False, non_interactive=False)

    out = capsys.readouterr().out
    assert "Registry seeding skipped" in out


def test_seed_registry_happy_path_writes_expected_entries(monkeypatch, tmp_path, capsys):
    """confirm=True skips the prompt entirely; the stubbed in-process
    discovery call prints two repo paths to stdout (captured via
    redirect_stdout). This is an end-to-end exercise of the REAL
    `registry_set` in-process writer (not mocked) -- asserts the on-disk
    `registry.local.toml` actually gains the expected `repos.<key> = <path>`
    entries, byte-parity with `_derive_repo_key`."""
    claude_klabauter_root = tmp_path / "claude-klabauter"
    (claude_klabauter_root / "coordinator" / "bin").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(claude_klabauter_root))

    registry_dir = tmp_path / "registry"
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))

    def _fake_discover(argv):
        print("/x/claude-klabauter")
        print("/x/DoE-claude")
        return 0

    monkeypatch.setattr(fr, "_discover_working_repos_main", _fake_discover)

    fr._seed_machine_local_registry(confirm=True, non_interactive=False)

    out = capsys.readouterr().out
    assert "Registering repos.claude_klabauter = /x/claude-klabauter" in out
    assert "Registering repos.doe_claude = /x/DoE-claude" in out

    registry_file = registry_dir / "registry.local.toml"
    assert registry_file.is_file()
    content = registry_file.read_text(encoding="utf-8")
    assert "\"repos.claude_klabauter\" = '/x/claude-klabauter'" in content
    assert "\"repos.doe_claude\" = '/x/DoE-claude'" in content

    from coordinator_core.machine_resolver import registry_get

    assert registry_get("repos.claude_klabauter") == "/x/claude-klabauter"
    assert registry_get("repos.doe_claude") == "/x/DoE-claude"


def test_seed_registry_no_repos_discovered_prints_manual_hint(monkeypatch, tmp_path, capsys):
    claude_klabauter_root = tmp_path / "claude-klabauter"
    (claude_klabauter_root / "coordinator" / "bin").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(claude_klabauter_root))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "registry"))

    monkeypatch.setattr(fr, "_discover_working_repos_main", lambda argv: 0)

    fr._seed_machine_local_registry(confirm=True, non_interactive=False)

    out = capsys.readouterr().out
    assert "No repos discovered. Register later" in out


def test_seed_registry_discover_failure_warns(monkeypatch, tmp_path, capsys):
    """In-process discovery raising an unexpected error degrades to a
    WARNING, not a crash -- mirrors the never-block posture of
    discover_working_repos.main() itself and the prior subprocess-failure
    disposition this replaces."""
    claude_klabauter_root = tmp_path / "claude-klabauter"
    (claude_klabauter_root / "coordinator" / "bin").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(claude_klabauter_root))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "registry"))

    def _raising_discover(argv):
        raise RuntimeError("boom")

    monkeypatch.setattr(fr, "_discover_working_repos_main", _raising_discover)

    fr._seed_machine_local_registry(confirm=True, non_interactive=False)

    captured = capsys.readouterr()
    assert "working-repo discovery failed" in captured.err
    assert "No repos discovered. Register later" in captured.out


def test_seed_registry_write_failure_warns_and_continues(monkeypatch, tmp_path, capsys):
    """A registry_set failure (ValueError/OSError) on one repo warns and
    continues -- Step 3 stays warn-and-continue, never fail-closed on a
    registry write problem (module docstring's warn-and-continue posture)."""
    claude_klabauter_root = tmp_path / "claude-klabauter"
    (claude_klabauter_root / "coordinator" / "bin").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(claude_klabauter_root))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "registry"))

    def _fake_discover(argv):
        print("/x/claude-klabauter")
        return 0

    monkeypatch.setattr(fr, "_discover_working_repos_main", _fake_discover)

    def _raising_registry_set(key, value):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(fr, "registry_set", _raising_registry_set)

    fr._seed_machine_local_registry(confirm=True, non_interactive=False)

    err = capsys.readouterr().err
    assert "failed to register repos.claude_klabauter" in err


# ---------------------------------------------------------------------------
# Fresh-install-shape smoke test (family_i: true — this IS an onboarding
# surface). Verifies the module is importable and main() degrades sanely
# with no CLAUDE_PLUGIN_ROOT set and no live toolchain mutation triggered
# (dry-run only — never actually installs Homebrew/brew formulae in CI).
# ---------------------------------------------------------------------------


def test_fresh_install_shape_dry_run_no_plugin_root_env(monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    rc = fr.main(["--plan"])
    assert rc == fr.EXIT_OK
    out = capsys.readouterr().out
    assert "about to:" in out


def test_module_importable_standalone():
    """Cold-machine import smoke: the module must not raise at import time
    even before any toolchain/registry state exists."""
    import importlib

    reloaded = importlib.reload(fr)
    assert callable(reloaded.main)


# ---------------------------------------------------------------------------
# resolution-journal wiring (C7 of docs/research/2026-08-06-install-receipt-
# persistence-design.md) — clause 0, the sole SHAPED clause
# (`_REPOS_REGISTRY_CLAUSE_INDEX`, the `repos.<derived-key>` machine-local
# registry seed via `_seed_machine_local_registry`).
# ---------------------------------------------------------------------------


@pytest.fixture
def _journal_env(tmp_path, monkeypatch):
    from coordinator_core.install import resolution_journal as journal_mod

    journal_path = tmp_path / "journal" / "resolution-journal.jsonl"
    monkeypatch.setenv(journal_mod.RESOLUTION_JOURNAL_ENV_VAR, str(journal_path))
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    return journal_mod


def _resolved(journal_mod):
    journal = journal_mod.read_journal()
    return journal.get("first-run", {}).get(fr._REPOS_REGISTRY_CLAUSE_INDEX)


def test_journal_records_registered_repo_keys(monkeypatch, tmp_path, _journal_env):
    claude_klabauter_root = tmp_path / "claude-klabauter"
    (claude_klabauter_root / "coordinator" / "bin").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(claude_klabauter_root))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "registry"))

    def _fake_discover(argv):
        print("/x/claude-klabauter")
        print("/x/DoE-claude")
        return 0

    monkeypatch.setattr(fr, "_discover_working_repos_main", _fake_discover)

    fr._seed_machine_local_registry(confirm=True, non_interactive=False)

    resolution = _resolved(_journal_env)
    assert resolution is not None
    keys = {e.key for e in resolution.entries}
    assert keys == {"repos.claude_klabauter", "repos.doe_claude"}
    assert all(e.kind == "machine-local-key" for e in resolution.entries)


def test_journal_omits_failed_registration_from_written_entries(monkeypatch, tmp_path, _journal_env):
    """A `registry_set` call that raises performed no write — only the
    genuinely-succeeded registration is journaled."""
    claude_klabauter_root = tmp_path / "claude-klabauter"
    (claude_klabauter_root / "coordinator" / "bin").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(claude_klabauter_root))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "registry"))

    def _fake_discover(argv):
        print("/x/claude-klabauter")
        print("/x/broken-repo")
        return 0

    monkeypatch.setattr(fr, "_discover_working_repos_main", _fake_discover)

    real_registry_set = fr.registry_set

    def _flaky_registry_set(key, value):
        if key == "repos.broken_repo":
            raise OSError("simulated write failure")
        return real_registry_set(key, value)

    monkeypatch.setattr(fr, "registry_set", _flaky_registry_set)

    fr._seed_machine_local_registry(confirm=True, non_interactive=False)

    resolution = _resolved(_journal_env)
    assert resolution is not None
    keys = {e.key for e in resolution.entries}
    assert keys == {"repos.claude_klabauter"}


def test_journal_empty_entries_on_no_repos_discovered(monkeypatch, tmp_path, _journal_env):
    claude_klabauter_root = tmp_path / "claude-klabauter"
    (claude_klabauter_root / "coordinator" / "bin").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(claude_klabauter_root))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "registry"))

    monkeypatch.setattr(fr, "_discover_working_repos_main", lambda argv: 0)

    fr._seed_machine_local_registry(confirm=True, non_interactive=False)

    resolution = _resolved(_journal_env)
    assert resolution is not None
    assert resolution.entries == ()


def test_journal_empty_entries_on_declined_confirm(monkeypatch, tmp_path, _journal_env):
    claude_klabauter_root = tmp_path / "claude-klabauter"
    (claude_klabauter_root / "coordinator" / "bin").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(claude_klabauter_root))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "registry"))

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "n")

    fr._seed_machine_local_registry(confirm=False, non_interactive=False)

    resolution = _resolved(_journal_env)
    assert resolution is not None
    assert resolution.entries == ()


def test_journal_empty_entries_on_missing_claude_klabauter_root(monkeypatch, _journal_env, capsys):
    """CLAUDE_KLABAUTER_ROOT unresolvable is a definitive 'resolved to nothing' for
    this run (the registry-seeding precondition failed, not merely
    unreached) — mirrors `_seed_machine_local_registry`'s own comment at
    that call site."""
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)

    def _raise(*a, **k):
        raise RuntimeError("simulated CLAUDE_KLABAUTER_ROOT resolution failure")

    monkeypatch.setattr(fr, "coordinator_engine_root_with_class", _raise)

    fr._seed_machine_local_registry(confirm=True, non_interactive=True)

    resolution = _resolved(_journal_env)
    assert resolution is not None
    assert resolution.entries == ()


def test_journal_omits_entry_when_mutation_disabled(monkeypatch, tmp_path, _journal_env):
    """`first_run.py` does not itself gate `_seed_machine_local_registry` on
    `COORDINATOR_DISABLE_MACHINE_MUTATION` — only the journal append does,
    via `resolution_journal.record_resolution`'s own guard. The
    `registry_set` calls still fire; only this clause's journal row is
    refused, leaving it UNREPORTED for this run."""
    claude_klabauter_root = tmp_path / "claude-klabauter"
    (claude_klabauter_root / "coordinator" / "bin").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(claude_klabauter_root))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "registry"))

    def _fake_discover(argv):
        print("/x/claude-klabauter")
        return 0

    monkeypatch.setattr(fr, "_discover_working_repos_main", _fake_discover)

    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")

    fr._seed_machine_local_registry(confirm=True, non_interactive=False)

    assert _resolved(_journal_env) is None

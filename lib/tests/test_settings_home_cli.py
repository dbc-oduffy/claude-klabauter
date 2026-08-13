"""
Unit tests for the settings-home resolution seam's CLI and Python resolver
surfaces (T6-T14).

Port of: coordinator/lib/tests/test-settings-home.sh (T6-T14).

T1-T5 (the former lib/settings-home.sh direct-source coverage) already live
as a pytest port in coordinator/tests/test_settings_home.py — not
re-ported here, per that file's own docstring.

The bash oracle this replaces targeted
coordinator/templates/bin/coordinator-settings-home, a path that no longer
exists (the CLI moved to coordinator/bin/coordinator-settings-home, an
extensionless Python entry point — verified: running the bash oracle today
produces "FATAL: required file not found" at the old path). This port
targets the current, live CLI location instead of reproducing the stale
path.

Spec backlink: docs/plans/2026-07-06-durable-substrate-to-settings-home.md § C1
Port backlink: docs/plans/2026-08-13-grind-the-posix-exec-baseline-to-zero.md
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
CLAUDE_HOME_PY = _REPO_ROOT / "coordinator" / "lib" / "claude-home" / "_claude_home.py"

# coordinator/bin/coordinator-settings-home in THIS repo is a bare-name
# forwarder to a harness-injected ~/.claude/bin copy (per its own docstring:
# "Forwards to the CLAUDE_HOME-resolved ~/.claude/bin/coordinator-settings-home"),
# not the resolver itself — verified: invoking it against a scratch HOME
# raises "resolver not installed ... run /coordinator:setup". The actual
# resolver (the bash oracle's original target, once at
# templates/bin/coordinator-settings-home) lives only in the example-doctrine-repo
# sibling repo now — same cross-repo boundary as detect-hardware.sh/
# spawn-hidden.sh's caller class (this repo's own CLAUDE.md: "Discovery-
# resolved surfaces ... belong in coordinator-claude, not here"). T6-T8
# resolve it via the example-doctrine-repo root pointer and skip (not fail) when that
# sibling checkout is unavailable on this machine.
_CLI_REL = "coordinator/templates/bin/coordinator-settings-home"


def _resolve_cli() -> Path | None:
    from coordinator_core.doe_root_pointer import read_doe_root_pointer

    doe_root = read_doe_root_pointer()
    if not doe_root:
        return None
    candidate = Path(doe_root) / _CLI_REL
    return candidate if candidate.is_file() else None


def test_claude_home_py_exists():
    assert CLAUDE_HOME_PY.is_file(), f"required file not found: {CLAUDE_HOME_PY}"


def _base_env(fake_home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("COORDINATOR_SETTINGS_HOME", None)
    env.pop("CLAUDE_HOME", None)
    env["HOME"] = str(fake_home)
    return env


def _run_cli(fake_home: Path, subcmd: str | None = None, extra_env: dict[str, str] | None = None):
    cli = _resolve_cli()
    if cli is None:
        pytest.skip(
            "example-doctrine-repo root not resolvable via coordinator_core.doe_root_pointer "
            "on this machine (or the resolver is missing there) — the CLI this "
            "test targets is not vendored in claude-klabauter; not a defect in "
            "claude-klabauter."
        )
    env = _base_env(fake_home)
    if extra_env:
        env.update(extra_env)
    argv = [sys.executable, str(cli)]
    if subcmd:
        argv.append(subcmd)
    result = subprocess.run(
        argv, env=env, capture_output=True, text=True, creationflags=_CREATIONFLAGS
    )
    return result.returncode, result.stdout.strip()


def _run_py(fake_home: Path, subcmd: str, extra_env: dict[str, str] | None = None):
    env = _base_env(fake_home)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [sys.executable, str(CLAUDE_HOME_PY), subcmd],
        env=env,
        capture_output=True,
        text=True,
        creationflags=_CREATIONFLAGS,
    )
    return result.returncode, result.stdout.strip()


def test_t6_cli_no_args_prints_settings_home_path(tmp_path):
    override = tmp_path / "t6-sh"
    rc, out = _run_cli(tmp_path / "fakehome", extra_env={"COORDINATOR_SETTINGS_HOME": str(override)})
    assert rc == 0
    assert out == str(override)


def test_t7_cli_check_divergent_homes_nonzero(tmp_path):
    # Both machine-local dirs must carry actual content — an empty dir now
    # counts as a completed-migration husk, not a second content home (see
    # _is_absent_or_empty_husk in both the CLI resolver and _claude_home.py),
    # so this fixture writes a marker file into each to trigger divergence.
    home_dir = tmp_path / "home"
    settings_dir = tmp_path / "settings"
    (home_dir / ".claude" / "machine-local").mkdir(parents=True)
    (settings_dir / "machine-local").mkdir(parents=True)
    (home_dir / ".claude" / "machine-local" / "marker.toml").write_text("x = 1\n")
    (settings_dir / "machine-local" / "marker.toml").write_text("x = 1\n")
    rc, _out = _run_cli(
        tmp_path / "fakehome",
        subcmd="check",
        extra_env={"CLAUDE_HOME": str(home_dir), "COORDINATOR_SETTINGS_HOME": str(settings_dir)},
    )
    assert rc != 0


def test_t8_cli_check_compat_symlink_zero(tmp_path):
    settings_dir = tmp_path / "settings"
    home_dir = tmp_path / "home"
    (settings_dir / "machine-local").mkdir(parents=True)
    (home_dir / ".claude").mkdir(parents=True)
    (home_dir / ".claude" / "machine-local").symlink_to(settings_dir / "machine-local")
    rc, _out = _run_cli(
        tmp_path / "fakehome",
        subcmd="check",
        extra_env={"CLAUDE_HOME": str(home_dir), "COORDINATOR_SETTINGS_HOME": str(settings_dir)},
    )
    assert rc == 0


def test_t9_python_coordinator_settings_home_wins(tmp_path):
    override = tmp_path / "t9-py-override"
    rc, out = _run_py(tmp_path / "fakehome", "settings-home", {"COORDINATOR_SETTINGS_HOME": str(override)})
    assert rc == 0
    assert out == str(override)


def test_t10_python_claude_home_relative_fallback(tmp_path):
    claude_home = tmp_path / "t10-ch"
    expected = claude_home / ".coordinator-claude-settings"
    rc, out = _run_py(tmp_path / "fakehome", "settings-home", {"CLAUDE_HOME": str(claude_home)})
    assert rc == 0
    assert out == str(expected)


def test_t11_python_sandbox_redirect_via_claude_home(tmp_path):
    sandbox = tmp_path / "t11-sandbox"
    expected = sandbox / ".coordinator-claude-settings"
    env = dict(os.environ)
    env.pop("COORDINATOR_SETTINGS_HOME", None)
    env["HOME"] = str(sandbox)
    env["CLAUDE_HOME"] = str(sandbox)
    result = subprocess.run(
        [sys.executable, str(CLAUDE_HOME_PY), "settings-home"],
        env=env,
        capture_output=True,
        text=True,
        creationflags=_CREATIONFLAGS,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == str(expected)


def test_t12_python_machine_local_delegates_to_settings_home(tmp_path):
    settings_home = tmp_path / "t12-sh"
    expected = settings_home / "machine-local"
    rc, out = _run_py(tmp_path / "fakehome", "machine-local", {"COORDINATOR_SETTINGS_HOME": str(settings_home)})
    assert rc == 0
    assert out == str(expected)


def test_t13_python_machine_local_warns_but_continues_on_divergent_realpaths(tmp_path):
    # See test_t7's comment: both dirs need content, not just existence, to
    # register as divergent under current _is_absent_or_empty_husk semantics.
    #
    # DELIBERATE BEHAVIOR CHANGE from the bash oracle's T13 (which asserted
    # fail-loud/non-zero): _claude_home.py's `machine-local` subcommand no
    # longer fails loud on divergence. It now WARNS on stderr and continues,
    # deterministically preferring settings-home — verified by direct
    # invocation; the tool's own stderr message says so explicitly
    # ("DIVERGENT MACHINE-LOCAL HOMES — CONTINUING, preferring settings-home
    # (new)... the substrate->settings-home migration is now performed
    # natively by the coordinator install step"). This is intentional product
    # evolution, not a latent bug — the port asserts CURRENT behavior rather
    # than reproducing the stale fail-loud assertion.
    home_dir = tmp_path / "t13" / "home"
    settings_dir = tmp_path / "t13" / "settings"
    (home_dir / ".claude" / "machine-local").mkdir(parents=True)
    (settings_dir / "machine-local").mkdir(parents=True)
    (home_dir / ".claude" / "machine-local" / "marker.toml").write_text("x = 1\n")
    (settings_dir / "machine-local" / "marker.toml").write_text("x = 1\n")
    env = _base_env(tmp_path / "fakehome")
    env["CLAUDE_HOME"] = str(home_dir)
    env["COORDINATOR_SETTINGS_HOME"] = str(settings_dir)
    result = subprocess.run(
        [sys.executable, str(CLAUDE_HOME_PY), "machine-local"],
        env=env,
        capture_output=True,
        text=True,
        creationflags=_CREATIONFLAGS,
    )
    assert result.returncode == 0
    assert "DIVERGENT MACHINE-LOCAL HOMES" in result.stderr
    assert result.stdout.strip() == str(settings_dir / "machine-local")


def test_t14_python_machine_local_compat_symlink_does_not_fail(tmp_path):
    settings_dir = tmp_path / "t14" / "settings"
    home_dir = tmp_path / "t14" / "home"
    (settings_dir / "machine-local").mkdir(parents=True)
    (home_dir / ".claude").mkdir(parents=True)
    (home_dir / ".claude" / "machine-local").symlink_to(settings_dir / "machine-local")
    rc, out = _run_py(
        tmp_path / "fakehome",
        "machine-local",
        {"CLAUDE_HOME": str(home_dir), "COORDINATOR_SETTINGS_HOME": str(settings_dir)},
    )
    assert rc == 0
    assert out == str(settings_dir / "machine-local")

"""
coordinator_core.plugin_health.tests.test_drift

Pytest port of coordinator-claude coordinator/bin/check-plugin-drift.test.sh — the Leg-3
working-tree-root regression net pinning fix d82ba601 + follow-up 9acb8ab9:
`git -C <live_path> status` on a data-only live path NESTED inside a parent git
repo (no nested .git, e.g. Example-retrieval-repo's ~/.claude/plugins/example-retrieval-repo) used to
escape UPWARD to the enclosing repo and report the parent's transient dirty tree
as the plugin's own — a perpetual false "[drift] working-tree ... refresh
blocked" on every active session. git_worktree_root_if_self() encapsulates the
guard (None -> Leg 3 skipped).

The original .test.sh sourced check-plugin-drift.sh via a CHECK_PLUGIN_DRIFT_
LIB_ONLY=1 sourcing hatch — that hatch cannot survive the port (there is no bash
file left to source). This module imports git_worktree_root_if_self() and
check_plugin() directly instead; every scenario (A-F) is preserved.

Adds two NEW regression cases the bash suite never exercised (both plan-directed
behavior deltas from the port, not ports of existing bash coverage):
  - test_mapping_check_rejects_malicious_finder: the eval() -> ast.literal_eval
    hardening in _venv_mapping_check — a finder file with `__import__('os')` in
    its MAPPING body must resolve to NO_FINDER, not execute.
  - test_default_leg_git_state_fetch_failure_warns: a failed `git fetch` now
    emits a [warn] stderr line (visible-but-non-blocking) instead of silently
    falling through to a stale-ref comparison, while still returning the
    stale-ref-derived verdict rather than crashing.

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T3a-g2
Port of: check-plugin-drift.test.sh (coordinator-claude 15f58741, 2026-07-16)
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.plugin_health.drift import (
    _venv_mapping_check,
    check_plugin,
    git_worktree_root_if_self,
)


def _mkrepo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    env = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    (path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "-C", str(path), "add", "seed.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "seed"], check=True, env={**_env_base(), **env})


def _env_base():
    import os

    return dict(os.environ)


def test_a_standalone_repo_root_returns_own_canon(tmp_path: Path):
    repo_a = tmp_path / "standalone"
    _mkrepo(repo_a)
    import os

    got = git_worktree_root_if_self(repo_a)
    want = Path(os.path.realpath(str(repo_a)))
    assert got == want


def test_b_regression_nested_data_only_dir_returns_none(tmp_path: Path):
    parent = tmp_path / "parent"
    _mkrepo(parent)
    nested = parent / "plugins" / "data-only-live"  # no nested .git — escapes to parent pre-fix
    nested.mkdir(parents=True)
    (nested / "data.bin").write_text("payload\n")

    got = git_worktree_root_if_self(nested)
    assert got is None


def test_c_nonexistent_path_returns_none(tmp_path: Path):
    got = git_worktree_root_if_self(tmp_path / "does-not-exist")
    assert got is None


def test_d_plain_non_git_dir_returns_none(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    got = git_worktree_root_if_self(plain)
    assert got is None


def test_e_regression_dirty_parent_nested_data_only_live_path_no_false_positive(tmp_path: Path):
    parent = tmp_path / "parent"
    _mkrepo(parent)
    nested = parent / "plugins" / "data-only-live"
    nested.mkdir(parents=True)
    (nested / "data.bin").write_text("payload\n")
    # Make the PARENT working tree dirty — if Leg 3 escaped upward, `git status`
    # would be non-empty and emit the false "refresh blocked".
    with (parent / "seed.txt").open("a") as f:
        f.write("dirty\n")

    result = check_plugin(
        "testplug",
        str(parent),
        str(nested),
        "origin/main",
        "",
        "",  # prop_mode="" reaches the default leg (Leg 3)
        "",
    )

    joined_lines = "\n".join(result.lines)
    assert "working-tree:" not in joined_lines or "refresh blocked" not in joined_lines
    # Emits the [info] skip diagnostic (live_path IS a git path via the parent).
    joined_stderr = "\n".join(result.stderr_lines)
    assert "Leg 3 skipped — live_path is not its own work-tree root" in joined_stderr


def test_f_standalone_dirty_repo_still_flagged(tmp_path: Path):
    repo_f = tmp_path / "dirty-live"
    _mkrepo(repo_f)
    with (repo_f / "seed.txt").open("a") as f:
        f.write("change\n")

    result = check_plugin(
        "dirtyplug",
        str(repo_f),
        str(repo_f),
        "origin/main",
        "",
        "",
        "",
    )

    joined_lines = "\n".join(result.lines)
    assert "working-tree:" in joined_lines and "refresh blocked" in joined_lines
    assert result.ok is False


# ---------------------------------------------------------------------------
# NEW regression cases (plan-directed behavior deltas — not ports of bash coverage)
# ---------------------------------------------------------------------------


def test_mapping_check_rejects_malicious_finder(tmp_path: Path):
    """eval() -> ast.literal_eval hardening: a finder file whose MAPPING body
    contains a non-literal expression (here, an attempted code-exec payload)
    must resolve to NO_FINDER — never execute — where the old eval() would."""
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    finder = site_packages / "__editable___testpkg_finder.py"
    finder.write_text(
        "MAPPING = {'testpkg': __import__('os').getcwd()}\n"
    )

    result = _venv_mapping_check(site_packages, tmp_path)
    assert result == "NO_FINDER"


def test_default_leg_git_state_fetch_failure_warns(tmp_path: Path, monkeypatch):
    """A failed `git fetch` no longer silently swallows into an undifferentiated
    stale-ref success path — it emits a [warn] stderr line and CONTINUES with
    the stale-ref comparison rather than crashing."""
    from coordinator_core.plugin_health import drift as drift_mod

    live = tmp_path / "live"
    _mkrepo(live)

    class _FailFetch:
        returncode = 1
        stdout = ""
        stderr = "fatal: unable to access (offline)"

    original_run_git = drift_mod._run_git

    def _fake_run_git(args, cwd=None, timeout=60):
        if args and args[0] == "fetch":
            return _FailFetch()
        return original_run_git(args, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(drift_mod, "_run_git", _fake_run_git)

    result = check_plugin(
        "offlineplug",
        "",
        str(live),
        "origin/main",
        "",
        "",
        "",
    )

    joined_stderr = "\n".join(result.stderr_lines)
    assert "git fetch failed (offline?)" in joined_stderr
    # Probe still returns a verdict (does not crash) — stale-ref rev-list against
    # HEAD..origin/main with no remote configured yields no count -> no drift claim
    # from Leg 1, but the probe completes and returns a DriftResult either way.
    assert result is not None

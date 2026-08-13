"""
Tests for coordinator_core.ops.detect_onboarding_offer.

Mirrors the coordinator-claude bash test suite case for case -- the bash suite's case
matrix is the parity oracle for this port.

Port of: test-detect-onboarding-offer.sh (coordinator-claude 432e3285, 2026-07-22)
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.detect_onboarding_offer import detect_onboarding_offer, main

# Declared, not excused: this file spawns real git because the bash-oracle parity
# contract (test-detect-onboarding-offer.sh) it ports depends on real repo state
# (baseline commit presence) that `detect_onboarding_offer` reads via git plumbing --
# no mock stands in for that. Each test builds its own tmp_path repo via
# `_init_git_repo`, so there is no shared state to hoist to module scope. The spawn
# ratchet's `_BASELINE` is shrink-only pre-existing residue and is explicitly not the
# route for this file -- coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _init_git_repo(path):
    subprocess.run(["git", "init", "--quiet"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(path), check=True)
    (path / "README.md").write_text("# baseline\n")
    subprocess.run(["git", "add", "--", "README.md"], cwd=str(path), check=True)
    subprocess.run(
        ["git", "commit", "--quiet", "--no-verify", "-m", "chore: baseline"],
        cwd=str(path),
        check=True,
    )


def _write_project_tracker(path):
    (path / "docs").mkdir(parents=True, exist_ok=True)
    (path / "docs" / "project-tracker.md").write_text("# Project Tracker\n")


def _write_gitignore_distribution(path):
    (path / ".gitignore").write_text("tasks/\narchive/\nnode_modules/\n")


def _write_currency_stamp(path, version):
    (path / "docs").mkdir(parents=True, exist_ok=True)
    (path / "docs" / "coordinator-currency.yaml").write_text(
        f"schema_version: {version}\nstamped_at: 2026-01-01\n"
    )


def _make_fake_plugin_root(base, version):
    proot = base / "fake-plugin"
    (proot / "bin").mkdir(parents=True, exist_ok=True)
    (proot / "lib").mkdir(parents=True, exist_ok=True)
    (proot / "coordinator-schema-version").write_text(f"{version}\n")
    # No bin/probe-onboarding-currency.py -- exercises the fallback branch
    # (direct coordinator_currency_probe call). The fallback branch's own
    # currency_lib presence-gate resolves off CLAUDE_KLABAUTER_ROOT, not plugin_root
    # (coordinator_currency.py migrated to claude-klabauter's own coordinator/lib/ --
    # see detect_onboarding_offer's fallback-branch comment) -- see the
    # _fake_claude_klabauter_root autouse fixture below for that file.
    return proot


@pytest.fixture(autouse=True)
def _fake_claude_klabauter_root(tmp_path, monkeypatch):
    """Autouse: pins CLAUDE_KLABAUTER_ROOT deterministically (env monkeypatch, never
    the real machine-local registry or a real claude-klabauter checkout) to a
    synthetic claude-klabauter root containing coordinator/lib/coordinator_currency.py
    -- the file detect_onboarding_offer's fallback branch presence-gates on.
    Without this every fallback-branch test would see the file "missing"
    and silently fall through to the final "no probe available" return,
    masking the branch this suite exists to exercise."""
    claude_klabauter_root = tmp_path / "fake-claude-klabauter-root"
    (claude_klabauter_root / "coordinator" / "lib").mkdir(parents=True, exist_ok=True)
    (claude_klabauter_root / "coordinator" / "lib" / "coordinator_currency.py").write_text(
        "# stub — not sourced by port\n"
    )
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", str(claude_klabauter_root))
    return claude_klabauter_root


# ---------------------------------------------------------------------------
# Case 1: unonboarded + working repo -> offer line emitted
# ---------------------------------------------------------------------------


def test_case1_unonboarded_working_repo_offers(tmp_path):
    repo = tmp_path / "case1"
    repo.mkdir()
    _init_git_repo(repo)
    plugin_root = _make_fake_plugin_root(tmp_path, 2)

    out = detect_onboarding_offer(str(repo), str(plugin_root))
    assert out
    assert "[onboarding]" in out
    assert "repo-setup" in out.lower()
    assert "Dismiss" in out


# ---------------------------------------------------------------------------
# Case 2: unonboarded + distribution repo -> silent
# ---------------------------------------------------------------------------


def test_case2_unonboarded_distribution_repo_silent(tmp_path):
    repo = tmp_path / "case2"
    repo.mkdir()
    _init_git_repo(repo)
    _write_gitignore_distribution(repo)
    plugin_root = _make_fake_plugin_root(tmp_path, 2)

    out = detect_onboarding_offer(str(repo), str(plugin_root))
    assert out == ""


# ---------------------------------------------------------------------------
# Case 3: stale (drift) repo -> offer line emitted
# ---------------------------------------------------------------------------


def test_case3_stale_drift_offers(tmp_path):
    repo = tmp_path / "case3"
    repo.mkdir()
    _init_git_repo(repo)
    _write_project_tracker(repo)
    _write_currency_stamp(repo, 1)
    plugin_root = _make_fake_plugin_root(tmp_path, 2)

    out = detect_onboarding_offer(str(repo), str(plugin_root))
    assert out
    assert "[onboarding]" in out
    assert "drift(1->2)" in out
    assert "older" in out


# ---------------------------------------------------------------------------
# Case 4 / 4b: dismissed (sentinel present) -> silent, incl. stale-and-dismissed
# ---------------------------------------------------------------------------


def test_case4_dismissed_silent(tmp_path):
    repo = tmp_path / "case4"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / ".git" / "coordinator-onboarding-dismissed").touch()
    plugin_root = _make_fake_plugin_root(tmp_path, 2)

    out = detect_onboarding_offer(str(repo), str(plugin_root))
    assert out == ""


def test_case4b_dismissed_stale_still_silent(tmp_path):
    repo = tmp_path / "case4b"
    repo.mkdir()
    _init_git_repo(repo)
    _write_project_tracker(repo)
    _write_currency_stamp(repo, 1)
    (repo / ".git" / "coordinator-onboarding-dismissed").touch()
    plugin_root = _make_fake_plugin_root(tmp_path, 2)

    out = detect_onboarding_offer(str(repo), str(plugin_root))
    assert out == ""


# ---------------------------------------------------------------------------
# Case 5: onboarded + current -> silent
# ---------------------------------------------------------------------------


def test_case5_onboarded_current_silent(tmp_path):
    repo = tmp_path / "case5"
    repo.mkdir()
    _init_git_repo(repo)
    _write_project_tracker(repo)
    _write_currency_stamp(repo, 2)
    plugin_root = _make_fake_plugin_root(tmp_path, 2)

    out = detect_onboarding_offer(str(repo), str(plugin_root))
    assert out == ""


# ---------------------------------------------------------------------------
# Case 6: non-git directory -> silent
# ---------------------------------------------------------------------------


def test_case6_non_git_dir_silent(tmp_path):
    repo = tmp_path / "case6"
    repo.mkdir()
    plugin_root = _make_fake_plugin_root(tmp_path, 2)

    out = detect_onboarding_offer(str(repo), str(plugin_root))
    assert out == ""


# ---------------------------------------------------------------------------
# Primary path: probe script present (executable) + drift -> offer line
# (exercises the primary branch specifically; every case above hits the
# fallback branch since _make_fake_plugin_root never writes the probe script)
# ---------------------------------------------------------------------------


def test_primary_path_probe_script_present_offers(tmp_path):
    repo = tmp_path / "primary"
    repo.mkdir()
    _init_git_repo(repo)
    _write_project_tracker(repo)
    _write_currency_stamp(repo, 1)

    plugin_root = _make_fake_plugin_root(tmp_path, 2)
    probe_script = plugin_root / "bin" / "probe-onboarding-currency.py"
    probe_script.write_text("#!/bin/sh\n# stub — not executed by port (in-process call)\n")
    probe_script.chmod(0o755)

    out = detect_onboarding_offer(str(repo), str(plugin_root))
    assert out
    assert "drift(1->2)" in out


def test_primary_path_probe_script_present_unstamped(tmp_path):
    repo = tmp_path / "primary-unstamped"
    repo.mkdir()
    _init_git_repo(repo)
    _write_project_tracker(repo)
    # No currency stamp -> unstamped(legacy)

    plugin_root = _make_fake_plugin_root(tmp_path, 2)
    probe_script = plugin_root / "bin" / "probe-onboarding-currency.py"
    probe_script.write_text("#!/bin/sh\n")
    probe_script.chmod(0o755)

    out = detect_onboarding_offer(str(repo), str(plugin_root))
    assert out
    assert "no currency stamp" in out
    # Primary-path message format is version-agnostic (no embedded status
    # string, e.g. no literal "unstamped(legacy)") -- unlike the fallback-path
    # message, which embeds it. Faithful reproduction of the bash oracle's
    # message-text asymmetry. (Substring-safe: tmp_path itself legitimately
    # contains "unstamped" via the test-case directory name.)
    assert "unstamped(" not in out


# ---------------------------------------------------------------------------
# Case 7: fallback path (probe script absent) + source_is_live -> silent
# ---------------------------------------------------------------------------


def test_case7_fallback_source_is_live_silent(tmp_path):
    repo = tmp_path / "case7"
    repo.mkdir()
    _init_git_repo(repo)
    _write_project_tracker(repo)
    # No currency stamp -> would be unstamped(legacy) if not source_is_live.

    plugin_root = repo / "plugins" / "coordinator"
    (plugin_root / "bin").mkdir(parents=True)
    (plugin_root / "lib").mkdir(parents=True)
    (plugin_root / "coordinator-schema-version").write_text("2\n")
    # Deliberately no bin/probe-onboarding-currency.py -- forces the fallback
    # branch, which must honour source_is_live before ever calling the probe.

    out = detect_onboarding_offer(str(repo), str(plugin_root))
    assert out == ""


def test_case7b_fallback_non_source_is_live_offers(tmp_path):
    repo = tmp_path / "case7b"
    repo.mkdir()
    _init_git_repo(repo)
    _write_project_tracker(repo)
    _write_currency_stamp(repo, 1)

    plugin_root = tmp_path / "case7b-plugin"
    (plugin_root / "bin").mkdir(parents=True)
    (plugin_root / "lib").mkdir(parents=True)
    (plugin_root / "coordinator-schema-version").write_text("2\n")
    # plugin root is outside the repo -> not source_is_live; still no probe
    # script -> forces fallback path.

    out = detect_onboarding_offer(str(repo), str(plugin_root))
    assert out
    assert "drift(1->2)" in out


# ---------------------------------------------------------------------------
# main() -- env-var + CLI-flag contract, and the always-exit-0 posture
# ---------------------------------------------------------------------------


def test_main_env_var_contract(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "main-env"
    repo.mkdir()
    _init_git_repo(repo)
    plugin_root = _make_fake_plugin_root(tmp_path, 2)

    monkeypatch.setenv("DETECT_ONBOARDING_REPO_ROOT", str(repo))
    monkeypatch.setenv("DETECT_ONBOARDING_PLUGIN_ROOT", str(plugin_root))
    rc = main([])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert "[onboarding]" in out


def test_main_cli_flags_override_env(tmp_path, monkeypatch, capsys):
    offered_repo = tmp_path / "main-cli-offer"
    offered_repo.mkdir()
    _init_git_repo(offered_repo)

    silent_repo = tmp_path / "main-cli-silent"
    silent_repo.mkdir()
    _init_git_repo(silent_repo)
    _write_gitignore_distribution(silent_repo)

    plugin_root = _make_fake_plugin_root(tmp_path, 2)

    monkeypatch.setenv("DETECT_ONBOARDING_REPO_ROOT", str(silent_repo))
    monkeypatch.setenv("DETECT_ONBOARDING_PLUGIN_ROOT", str(plugin_root))
    rc = main(["--repo", str(offered_repo), "--plugin-root", str(plugin_root)])
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert "[onboarding]" in out


def test_main_never_fails_on_missing_plugin_root(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "main-no-plugin"
    repo.mkdir()
    _init_git_repo(repo)
    _write_project_tracker(repo)
    _write_currency_stamp(repo, 1)

    monkeypatch.delenv("DETECT_ONBOARDING_PLUGIN_ROOT", raising=False)
    rc = main(["--repo", str(repo)], default_plugin_root=None)
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == ""


# ---------------------------------------------------------------------------
# Fallback-branch reachability -- CLAUDE_KLABAUTER_ROOT resolution (2026-07-22 repoint:
# coordinator_currency.py migrated out of plugin_root's lib/ into claude-klabauter's
# own coordinator/lib/; the fallback branch must resolve it there, not
# silently always miss and return "" regardless of actual drift).
# ---------------------------------------------------------------------------


def test_fallback_unresolvable_claude_klabauter_root_degrades_to_silent(tmp_path, monkeypatch):
    """CLAUDE_KLABAUTER_ROOT unresolvable (no env, no settings-home pointer, no
    machine-local entry) must degrade to the "no probe available" silent
    return -- never raise -- matching detect_onboarding_offer's own
    "Never raises" contract."""
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)

    repo = tmp_path / "case-unresolvable-claude-klabauter-root"
    repo.mkdir()
    _init_git_repo(repo)
    _write_project_tracker(repo)
    _write_currency_stamp(repo, 1)
    plugin_root = _make_fake_plugin_root(tmp_path, 2)

    from coordinator_core.ops import detect_onboarding_offer as _mod

    def _raise():
        raise RuntimeError("coordinator_claude_klabauter_root: cannot resolve CLAUDE_KLABAUTER_ROOT")

    monkeypatch.setattr(_mod, "coordinator_claude_klabauter_root", _raise)

    out = detect_onboarding_offer(str(repo), str(plugin_root))
    assert out == ""


def test_fallback_reaches_probe_when_claude_klabauter_root_resolved(tmp_path):
    """With CLAUDE_KLABAUTER_ROOT resolved (via the autouse fixture) and
    coordinator/lib/coordinator_currency.py present under it, the fallback
    branch actually reaches coordinator_currency_probe() and surfaces
    drift -- this is the reachability the repoint restores; sibling to
    test_case3_stale_drift_offers (primary path uses the probe script, this
    exercises the same drift verdict through the fallback branch)."""
    repo = tmp_path / "case-fallback-reachable"
    repo.mkdir()
    _init_git_repo(repo)
    _write_project_tracker(repo)
    _write_currency_stamp(repo, 1)
    plugin_root = _make_fake_plugin_root(tmp_path, 2)

    out = detect_onboarding_offer(str(repo), str(plugin_root))
    assert out
    assert "drift(1->2)" in out

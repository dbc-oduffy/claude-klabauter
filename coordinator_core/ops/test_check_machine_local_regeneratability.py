"""Tests for coordinator_core.ops.check_machine_local_regeneratability.

Golden oracle snapshotted 2026-07-17 against the DoE-side bats-equivalent fixture
corpus (cases a-d) plus additional edge cases for the in-process TOML parsing
this port introduces (no subprocess helper).

Port of: check-machine-local-regeneratability.sh (DoE b5a4192c, 2026-07-20)
Oracle: check-machine-local-regeneratability.test.sh (DoE a2fe06f8, 2026-07-22)
"""

from __future__ import annotations

import os
import sys

import pytest

from coordinator_core.ops.check_machine_local_regeneratability import (
    _key_matches_regen_entry,
    main,
)
from coordinator_core.testing.fake_machine_local import write_fake_machine_local


def _make_claude_dir(tmp_path):
    ml_dir = tmp_path / ".claude" / "machine-local"
    ml_dir.mkdir(parents=True)
    return tmp_path / ".claude", ml_dir


def _run(claude_dir, capsys, argv=None, monkeypatch=None, ml_dir=None):
    if monkeypatch is not None and ml_dir is not None:
        monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(ml_dir))
    rc = main((argv or []) + ["--claude-dir", str(claude_dir)])
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# ---------------------------------------------------------------------------
# (a) Clean state — all session-accumulated entries have tracked baseline
# ---------------------------------------------------------------------------
def test_clean_state_silent_exit_zero(tmp_path, capsys, monkeypatch):
    claude_dir, ml_dir = _make_claude_dir(tmp_path)
    (ml_dir / "registry.toml").write_text(
        """
schema = 1

"coordinator.python"       = ""
"plugin.mirrors"           = ""
"publish.targets"          = []
"repos.example-sim-repo"           = ""
"repos.example_retrieval_repo"        = ""
"repos.example_retrieval_repo_ue_addon" = ""
"repos.example_game_workbench_repo" = ""
"repos.example_repo"         = ""
"repos.example_stats_repo"         = ""
"repos.example_league_data_repo"   = ""
"repos.experiments"        = ""
"repos.example_cockpit_repo"    = ""
"repos.example-os-repo"           = ""

[regeneratability]
"coordinator.python"           = "idempotent-regeneratable"
"plugin.mirrors"                = "idempotent-regeneratable"
"publish.targets"               = "idempotent-regeneratable"
"repos.example-sim-repo"                = "session-accumulated-must-survive-crash"
"repos.example_retrieval_repo"             = "session-accumulated-must-survive-crash"
"repos.example_retrieval_repo_ue_addon"    = "session-accumulated-must-survive-crash"
"repos.example_game_workbench_repo"  = "session-accumulated-must-survive-crash"
"repos.example_repo"              = "session-accumulated-must-survive-crash"
"repos.example_stats_repo"              = "session-accumulated-must-survive-crash"
"repos.example_league_data_repo"        = "session-accumulated-must-survive-crash"
"repos.experiments"             = "session-accumulated-must-survive-crash"
"repos.example_cockpit_repo"         = "session-accumulated-must-survive-crash"
"repos.example-os-repo"                = "session-accumulated-must-survive-crash"
"""
    )
    (ml_dir / "registry.local.toml").write_text(
        'schema = 1\n"repos.example_retrieval_repo" = "/home/user/example-retrieval-repo"\n'
    )

    rc, out, err = _run(claude_dir, capsys, monkeypatch=monkeypatch, ml_dir=ml_dir)

    assert rc == 0
    assert "unclassified" not in err
    assert "install-surface-completeness defect" not in err


# ---------------------------------------------------------------------------
# (b) Gitignored session-accumulated entry with no tracked baseline → finding
# ---------------------------------------------------------------------------
def test_gap_gitignored_only_flags_finding(tmp_path, capsys, monkeypatch):
    claude_dir, ml_dir = _make_claude_dir(tmp_path)
    (ml_dir / "registry.toml").write_text(
        """
schema = 1

"coordinator.python"       = ""
"plugin.mirrors"           = ""
"publish.targets"          = []
"repos.example-sim-repo"           = ""
"repos.example_retrieval_repo_ue_addon" = ""
"repos.example_game_workbench_repo" = ""
"repos.example_repo"         = ""
"repos.example_stats_repo"         = ""
"repos.example_league_data_repo"   = ""
"repos.experiments"        = ""
"repos.example_cockpit_repo"    = ""
"repos.example-os-repo"           = ""

[regeneratability]
"coordinator.python"           = "idempotent-regeneratable"
"plugin.mirrors"                = "idempotent-regeneratable"
"publish.targets"               = "idempotent-regeneratable"
"repos.example-sim-repo"                = "session-accumulated-must-survive-crash"
"repos.example_retrieval_repo"             = "session-accumulated-must-survive-crash"
"repos.example_retrieval_repo_ue_addon"    = "session-accumulated-must-survive-crash"
"repos.example_game_workbench_repo"  = "session-accumulated-must-survive-crash"
"repos.example_repo"              = "session-accumulated-must-survive-crash"
"repos.example_stats_repo"              = "session-accumulated-must-survive-crash"
"repos.example_league_data_repo"        = "session-accumulated-must-survive-crash"
"repos.experiments"             = "session-accumulated-must-survive-crash"
"repos.example_cockpit_repo"         = "session-accumulated-must-survive-crash"
"repos.example-os-repo"                = "session-accumulated-must-survive-crash"
"""
    )
    (ml_dir / "registry.local.toml").write_text(
        'schema = 1\n"repos.example_retrieval_repo" = "/home/user/example-retrieval-repo"\n'
    )

    rc, out, err = _run(claude_dir, capsys, monkeypatch=monkeypatch, ml_dir=ml_dir)

    assert rc == 0
    assert "install-surface-completeness defect" in err
    assert "repos.example_retrieval_repo" in err
    assert "Offer:" in err
    # repos.example-sim-repo IS declared in the tracked registry.toml — must not be flagged
    assert "'repos.example-sim-repo'" not in err


# ---------------------------------------------------------------------------
# (c) Full coverage of coordinator-owned keys → no unclassified warning
# ---------------------------------------------------------------------------
def test_full_coverage_no_unclassified_warning(tmp_path, capsys, monkeypatch):
    claude_dir, ml_dir = _make_claude_dir(tmp_path)
    (ml_dir / "registry.toml").write_text(
        """
schema = 1

[regeneratability]
"coordinator.python"           = "idempotent-regeneratable"
"plugin.mirrors"                = "idempotent-regeneratable"
"publish.targets"               = "idempotent-regeneratable"
"repos.example-sim-repo"                = "idempotent-regeneratable"
"repos.example_retrieval_repo"             = "idempotent-regeneratable"
"repos.example_retrieval_repo_ue_addon"    = "idempotent-regeneratable"
"repos.example_game_workbench_repo"  = "idempotent-regeneratable"
"repos.example_repo"              = "idempotent-regeneratable"
"repos.example_stats_repo"              = "idempotent-regeneratable"
"repos.example_league_data_repo"        = "idempotent-regeneratable"
"repos.experiments"             = "idempotent-regeneratable"
"repos.example_cockpit_repo"         = "idempotent-regeneratable"
"repos.example-os-repo"                = "idempotent-regeneratable"
"repos.claude_klabauter"        = "idempotent-regeneratable"
"""
    )

    rc, out, err = _run(claude_dir, capsys, monkeypatch=monkeypatch, ml_dir=ml_dir)

    assert rc == 0
    assert "absent from [regeneratability]" not in err


# ---------------------------------------------------------------------------
# (d) Coordinator-owned key absent from [regeneratability] table → warning
# ---------------------------------------------------------------------------
def test_missing_key_flags_unclassified_warning(tmp_path, capsys, monkeypatch):
    claude_dir, ml_dir = _make_claude_dir(tmp_path)
    (ml_dir / "registry.toml").write_text(
        """
schema = 1

[regeneratability]
# coordinator.python intentionally omitted
"publish.targets"               = "idempotent-regeneratable"
"repos.example-sim-repo"                = "idempotent-regeneratable"
"repos.example_retrieval_repo"             = "idempotent-regeneratable"
"repos.example_retrieval_repo_ue_addon"    = "idempotent-regeneratable"
"repos.example_game_workbench_repo"  = "idempotent-regeneratable"
"repos.example_repo"              = "idempotent-regeneratable"
"repos.example_stats_repo"              = "idempotent-regeneratable"
"repos.example_league_data_repo"        = "idempotent-regeneratable"
"repos.experiments"             = "idempotent-regeneratable"
"repos.example_cockpit_repo"         = "idempotent-regeneratable"
"repos.example-os-repo"                = "idempotent-regeneratable"
"""
    )

    rc, out, err = _run(claude_dir, capsys, monkeypatch=monkeypatch, ml_dir=ml_dir)

    assert rc == 0
    assert "absent from [regeneratability]" in err
    assert "coordinator.python" in err
    assert "plugin.mirrors" in err


# ---------------------------------------------------------------------------
# plugin.mirrors namespace-prefix match (Check 1) — per-plugin sub-keys satisfy it
# ---------------------------------------------------------------------------
def test_plugin_mirrors_namespace_prefix_satisfies_check1(tmp_path, capsys, monkeypatch):
    claude_dir, ml_dir = _make_claude_dir(tmp_path)
    (ml_dir / "registry.toml").write_text(
        """
schema = 1

[regeneratability]
"coordinator.python"           = "idempotent-regeneratable"
"plugin.mirrors.coordinator-claude" = "idempotent-regeneratable"
"publish.targets"               = "idempotent-regeneratable"
"repos.example-sim-repo"                = "idempotent-regeneratable"
"repos.example_retrieval_repo"             = "idempotent-regeneratable"
"repos.example_retrieval_repo_ue_addon"    = "idempotent-regeneratable"
"repos.example_game_workbench_repo"  = "idempotent-regeneratable"
"repos.example_repo"              = "idempotent-regeneratable"
"repos.example_stats_repo"              = "idempotent-regeneratable"
"repos.example_league_data_repo"        = "idempotent-regeneratable"
"repos.experiments"             = "idempotent-regeneratable"
"repos.example_cockpit_repo"         = "idempotent-regeneratable"
"repos.example-os-repo"                = "idempotent-regeneratable"
"""
    )

    rc, out, err = _run(claude_dir, capsys, monkeypatch=monkeypatch, ml_dir=ml_dir)

    assert rc == 0
    assert "'plugin.mirrors'" not in err


# ---------------------------------------------------------------------------
# repos.* ladder-resolve skip — machine-local get rc=0 suppresses the finding
# ---------------------------------------------------------------------------
def test_repos_key_ladder_resolved_suppresses_finding(tmp_path, capsys, monkeypatch):
    claude_dir, ml_dir = _make_claude_dir(tmp_path)
    (ml_dir / "registry.toml").write_text(
        """
schema = 1

[regeneratability]
"repos.example-sim-repo" = "session-accumulated-must-survive-crash"
"""
    )
    (ml_dir / "registry.local.toml").write_text(
        'schema = 1\n"repos.example-sim-repo" = "/home/user/example-sim-repo"\n'
    )

    bin_dir = claude_dir / "bin"
    write_fake_machine_local(bin_dir, "import sys\nsys.exit(0)\n")

    rc, out, err = _run(claude_dir, capsys, monkeypatch=monkeypatch, ml_dir=ml_dir)

    assert rc == 0
    assert "repos.example-sim-repo" not in err


# ---------------------------------------------------------------------------
# repos.* ladder-probe subprocess failure (missing binary) degrades to fail-safe:
# the key still gets evaluated by the normal tracked/local check, never crashes.
# ---------------------------------------------------------------------------
def test_repos_key_missing_ladder_binary_falls_through_safely(tmp_path, capsys, monkeypatch):
    claude_dir, ml_dir = _make_claude_dir(tmp_path)
    (ml_dir / "registry.toml").write_text(
        """
schema = 1

[regeneratability]
"repos.example-sim-repo" = "session-accumulated-must-survive-crash"
"""
    )
    (ml_dir / "registry.local.toml").write_text(
        'schema = 1\n"repos.example-sim-repo" = "/home/user/example-sim-repo"\n'
    )
    # No claude_dir/bin/machine-local present at all — _ladder_resolves must not raise.

    rc, out, err = _run(claude_dir, capsys, monkeypatch=monkeypatch, ml_dir=ml_dir)

    assert rc == 0
    assert "install-surface-completeness defect" in err
    assert "repos.example-sim-repo" in err


# ---------------------------------------------------------------------------
# Missing HOME/CLAUDE_HOME/USERPROFILE — offer-shaped, exit 0, one stderr line
# ---------------------------------------------------------------------------
def test_unresolvable_home_exits_zero(capsys, monkeypatch):
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)

    rc = main([])
    captured = capsys.readouterr()

    assert rc == 0
    assert "Cannot resolve HOME" in captured.err
    assert "CLAUDE_DIR is empty" in captured.err


# ---------------------------------------------------------------------------
# Missing machine-local/ directory entirely — no crash, exit 0
# ---------------------------------------------------------------------------
def test_missing_machine_local_dir_no_crash(tmp_path, capsys, monkeypatch):
    claude_dir = tmp_path / ".claude"
    ml_dir = tmp_path / ".claude" / "machine-local"
    # machine-local/ deliberately not created.
    rc, out, err = _run(claude_dir, capsys, monkeypatch=monkeypatch, ml_dir=ml_dir)
    assert rc == 0


# ---------------------------------------------------------------------------
# Malformed TOML in a tracked file — parse error surfaces on stderr, exit 0
# ---------------------------------------------------------------------------
def test_malformed_toml_emits_parse_error_and_exits_zero(tmp_path, capsys, monkeypatch):
    claude_dir, ml_dir = _make_claude_dir(tmp_path)
    (ml_dir / "registry.toml").write_text("this is not [valid toml\n")

    rc, out, err = _run(claude_dir, capsys, monkeypatch=monkeypatch, ml_dir=ml_dir)

    assert rc == 0
    assert "Failed to parse" in err


# ---------------------------------------------------------------------------
# --help prints usage and exits 0
# ---------------------------------------------------------------------------
def test_registry_resolves_via_settings_home_ladder(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)
    settings_home = tmp_path / "settings-home"
    ml_dir = settings_home / "machine-local"
    ml_dir.mkdir(parents=True)
    (ml_dir / "registry.toml").write_text(
        """
schema = 1

[regeneratability]
"coordinator.python"           = "idempotent-regeneratable"
"plugin.mirrors"                = "idempotent-regeneratable"
"publish.targets"               = "idempotent-regeneratable"
"repos.example-sim-repo"                = "idempotent-regeneratable"
"repos.example_retrieval_repo"             = "idempotent-regeneratable"
"repos.example_retrieval_repo_ue_addon"    = "idempotent-regeneratable"
"repos.example_game_workbench_repo"  = "idempotent-regeneratable"
"repos.example_repo"              = "idempotent-regeneratable"
"repos.example_stats_repo"              = "idempotent-regeneratable"
"repos.example_league_data_repo"        = "idempotent-regeneratable"
"repos.experiments"             = "idempotent-regeneratable"
"repos.example_cockpit_repo"         = "idempotent-regeneratable"
"repos.example-os-repo"                = "idempotent-regeneratable"
"repos.claude_klabauter"        = "idempotent-regeneratable"
"""
    )
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

    # claude_dir has no machine-local under it at all — proves the registry is
    # NOT being read from the legacy <claude_dir>/machine-local location.
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)

    rc, out, err = _run(claude_dir, capsys)

    assert rc == 0
    assert "absent from [regeneratability]" not in err


def test_help_flag_prints_usage(capsys):
    rc = main(["--help"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "usage:" in captured.out


# ---------------------------------------------------------------------------
# AC8 (docs/plans/2026-08-07-two-tier-engine-root-adopt-dr132.md, chunk C6b):
# repos.claude_klabauter joins COORDINATOR_OWNED_KEYS + the family-prefix arm.
# ---------------------------------------------------------------------------
def test_claude_klabauter_classified_no_warning(tmp_path, capsys, monkeypatch):
    claude_dir, ml_dir = _make_claude_dir(tmp_path)
    (ml_dir / "registry.toml").write_text(
        """
schema = 1

[regeneratability]
"coordinator.python"           = "idempotent-regeneratable"
"plugin.mirrors"                = "idempotent-regeneratable"
"publish.targets"               = "idempotent-regeneratable"
"repos.example-sim-repo"                = "idempotent-regeneratable"
"repos.example_retrieval_repo"             = "idempotent-regeneratable"
"repos.example_retrieval_repo_ue_addon"    = "idempotent-regeneratable"
"repos.example_game_workbench_repo"  = "idempotent-regeneratable"
"repos.example_repo"              = "idempotent-regeneratable"
"repos.example_stats_repo"              = "idempotent-regeneratable"
"repos.example_league_data_repo"        = "idempotent-regeneratable"
"repos.experiments"             = "idempotent-regeneratable"
"repos.example_cockpit_repo"         = "idempotent-regeneratable"
"repos.example-os-repo"                = "idempotent-regeneratable"
"repos.claude_klabauter"        = "idempotent-regeneratable"
"""
    )

    rc, out, err = _run(claude_dir, capsys, monkeypatch=monkeypatch, ml_dir=ml_dir)

    assert rc == 0
    assert "absent from [regeneratability]" not in err


def test_claude_klabauter_unclassified_warns(tmp_path, capsys, monkeypatch):
    claude_dir, ml_dir = _make_claude_dir(tmp_path)
    (ml_dir / "registry.toml").write_text(
        """
schema = 1

[regeneratability]
"coordinator.python"           = "idempotent-regeneratable"
"plugin.mirrors"                = "idempotent-regeneratable"
"publish.targets"               = "idempotent-regeneratable"
"repos.example-sim-repo"                = "idempotent-regeneratable"
"repos.example_retrieval_repo"             = "idempotent-regeneratable"
"repos.example_retrieval_repo_ue_addon"    = "idempotent-regeneratable"
"repos.example_game_workbench_repo"  = "idempotent-regeneratable"
"repos.example_repo"              = "idempotent-regeneratable"
"repos.example_stats_repo"              = "idempotent-regeneratable"
"repos.example_league_data_repo"        = "idempotent-regeneratable"
"repos.experiments"             = "idempotent-regeneratable"
"repos.example_cockpit_repo"         = "idempotent-regeneratable"
"repos.example-os-repo"                = "idempotent-regeneratable"
# repos.claude_klabauter intentionally omitted
"""
    )

    rc, out, err = _run(claude_dir, capsys, monkeypatch=monkeypatch, ml_dir=ml_dir)

    assert rc == 0
    assert "absent from [regeneratability]" in err
    assert "repos.claude_klabauter" in err


def test_family_prefix_arm_resolves_dotted_key_against_bare_family_entry():
    assert _key_matches_regen_entry("engine.working_repos.doe_claude", "engine.working_repos")


def test_family_prefix_arm_rejects_leading_substring_without_dotted_boundary():
    # "engine.working_repositories" shares the leading substring "engine.working_repos"
    # but is not "engine.working_repos" plus a dotted segment — must NOT match. Guards
    # against a sloppy str.startswith() implementation of the family-prefix arm.
    assert not _key_matches_regen_entry("engine.working_repositories", "engine.working_repos")
    assert not _key_matches_regen_entry("engine.working_repos", "engine.working_repositories")


def test_plugin_mirrors_family_prefix_arm_still_works_via_helper():
    assert _key_matches_regen_entry("plugin.mirrors", "plugin.mirrors.coordinator-claude")
    assert not _key_matches_regen_entry("plugin.mirrors", "plugin.mirrorsx")

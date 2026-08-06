"""Characterization tests for coordinator_core.ops.detect_existing_claude_home.

Mirrors Port of: detect-existing-claude-home.test.sh (example-doctrine-repo 6fb5fb37,
2026-07-22) (16 scenarios, T1-T7) plus a handful of Python-port-specific
edge cases (HOME fallback, filesystem-error swallowing, glob-pattern
fidelity of `_is_cc_managed_entry`).

Spec backlink: docs/plans/2026-07-16-bash-to-naked-python-engine-migration.md
Port of: detect-existing-claude-home.sh (example-doctrine-repo 6fb5fb37, 2026-07-22)
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.detect_existing_claude_home import (
    _has_installed_plugin,
    _installed_plugins_json_nonempty,
    _is_cc_managed_entry,
    _plugins_dir_nonempty,
    classify,
    emit,
    main,
    resolve_target,
)


def _assert(tmp_path: Path, exp_state: str, exp_track: str) -> None:
    state, track, _reason = classify(str(tmp_path))
    assert (state, track) == (exp_state, exp_track)


# ---------------------------------------------------------------------------
# T1: empty fixture → pristine
# ---------------------------------------------------------------------------
def test_t1_empty_fixture_is_pristine(tmp_path):
    _assert(tmp_path, "pristine", "A")


# ---------------------------------------------------------------------------
# T2: real plugin subdir → configured
# ---------------------------------------------------------------------------
def test_t2_real_plugin_subdir_is_configured(tmp_path):
    plugin_dir = tmp_path / "plugins" / "my-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "PLUGIN.md").write_text("plugin content\n")
    _assert(tmp_path, "configured", "B")


# ---------------------------------------------------------------------------
# T2b: non-scaffolding file directly under plugins/ → configured
# ---------------------------------------------------------------------------
def test_t2b_plugin_file_is_configured(tmp_path):
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "some-plugin-file.md").write_text("x\n")
    _assert(tmp_path, "configured", "B")


# ---------------------------------------------------------------------------
# T2c: empty plugins/ subdir → used-vanilla (scaffolding, not real plugin)
# ---------------------------------------------------------------------------
def test_t2c_empty_plugins_subdir_is_used_vanilla(tmp_path):
    (tmp_path / "plugins" / "empty-plugin").mkdir(parents=True)
    _assert(tmp_path, "used-vanilla", "A")


# ---------------------------------------------------------------------------
# T2d: fresh Claude Code scaffolding only → used-vanilla (the old false-positive)
# ---------------------------------------------------------------------------
def test_t2d_fresh_cc_scaffolding_is_used_vanilla(tmp_path):
    (tmp_path / "plugins" / "cache" / "some-marketplace").mkdir(parents=True)
    (tmp_path / "plugins" / "marketplaces").mkdir()
    (tmp_path / "plugins" / "installed_plugins.json").write_text(
        '{"version": 2, "plugins": {}}\n'
    )
    (tmp_path / "plugins" / "known_marketplaces.json").write_text("{}\n")
    (tmp_path / "plugins" / "blocklist.json").write_text("{}\n")
    (tmp_path / "plugins" / "plugin-catalog-cache.json").write_text("{}\n")
    (tmp_path / "plugins" / ".last_inuse_sweep").write_text("sweep\n")
    (tmp_path / "plugins" / "cache" / "some-marketplace" / "file").write_text("x\n")
    _assert(tmp_path, "used-vanilla", "A")


# ---------------------------------------------------------------------------
# T2e: installed_plugins.json non-empty → configured
# ---------------------------------------------------------------------------
def test_t2e_installed_plugins_json_nonempty_is_configured(tmp_path):
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "installed_plugins.json").write_text(
        '{"version": 2, "plugins": {"skill-creator@official": [{"scope":"user"}]}}\n'
    )
    _assert(tmp_path, "configured", "B")


# ---------------------------------------------------------------------------
# T2f: isolated pretty-printed empty-map installed_plugins.json → used-vanilla
# ---------------------------------------------------------------------------
def test_t2f_isolated_pretty_empty_map_is_used_vanilla(tmp_path):
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "installed_plugins.json").write_text(
        '{\n  "version": 2,\n  "plugins": {}\n}\n'
    )
    _assert(tmp_path, "used-vanilla", "A")


# ---------------------------------------------------------------------------
# T2g: pretty-printed non-empty installed_plugins.json → configured
# ---------------------------------------------------------------------------
def test_t2g_pretty_nonempty_map_is_configured(tmp_path):
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "installed_plugins.json").write_text(
        '{\n  "version": 2,\n  "plugins": {\n    "skill-creator@official": [\n'
        '      { "scope": "user" }\n    ]\n  }\n}\n'
    )
    _assert(tmp_path, "configured", "B")


# ---------------------------------------------------------------------------
# T3 / T3b: CLAUDE.md (substantial / stub) → used-vanilla, NOT configured
# ---------------------------------------------------------------------------
def test_t3_substantial_claude_md_is_used_vanilla(tmp_path):
    lines = "# My Claude Config\n\n" + "".join(f"Non-blank line {i}\n" for i in range(1, 12))
    (tmp_path / "CLAUDE.md").write_text(lines)
    _assert(tmp_path, "used-vanilla", "A")


def test_t3b_stub_claude_md_is_used_vanilla(tmp_path):
    lines = "# Stub\n\n" + "".join(f"Line {i}\n" for i in range(1, 6))
    (tmp_path / "CLAUDE.md").write_text(lines)
    _assert(tmp_path, "used-vanilla", "A")


# ---------------------------------------------------------------------------
# T4 / T4b: git-tracked target → configured; git-tracked ANCESTOR → pristine
# ---------------------------------------------------------------------------
def test_t4_git_tracked_target_is_configured(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, timeout=30)
    _assert(tmp_path, "configured", "B")


def test_t4b_subdir_of_ancestor_repo_is_pristine(tmp_path):
    parent = tmp_path / "git-parent"
    parent.mkdir()
    subprocess.run(["git", "init", "--quiet", str(parent)], check=True, timeout=30)
    target = parent / "dot-claude"
    target.mkdir()
    _assert(target, "pristine", "A")


# ---------------------------------------------------------------------------
# T5: session/runtime artifacts → used-vanilla
# ---------------------------------------------------------------------------
def test_t5_session_artifacts_are_used_vanilla(tmp_path):
    (tmp_path / "projects" / "some-project").mkdir(parents=True)
    (tmp_path / "history.jsonl").write_text("x\n")
    _assert(tmp_path, "used-vanilla", "A")


# ---------------------------------------------------------------------------
# T6 / T6b: coordinator infra → configured
# ---------------------------------------------------------------------------
def test_t6_state_dir_is_configured(tmp_path):
    (tmp_path / "state").mkdir()
    _assert(tmp_path, "configured", "B")


def test_t6b_coordinator_local_md_is_configured(tmp_path):
    (tmp_path / "coordinator.local.md").write_text("project_type: x\n")
    _assert(tmp_path, "configured", "B")


# ---------------------------------------------------------------------------
# T7: precedence — configured signal beats used-vanilla signals
# ---------------------------------------------------------------------------
def test_t7_configured_beats_used_vanilla(tmp_path):
    (tmp_path / "projects").mkdir()
    (tmp_path / "CLAUDE.md").write_text("# config\n")
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, timeout=30)
    _assert(tmp_path, "configured", "B")


# ---------------------------------------------------------------------------
# Python-port-specific edge cases
# ---------------------------------------------------------------------------


def test_resolve_target_precedence_arg_over_env_over_home(tmp_path, monkeypatch):
    # arg wins over everything
    assert resolve_target("/explicit/path", env={"CLAUDE_CONFIG_DIR": "/env/path", "HOME": "/home"}) == "/explicit/path"
    # env wins over HOME
    assert resolve_target(None, env={"CLAUDE_CONFIG_DIR": "/env/path", "HOME": "/home"}) == "/env/path"
    # HOME is the final fallback
    assert resolve_target(None, env={"HOME": "/home"}) == os.path.join("/home", ".claude")


def test_resolve_target_falls_back_to_expanduser_when_home_unset():
    # Windows-shape env: no HOME, no CLAUDE_CONFIG_DIR — must not crash or
    # resolve to a bogus path; falls back to os.path.expanduser("~").
    result = resolve_target(None, env={})
    assert result == os.path.join(os.path.expanduser("~"), ".claude")


def test_is_cc_managed_entry_dotfiles():
    assert _is_cc_managed_entry(".refresh-log") is True
    assert _is_cc_managed_entry(".last_inuse_sweep") is True


def test_is_cc_managed_entry_bak_patterns():
    assert _is_cc_managed_entry("installed_plugins.json.bak") is True
    assert _is_cc_managed_entry("installed_plugins.json.bak-20260101") is True
    assert _is_cc_managed_entry("not-a-backup.json") is False


def test_is_cc_managed_entry_literal_files_and_dirs():
    for name in ("config.json", "installed_plugins.json", "known_marketplaces.json",
                 "blocklist.json", "plugin-catalog-cache.json"):
        assert _is_cc_managed_entry(name) is True
    for name in ("repos", "marketplaces", "cache", "data"):
        assert _is_cc_managed_entry(name) is True
    assert _is_cc_managed_entry("my-real-plugin") is False


def test_installed_plugins_json_nonempty_missing_file(tmp_path):
    assert _installed_plugins_json_nonempty(str(tmp_path)) is False


def test_installed_plugins_json_nonempty_malformed_no_plugins_key(tmp_path):
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "installed_plugins.json").write_text('{"version": 2}\n')
    assert _installed_plugins_json_nonempty(str(tmp_path)) is False


def test_has_installed_plugin_no_plugins_dir(tmp_path):
    assert _has_installed_plugin(str(tmp_path)) is False


def test_plugins_dir_nonempty_no_dir(tmp_path):
    assert _plugins_dir_nonempty(str(tmp_path)) is False


def test_emit_format():
    assert emit("pristine", "A", "reason text") == "state=pristine track=A reason: reason text"


def test_main_prints_classification_line_and_returns_0(tmp_path, capsys):
    rc = main([str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == f"state=pristine track=A reason: {tmp_path} has no Claude Code artifacts — never used"


def test_main_no_args_uses_env_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    rc = main([])
    assert rc == 0


@pytest.mark.skipif(os.name == "nt", reason="posix permission bits don't apply on Windows")
def test_installed_plugins_json_nonempty_swallows_permission_error(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    f = plugins_dir / "installed_plugins.json"
    f.write_text('{"plugins": {"x": []}}\n')
    f.chmod(0o000)
    try:
        # Root (and some CI containers) bypass permission bits entirely.
        if os.access(str(f), os.R_OK):
            pytest.skip("running as a user that bypasses file permission bits")
        assert _installed_plugins_json_nonempty(str(tmp_path)) is False
    finally:
        f.chmod(0o644)

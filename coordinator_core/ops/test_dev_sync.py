"""Characterization + parity tests for coordinator_core.ops.dev_sync.

Port source: coordinator/dist/publish-repo-setup/dev-sync.sh (coordinator-claude, ~115 lines).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coordinator_core.ops.dev_sync import main


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    plugin_dir = repo / "plugins" / "coordinator" / ".claude-plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.json").write_text(json.dumps({"name": "coordinator", "version": "1.2.3"}))
    skills = repo / "plugins" / "coordinator" / "skills"
    skills.mkdir(parents=True)
    (skills / "foo.md").write_text("test\n")

    no_version = repo / "plugins" / "other-noversion"
    no_version.mkdir(parents=True)
    (no_version / "readme.md").write_text("x\n")

    return repo


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def test_sync_all_creates_cache_and_skips_no_version(tmp_path, fake_home, monkeypatch, capsys):
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("DEV_SYNC_REPO_ROOT", str(repo))

    rc = main([])

    assert rc == 0
    out = capsys.readouterr().out
    assert "NEW:  coordinator (1.2.3)" in out
    assert "SYNC: coordinator (1.2.3) — 2 files" in out
    assert "SKIP: other-noversion (no .claude-plugin/plugin.json)" in out

    cache_target = fake_home / ".claude" / "plugins" / "cache" / "coordinator-claude" / "coordinator" / "1.2.3"
    assert (cache_target / "skills" / "foo.md").read_text() == "test\n"
    assert (cache_target / ".claude-plugin" / "plugin.json").is_file()


def test_sync_specific_target_missing_source_dir_skips(tmp_path, fake_home, monkeypatch, capsys):
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("DEV_SYNC_REPO_ROOT", str(repo))

    rc = main(["nonexistent-plugin"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "SKIP: nonexistent-plugin (source dir not found)" in out


def test_orphaned_at_marker_preserved_across_resync(tmp_path, fake_home, monkeypatch, capsys):
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("DEV_SYNC_REPO_ROOT", str(repo))

    assert main(["coordinator"]) == 0
    cache_target = fake_home / ".claude" / "plugins" / "cache" / "coordinator-claude" / "coordinator" / "1.2.3"
    (cache_target / ".orphaned_at").write_text("2026-01-01T00:00:00Z")

    (repo / "plugins" / "coordinator" / "skills" / "foo.md").write_text("test\nnew-content\n")

    rc = main(["coordinator"])
    assert rc == 0

    assert (cache_target / ".orphaned_at").read_text() == "2026-01-01T00:00:00Z"
    assert (cache_target / "skills" / "foo.md").read_text() == "test\nnew-content\n"


def test_no_plugins_dir_at_all_no_op(tmp_path, fake_home, monkeypatch, capsys):
    repo = tmp_path / "repo2"
    repo.mkdir()
    monkeypatch.setenv("DEV_SYNC_REPO_ROOT", str(repo))

    rc = main([])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Done." in out


def test_cache_target_safety_guard_rejects_outside_expected_prefix(tmp_path, fake_home, monkeypatch):
    """Mirrors the bash oracle's issue-#13 safety guard: refuse to wipe a
    cache_target that isn't under $HOME/.claude/plugins/cache."""
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("DEV_SYNC_REPO_ROOT", str(repo))

    # Point Path.home() somewhere that does NOT match the marketplace/cache
    # constant baked into the module (MARKETPLACE/cache_dir composition is
    # always under Path.home(), so this path is exercised via an alternate
    # home whose cache_dir the module itself computes — verifying the guard
    # requires forcing a mismatch, done here by monkeypatching Path.home
    # AFTER the module resolved cache_dir once already in a prior call is
    # out of scope; instead we assert the guard fires when cache_dir logic
    # is bypassed via directly calling _sync_plugin with a foreign cache_dir.
    from coordinator_core.ops.dev_sync import _sync_plugin

    foreign_cache_dir = tmp_path / "not-under-home"
    foreign_cache_dir.mkdir()

    with pytest.raises(SystemExit) as exc_info:
        _sync_plugin("coordinator", repo / "plugins", foreign_cache_dir)

    assert exc_info.value.code == 1

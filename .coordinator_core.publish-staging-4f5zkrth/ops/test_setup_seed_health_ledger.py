"""Tests for coordinator_core.ops.setup_seed_health_ledger.

Port of: setup-seed-health-ledger.sh (DoE 6fb5fb37, 2026-07-22).
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.ops.setup_seed_health_ledger import main, seed_health_ledger


def test_fresh_repo_seeds_ledger(tmp_path):
    repo_root = str(tmp_path)
    rc = seed_health_ledger(repo_root)
    assert rc == 0

    ledger_path = tmp_path / "state" / "health-ledger.md"
    assert ledger_path.is_file()
    content = ledger_path.read_text(encoding="utf-8")
    assert content.startswith("# Health Ledger\n")
    assert "**Last full audit:** (none — run /architecture-survey)" in content
    assert "**Last targeted audit:** (none)" in content
    assert "**Next rotation target:** (none — add systems as they are touched)" in content
    assert "| System | Grade | Last Audited | Notes |" in content
    assert "|--------|-------|-------------|-------|" in content
    # No pre-populated system rows — table header is the last line.
    assert content.rstrip("\n").endswith("|--------|-------|-------------|-------|")


def test_idempotent_when_ledger_already_exists(tmp_path, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    ledger_path = state_dir / "health-ledger.md"
    ledger_path.write_text("PREEXISTING\n", encoding="utf-8")

    rc = seed_health_ledger(str(tmp_path))
    assert rc == 0
    assert ledger_path.read_text(encoding="utf-8") == "PREEXISTING\n"

    out = capsys.readouterr().out
    assert "already exists" in out
    assert "skipping (idempotent)" in out


def test_creates_state_dir_if_missing(tmp_path):
    assert not (tmp_path / "state").exists()
    rc = seed_health_ledger(str(tmp_path))
    assert rc == 0
    assert (tmp_path / "state").is_dir()


def test_nonexistent_repo_root_fails_loud(capsys):
    rc = seed_health_ledger("/does/not/exist/xyz-nonexistent-path-oduffy")
    assert rc == 1
    err = capsys.readouterr().err
    assert "REPO_ROOT does not exist or is not a directory" in err
    assert "Remediation" in err


def test_main_defaults_repo_root_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main([])
    assert rc == 0
    assert (tmp_path / "state" / "health-ledger.md").is_file()


def test_main_uses_explicit_argv_repo_root(tmp_path):
    rc = main([str(tmp_path)])
    assert rc == 0
    assert (tmp_path / "state" / "health-ledger.md").is_file()


def test_write_failure_reports_error_and_nonzero(tmp_path, monkeypatch):
    repo_root = str(tmp_path)

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", _boom)
    rc = seed_health_ledger(repo_root)
    assert rc == 1


def test_resolve_state_root_meta_repo_routes_to_makima(tmp_path, monkeypatch):
    from coordinator_core.ops import setup_seed_health_ledger as mod

    fake_home = tmp_path / "dotclaude"
    fake_home.mkdir()
    fake_makima = tmp_path / "project-makima"
    fake_makima.mkdir()

    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(fake_makima))

    resolved = mod._resolve_state_root(str(fake_home))
    assert os.path.realpath(resolved) == os.path.realpath(str(fake_makima / "state"))


def test_resolve_state_root_non_meta_repo_stays_local(tmp_path, monkeypatch):
    from coordinator_core.ops import setup_seed_health_ledger as mod

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "dotclaude"))
    monkeypatch.delenv("MAKIMA_ROOT", raising=False)

    other_repo = tmp_path / "some-project"
    other_repo.mkdir()
    resolved = mod._resolve_state_root(str(other_repo))
    assert os.path.realpath(resolved) == os.path.realpath(str(other_repo / "state"))


def test_resolve_state_root_meta_repo_falls_back_when_makima_unresolvable(tmp_path, monkeypatch):
    from coordinator_core.ops import setup_seed_health_ledger as mod

    fake_home = tmp_path / "dotclaude"
    fake_home.mkdir()

    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    monkeypatch.delenv("MAKIMA_ROOT", raising=False)
    monkeypatch.setattr(mod, "_machine_local_get", lambda key: None)

    resolved = mod._resolve_state_root(str(fake_home))
    assert os.path.realpath(resolved) == os.path.realpath(str(fake_home / "state"))

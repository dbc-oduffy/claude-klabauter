
from __future__ import annotations

import time
from pathlib import Path

import pytest

from coordinator_core.hooks import foreign_path_filter as fpf
from coordinator_core.hooks import project_orientation as po


def test_module_registers_the_op():
    from coordinator_core.ipc import _REGISTRY

    assert "hooks.project_orientation" in _REGISTRY


def test_import_is_fast():
    import importlib
    import sys

    for name in list(sys.modules):
        if name == "coordinator_core.hooks.project_orientation" or name.startswith(
            "coordinator_core.hooks.project_orientation."
        ):
            del sys.modules[name]

    t0 = time.perf_counter()
    importlib.import_module("coordinator_core.hooks.project_orientation")
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 500, f"cold import took {elapsed_ms}ms"


def test_fire_returns_session_start_context_envelope():
    resp = po._handler({"payload": {"cwd": str(Path(__file__).resolve().parents[3]), "env": {}}})
    assert resp["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert isinstance(resp["hookSpecificOutput"]["additionalContext"], str)


def test_fire_is_fast_against_a_real_repo():
    t0 = time.perf_counter()
    resp = po._handler(
        {"payload": {"cwd": str(Path(__file__).resolve().parents[3]), "env": {}}}
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 500, f"fire took {elapsed_ms}ms"
    assert "hookSpecificOutput" in resp


def test_fire_never_raises_on_malformed_payload():
    resp = po._handler({"payload": {"cwd": 12345, "env": "not-a-mapping"}})
    assert resp["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_fire_with_no_payload_key_at_all():
    resp = po._handler({})
    assert resp["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_lightweight_branch_fires_when_no_cache(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "state").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    resp = po._handler({"payload": {"cwd": str(repo), "env": {"CLAUDE_PROJECT_DIR": str(repo)}}})
    text = resp["hookSpecificOutput"]["additionalContext"]
    assert "Orientation (lightweight" in text


def test_cache_present_banner_echoes_cache_text(tmp_path):
    repo = tmp_path / "repo"
    state = repo / "state"
    state.mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    cache = state / "orientation_cache.md"
    cache.write_text("---\ngenerated_at: 2020-01-01T00:00:00Z\n---\nHELLO\n", encoding="utf-8")

    resp = po._handler({"payload": {"cwd": str(repo), "env": {"CLAUDE_PROJECT_DIR": str(repo)}}})
    text = resp["hookSpecificOutput"]["additionalContext"]
    assert "Orientation (RAM cache)" in text
    assert "HELLO" in text


def test_env_off_switch_suppresses_repomap_banner(tmp_path):
    repo = tmp_path / "repo"
    claude_dir = repo / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "repomap.md").write_text("x", encoding="utf-8")
    import os

    old_mtime = time.time() - 1000 * 3600
    os.utime(claude_dir / "repomap.md", (old_mtime, old_mtime))

    out: list = []
    po._repomap_staleness_banner(out, {"COORDINATOR_REPOMAP_STATUS_OFF": "1"}, str(repo))
    assert out == []

    out2: list = []
    po._repomap_staleness_banner(out2, {}, str(repo))
    assert out2 and "Repo map" in out2[-1]


def test_foreign_path_filter_registers_no_op():
    assert not hasattr(fpf, "register_op")


def test_session_repo_is_plane_fails_open_on_unresolvable_registry(monkeypatch):
    monkeypatch.setattr(fpf, "registry_get", lambda key: None)
    assert fpf.session_repo_is_plane("/nonexistent/path") is False


def test_session_repo_is_plane_matches_a_registered_plane_root(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fpf,
        "registry_get",
        lambda key: str(tmp_path) if key == "repos.claude_klabauter" else None,
    )
    assert fpf.session_repo_is_plane(str(tmp_path)) is True
    assert fpf.session_repo_is_plane(str(tmp_path / "elsewhere")) is False


def test_session_repo_is_plane_never_raises_on_a_bad_registry_value(monkeypatch):
    def _boom(key):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(fpf, "registry_get", _boom)
    assert fpf.session_repo_is_plane("/tmp") is False

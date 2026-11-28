"""index_step: registered root skips, absent root launches one narrowed spawn, no rag fails named."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.install import rag_fleet_registration as reg
from coordinator_core.ops.repo_induct import _index


@pytest.fixture
def env(tmp_path, monkeypatch):
    rag_root = tmp_path / "rag"
    rag_root.mkdir()
    repo = tmp_path / "myrepo"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    registry = tmp_path / "projects.json"
    monkeypatch.setenv("PROJECT_RAG_REGISTRY_PATH", str(registry))
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    monkeypatch.setattr("coordinator_core._settings_home.settings_home", lambda: tmp_path / "home")
    monkeypatch.setattr(reg, "detect_example_retrieval_repo", lambda: reg.RagInstall(root=rag_root, interpreter="py"))
    monkeypatch.setattr(reg, "extractor_install_argv", lambda rag, langs=None: None)
    return repo, registry


def test_registered_root_is_skipped_without_spawn(env):
    repo, registry = env
    registry.write_text(json.dumps({"sources": [{"path": str(repo)}]}), encoding="utf-8")
    calls = []
    rec = _index.index_step(repo, "repos.myrepo", spawn=lambda *a: calls.append(a) or True)
    assert rec["status"] == "skipped" and "state=registered" in rec["detail"]
    assert calls == []


def test_absent_root_launches_one_narrowed_spawn(env, tmp_path):
    repo, _ = env
    calls = []
    rec = _index.index_step(repo, "repos.myrepo", spawn=lambda *a: calls.append(a) or True)
    assert rec["status"] == "done" and "state=launched" in rec["detail"]
    assert len(calls) == 1
    plan = json.loads(Path(calls[0][2][0]).read_text(encoding="utf-8"))
    assert list(plan["roots"]) == ["myrepo"]
    assert list(plan["register_argv"]) == ["myrepo"] and list(plan["index_argv"]) == ["myrepo"]
    assert Path(plan["roots"]["myrepo"]) == repo.resolve()
    assert Path(plan["status_path"]).parent == tmp_path / "home" / "rag-fleet-registration" / "myrepo"
    assert rec["changed"] == [calls[0][2][0]]


def test_no_rag_fails_with_named_reason(env, monkeypatch):
    repo, _ = env
    monkeypatch.setattr(reg, "detect_example_retrieval_repo", lambda: None)
    rec = _index.index_step(repo, "repos.myrepo", spawn=lambda *a: True)
    assert rec["status"] == "failed" and "example_retrieval_repo_not_detected" in rec["detail"]


def test_spawn_failure_is_failed(env):
    repo, _ = env
    rec = _index.index_step(repo, "repos.myrepo", spawn=lambda *a: False)
    assert rec["status"] == "failed" and "spawn_failed" in rec["detail"]


def test_disable_env_skips_launch(env, monkeypatch):
    repo, _ = env
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    calls = []
    rec = _index.index_step(repo, "repos.myrepo", spawn=lambda *a: calls.append(a) or True)
    assert rec["status"] == "skipped" and "state=disabled" in rec["detail"]
    assert calls == []

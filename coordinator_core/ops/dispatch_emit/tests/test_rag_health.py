"""rag_health.repo_verdict: example-retrieval-repo's persisted per-repo health verdict, both sides of each decision."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import rag_health

OK = {"state": "ok", "reason": "ready", "checked_at": "2026-11-19T23:52:03Z"}


def _home(tmp_path: Path, lanes: dict, slug: str = "demo", extra: dict | None = None, repo: Path | None = None):
    repo = repo or (tmp_path / "work" / "demo")
    home = tmp_path / "project-rag"
    home.mkdir()
    (home / "served-repos.json").write_text(
        json.dumps({"repos": [{"slug": slug, "root": str(repo)}]}), encoding="utf-8"
    )
    record = {"corpus_health": lanes}
    record.update(extra or {})
    (home / "corpus-state.json").write_text(json.dumps({"repos": {slug: record}}), encoding="utf-8")
    return home, repo


def test_semantic_lane_not_ok_names_state_and_reason(tmp_path):
    lanes = {
        "structural_exact": OK,
        "semantic_served_set": {"state": "degraded", "reason": "no project collection", "checked_at": "2026-11-19T23:52:03Z"},
    }
    home, repo = _home(tmp_path, lanes)
    entry = rag_health.repo_verdict(repo, home)
    assert entry["source"] == "project_health"
    assert entry["verdict"] == "degraded"
    assert "no project collection" in entry["reason"]
    assert entry["lanes"] == [{"lane": "semantic_served_set", "state": "degraded", "reason": "no project collection"}]
    assert entry["stale_since"] == "2026-11-19T23:52:03Z"
    assert entry["next_action"]


def test_all_lanes_ok_returns_none(tmp_path):
    home, repo = _home(tmp_path, {"structural_exact": OK, "semantic_served_set": OK})
    assert rag_health.repo_verdict(repo, home) is None


def test_persisted_semantic_search_block_not_ok_is_the_verdict(tmp_path):
    block = {"verdict": "broken", "reason": "no project collection", "indexed_files": 880, "eligible_files": 6846,
             "checked_at": "2026-11-25T10:00:00Z"}
    home, repo = _home(tmp_path, {"semantic_served_set": OK}, extra={"semantic_search": block})
    entry = rag_health.repo_verdict(repo, home)
    assert entry["verdict"] == "broken"
    assert "880/6846" in entry["reason"]
    assert entry["stale_since"] == "2026-11-25T10:00:00Z"


def test_persisted_semantic_search_block_ok_does_not_flag(tmp_path):
    block = {"verdict": "ok", "reason": "served", "checked_at": "2026-11-25T10:00:00Z"}
    home, repo = _home(tmp_path, {"semantic_served_set": OK}, extra={"semantic_search": block})
    assert rag_health.repo_verdict(repo, home) is None


def test_non_semantic_degraded_lane_still_an_entry(tmp_path):
    lanes = {"semantic_served_set": OK, "research_x": {"state": "degraded", "reason": "loading", "checked_at": "2026-11-19T23:00:00Z"}}
    home, repo = _home(tmp_path, lanes)
    entry = rag_health.repo_verdict(repo, home)
    assert entry["verdict"] == "degraded"
    assert entry["stale_since"] == "2026-11-19T23:00:00Z"


def test_all_ok_but_older_than_index_change_is_stale(tmp_path):
    home, repo = _home(tmp_path, {"semantic_served_set": OK})
    marker = home / "indexes" / "demo" / "root" / ".index_job.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    entry = rag_health.repo_verdict(repo, home)
    assert entry["verdict"] == "stale"
    assert entry["stale_since"] == OK["checked_at"]


def test_all_ok_newer_than_index_change_is_none(tmp_path):
    home, repo = _home(tmp_path, {"semantic_served_set": {**OK, "checked_at": "2999-01-01T00:00:00Z"}})
    marker = home / "indexes" / "demo" / "root" / ".index_job.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    assert rag_health.repo_verdict(repo, home) is None


def test_missing_files_is_health_unreadable(tmp_path):
    home = tmp_path / "project-rag"
    home.mkdir()
    entry = rag_health.repo_verdict(tmp_path / "x", home)
    assert entry["verdict"] == "health_unreadable"
    assert entry["source"] == "project_health"


def test_corrupt_json_is_health_unreadable(tmp_path):
    home, repo = _home(tmp_path, {"semantic_served_set": OK})
    (home / "corpus-state.json").write_text("{not json", encoding="utf-8")
    assert rag_health.repo_verdict(repo, home)["verdict"] == "health_unreadable"


def test_repo_absent_from_served_repos_is_unknown_repo(tmp_path):
    home, _repo = _home(tmp_path, {"semantic_served_set": OK})
    entry = rag_health.repo_verdict(tmp_path / "elsewhere", home)
    assert entry["verdict"] == "health_unknown_repo"


def test_slug_absent_from_corpus_state_is_unknown_repo(tmp_path):
    home, repo = _home(tmp_path, {"semantic_served_set": OK})
    (home / "corpus-state.json").write_text(json.dumps({"repos": {"other": {}}}), encoding="utf-8")
    assert rag_health.repo_verdict(repo, home)["verdict"] == "health_unknown_repo"


def test_default_home_comes_from_settings_home(tmp_path, monkeypatch):
    home, repo = _home(tmp_path, {"semantic_served_set": {"state": "unknown", "reason": "r", "checked_at": "2026-11-19T00:00:00Z"}})
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    assert rag_health.repo_verdict(repo)["verdict"] == "unknown"


def test_read_spawns_no_subprocess(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    home, repo = _home(tmp_path, {"semantic_served_set": OK})
    assert rag_health.repo_verdict(repo, home) is None


@pytest.mark.skipif(os.name != "nt", reason="case-insensitive root match is a Windows rule")
def test_root_match_is_case_and_separator_insensitive_on_windows(tmp_path):
    home, repo = _home(tmp_path, {"semantic_served_set": OK})
    assert rag_health.repo_verdict(str(repo).upper().replace("\\", "/"), home) is None

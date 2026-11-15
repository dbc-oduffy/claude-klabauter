"""Tests for the skill.preconditions op: one verdict per registered check, no spawn."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import skill_preconditions as sp

NOW = 1_800_000_000.0
CACHE_REL = ".coordinator-local/index-freshness-verdict.json"

CHECK = {
    "id": "example-retrieval-repo-index-freshness",
    "command": "read the verdict and checked_at from reads_cache",
    "reads_cache": CACHE_REL,
    "max_age_hours": 6,
    "fallback_command": "example-retrieval-repo-cli freshness-verdict --repo <slug> --line",
    "on_stale": "queue",
    "applies_when": "consumer repo with a example-retrieval-repo index",
}


@pytest.fixture
def plugin(tmp_path: Path) -> Path:
    root = tmp_path / "plugin"
    root.mkdir()
    (root / sp.REGISTRY_FILENAME).write_text(
        yaml.safe_dump({"skills": {"distill": [CHECK], "odd": [{"id": "x", "command": "run it", "on_stale": "queue"}]}}),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def no_spawn(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("skill.preconditions must not spawn a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)


@pytest.fixture
def indexed(monkeypatch):
    monkeypatch.setattr(sp, "_has_index", lambda repo: True)


def write_cache(repo: Path, *, age_hours: float, verdict: str = "current", **extra) -> None:
    dest = repo / CACHE_REL
    dest.parent.mkdir(parents=True, exist_ok=True)
    body = {"cache_version": 1, "checked_epoch": NOW - age_hours * 3600, "verdict": verdict, "reason": None, **extra}
    dest.write_text(json.dumps(body), encoding="utf-8")


def run(plugin: Path, repo: Path, skill: str = "distill") -> dict:
    return sp.skill_preconditions(skill, str(repo), plugin_roots=[plugin], now=NOW)


def only(result: dict) -> dict:
    assert len(result["checks"]) == 1
    return result["checks"][0]


def test_fresh_cache_reads_fresh_with_age_and_on_stale(plugin, repo, indexed):
    write_cache(repo, age_hours=1.0)
    check = only(run(plugin, repo))
    assert check["verdict"] == "fresh"
    assert check["id"] == "example-retrieval-repo-index-freshness"
    assert check["on_stale"] == "queue"
    assert check["age_hours"] == 1.0
    assert check["checked_at"].endswith("Z")


def test_cache_older_than_max_age_reads_stale(plugin, repo, indexed):
    write_cache(repo, age_hours=7.0)
    assert only(run(plugin, repo))["verdict"] == "stale"


def test_cache_at_the_age_bound_still_reads_fresh(plugin, repo, indexed):
    write_cache(repo, age_hours=6.0)
    assert only(run(plugin, repo))["verdict"] == "fresh"


def test_missing_cache_reads_absent(plugin, repo, indexed):
    check = only(run(plugin, repo))
    assert check["verdict"] == "absent"
    assert check["on_stale"] == "queue"


def test_unparseable_cache_reads_absent(plugin, repo, indexed):
    (repo / ".coordinator-local").mkdir()
    (repo / CACHE_REL).write_text("{not json", encoding="utf-8")
    assert only(run(plugin, repo))["verdict"] == "absent"


def test_cache_of_another_version_reads_absent(plugin, repo, indexed):
    write_cache(repo, age_hours=1.0, cache_version=2)
    assert only(run(plugin, repo))["verdict"] == "absent"


def test_cached_stale_verdict_reads_stale_even_when_young(plugin, repo, indexed):
    write_cache(repo, age_hours=0.5, verdict="stale", reason="behind_head")
    check = only(run(plugin, repo))
    assert check["verdict"] == "stale"
    assert check["reason"] == "behind_head"


def test_cached_unknown_verdict_never_reads_fresh(plugin, repo, indexed):
    write_cache(repo, age_hours=0.5, verdict="unknown")
    assert only(run(plugin, repo))["verdict"] == "stale"


def test_cached_absent_verdict_reads_absent(plugin, repo, indexed):
    write_cache(repo, age_hours=0.5, verdict="absent", reason="no_structural_db")
    assert only(run(plugin, repo))["verdict"] == "absent"


def test_repo_without_a_example_retrieval_repo_index_is_not_applicable(plugin, repo, monkeypatch):
    monkeypatch.setattr(sp, "_has_index", lambda r: False)
    write_cache(repo, age_hours=1.0)
    assert only(run(plugin, repo))["verdict"] == "not_applicable"


def test_unregistered_tmp_repo_is_not_applicable_through_the_real_slug_lookup(plugin, repo):
    assert only(run(plugin, repo))["verdict"] == "not_applicable"


def test_unknown_skill_returns_empty_checks_naming_the_skill(plugin, repo):
    result = run(plugin, repo, "no-such-skill")
    assert result["skill"] == "no-such-skill"
    assert result["checks"] == []


def test_missing_registry_returns_empty_checks(tmp_path, repo):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = run(empty, repo)
    assert result["checks"] == []
    assert result["registry_found"] is False


def test_check_outside_the_cache_read_shape_is_unsupported_and_names_its_id(plugin, repo, indexed):
    check = only(run(plugin, repo, "odd"))
    assert check == {"id": "x", "on_stale": "queue", "verdict": "unsupported"}


@pytest.mark.parametrize("rel", ["/etc/passwd", "../outside.json"])
def test_cache_path_escaping_the_repo_is_unsupported(plugin, repo, indexed, rel):
    (plugin / sp.REGISTRY_FILENAME).write_text(
        yaml.safe_dump({"skills": {"distill": [{**CHECK, "reads_cache": rel}]}}), encoding="utf-8"
    )
    assert only(run(plugin, repo))["verdict"] == "unsupported"


def test_registry_under_a_coordinator_subdir_is_found(tmp_path, repo, indexed):
    root = tmp_path / "clone"
    (root / "coordinator").mkdir(parents=True)
    (root / "coordinator" / sp.REGISTRY_FILENAME).write_text(
        yaml.safe_dump({"skills": {"distill": [CHECK]}}), encoding="utf-8"
    )
    write_cache(repo, age_hours=1.0)
    assert only(run(root, repo))["verdict"] == "fresh"


def test_handler_resolves_plugin_root_from_env_and_runs_end_to_end(plugin, repo, indexed, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin))
    write_cache(repo, age_hours=0.1)
    reply = sp._skill_preconditions({"skill": "distill", "repo_root": str(repo)})
    assert reply["ok"] is True
    assert only(reply)["verdict"] == "fresh"


def test_handler_refuses_a_missing_skill_without_raising():
    assert sp._skill_preconditions({"repo_root": "x"})["ok"] is False

"""SessionStart retirement of superseded rules files by the global-doctrine deriver."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.hooks import derive_global_doctrine_live_copy as mod

RETIRED = ("context7.md", "fleet-friction-reporting.md")


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "global-doctrine" / "rules").mkdir(parents=True)
    (repo / ".coordinator-dev-repo").write_text("", encoding="utf-8")
    (repo / "global-doctrine" / "CLAUDE.md").write_text("doctrine", encoding="utf-8")
    (repo / "global-doctrine" / "rules" / "rules.md").write_text("r", encoding="utf-8")
    live_rules = tmp_path / "home" / "rules"
    live_rules.mkdir(parents=True)
    published = repo / "coordinator" / "templates" / "global-doctrine" / "rules"
    published.mkdir(parents=True)
    monkeypatch.setattr(mod, "_resolve_doctrine_repo_root", lambda: repo)
    monkeypatch.setattr(mod, "_live_rules_dir", lambda: live_rules)
    monkeypatch.setattr(mod, "_live_path", lambda: tmp_path / "home" / "CLAUDE.md")
    return repo, live_rules, published


def _session_start():
    return mod.evaluate({"hook_event_name": "SessionStart"})


def test_retires_from_live_and_published_when_untracked(env):
    repo, live, published = env
    for d in (live, published):
        for name in RETIRED:
            (d / name).write_text("old", encoding="utf-8")
    (live / "operator-own.md").write_text("keep", encoding="utf-8")

    _session_start()

    for d in (live, published):
        for name in RETIRED:
            assert not (d / name).exists()
    assert (live / "operator-own.md").exists()
    assert (live / "rules.md").exists()
    assert (published / "rules.md").exists()


def test_tracked_name_is_not_retired(env):
    repo, live, published = env
    (repo / "global-doctrine" / "rules" / "context7.md").write_text("tracked", encoding="utf-8")
    (live / "fleet-friction-reporting.md").write_text("old", encoding="utf-8")
    (published / "context7.md").write_text("stale", encoding="utf-8")

    _session_start()

    assert (live / "context7.md").read_text(encoding="utf-8") == "tracked"
    assert (published / "context7.md").read_text(encoding="utf-8") == "tracked"
    assert not (live / "fleet-friction-reporting.md").exists()


def test_non_dev_repo_retires_nothing(env, tmp_path):
    repo, live, published = env
    (repo / ".coordinator-dev-repo").unlink()
    (live / "context7.md").write_text("old", encoding="utf-8")

    assert _session_start() is None
    assert (live / "context7.md").exists()

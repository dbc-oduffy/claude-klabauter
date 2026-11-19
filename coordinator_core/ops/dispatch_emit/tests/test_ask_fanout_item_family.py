"""dispatch.ask_fanout: an asks entry's family lands on its own slice sizing only; an unknown family refuses."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops.dispatch_emit import ask_fanout as mod
from coordinator_core.ops.dispatch_emit import stage_family
from coordinator_core.session_baton import store

SID = "sid-item-family"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "hub" / SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    assert PT.append_turn("plan these", session_id=SID)["ok"]
    return tmp_path


def _go(repo, asks):
    return mod.fanout({"asks": asks, "session_id": SID}, repo)


def _files(repo):
    return sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file() and "hub" not in p.parts)


def test_family_lands_on_exactly_the_entry_that_set_it(repo) -> None:
    out = _go(repo, [
        {"ask": "fix a", "writes": ["a.py"], "family": "grok"},
        {"ask": "fix b", "writes": ["b.py"]},
    ])
    assert out["exit_code"] == 0, out
    docs = [yaml.safe_load((repo / r["sizing"]).read_text(encoding="utf-8")) for r in out["ready"]]
    assert [d.get("family") for d in docs] == ["grok", None]


def test_claude_family_writes_no_family_key(repo) -> None:
    out = _go(repo, [{"ask": "fix a", "family": "claude"}, {"ask": "fix b", "family": "codex"}])
    assert out["exit_code"] == 0, out
    docs = [yaml.safe_load((repo / r["sizing"]).read_text(encoding="utf-8")) for r in out["ready"]]
    assert [d.get("family") for d in docs] == [None, "codex"]


def test_unknown_family_refuses_naming_the_key_and_writes_nothing(repo) -> None:
    before = _files(repo)
    out = _go(repo, [{"ask": "fix a"}, {"ask": "fix b", "family": "gemini"}])
    assert out["exit_code"] == 1 and out["applied"] is False
    assert "asks[1].family" in out["error"] and "gemini" in out["error"]
    assert _files(repo) == before


def test_accepted_set_is_the_stage_family_set() -> None:
    assert set(mod._NON_CLAUDE_FAMILIES) | {"claude"} == set(stage_family.FAMILIES)

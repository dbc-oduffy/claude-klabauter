"""dispatch.ask_fanout: an asks entry's stop_after lands on its own slice sizing; a bad value refuses."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops.dispatch_emit import ask_fanout as mod
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.session_baton import store

SID = "sid-stop-after"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "hub" / SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    assert PT.append_turn("plan these", session_id=SID)["ok"]
    monkeypatch.setattr(c, "concurrent_run_budget", lambda physical_cores=None: 2)
    return tmp_path


def _go(repo, asks):
    return mod.fanout({"asks": asks, "session_id": SID}, repo)


def _files(repo):
    return sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file() and "hub" not in p.parts)


def test_stop_after_lands_on_exactly_the_entry_that_set_it(repo):
    out = _go(repo, [
        {"ask": "fix a", "writes": ["a.py"], "stop_after": "plan"},
        {"ask": "fix b", "writes": ["b.py"]},
    ])
    assert out["exit_code"] == 0, out
    docs = [yaml.safe_load((repo / r["sizing"]).read_text(encoding="utf-8")) for r in out["ready"]]
    assert [d["em_analysis"].get("stop_after") for d in docs] == ["plan", None]


def test_bad_stop_after_refuses_naming_the_key_and_writes_nothing(repo):
    before = _files(repo)
    out = _go(repo, [{"ask": "fix a"}, {"ask": "fix b", "stop_after": "execute"}])
    assert out["exit_code"] == 1 and out["applied"] is False
    assert "asks[1].stop_after" in out["error"] and "'plan'" in out["error"]
    assert _files(repo) == before

"""dispatch.ask_fanout: an asks entry's context lands on its slice sizing; an unknown entry key refuses."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops.dispatch_emit import ask_fanout as mod
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.sizing_intake_contract import EM_CONTEXT_MARKER
from coordinator_core.session_baton import store

SID = "sid-entry-context"
VERBATIM = "split these two jobs please, they are unrelated"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "hub" / SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    assert PT.append_turn(VERBATIM, session_id=SID)["ok"]
    monkeypatch.setattr(c, "concurrent_run_budget", lambda physical_cores=None: 2)
    return tmp_path


def _go(repo, asks):
    return mod.fanout({"asks": asks, "session_id": SID}, repo)


def _sizing(repo, rel):
    return yaml.safe_load((repo / rel).read_text(encoding="utf-8"))


def _files(repo):
    return sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*") if p.is_file() and "hub" not in p.parts)


def test_entry_context_is_stamped_on_its_own_slice_only(repo):
    out = _go(repo, [
        {"ask": "fix a", "writes": ["a.py"]},
        {"ask": "fix b", "writes": ["b.py"], "context": ["a bug in x", "docs/a.md"]},
    ])
    assert out["exit_code"] == 0, out
    first, second = (_sizing(repo, r["sizing"]) for r in out["ready"])
    brief = second["em_analysis"]["em_brief"]
    assert brief.startswith(EM_CONTEXT_MARKER) and "a bug in x" in brief
    assert second["em_analysis"]["em_context_paths"] == "docs/a.md"
    assert "em_brief" not in first["em_analysis"]
    assert "em_context_paths" not in first["em_analysis"]


def test_em_context_and_prose_context_share_one_marker(repo):
    out = _go(repo, [{"ask": "fix a", "em_context": "why", "context": "more prose"}])
    assert out["exit_code"] == 0, out
    brief = _sizing(repo, out["ready"][0]["sizing"])["em_analysis"]["em_brief"]
    assert brief.count(EM_CONTEXT_MARKER) == 1
    assert brief.index("why") < brief.index("more prose")


def test_pm_entry_context_leaves_verbatim_and_intent_alone(repo):
    out = _go(repo, [
        {"ask": "fix a", "source": "pm", "context": ["a bug in x"], "writes": ["a.py"]},
        {"ask": "fix b", "source": "pm", "writes": ["b.py"]},
    ])
    assert out["exit_code"] == 0, out
    doc = _sizing(repo, out["ready"][0]["sizing"])
    assert doc["intent"] == "fix a"
    assert [v["text"] for v in doc["pm_verbatims"]] == [VERBATIM]
    assert "a bug in x" in doc["em_analysis"]["em_brief"]


def test_unknown_entry_key_refuses_naming_it_and_writes_nothing(repo):
    before = _files(repo)
    out = _go(repo, [{"ask": "fix a"}, {"ask": "fix b", "contxt": ["x"]}])
    assert out["exit_code"] == 1 and out["applied"] is False
    assert "asks[1]" in out["error"] and "contxt" in out["error"]
    assert all(k in out["error"] for k in c.ASKS_ITEM_KEYS)
    assert _files(repo) == before


@pytest.mark.parametrize("bad", [5, [1], {"a": "b"}])
def test_malformed_context_refuses(repo, bad):
    out = _go(repo, [{"ask": "fix a", "context": bad}])
    assert out["exit_code"] == 1 and "context" in out["error"]

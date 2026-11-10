"""dispatch.ask_fanout: an asks entry's writes rides its child sizing into the XS footprint check."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops.dispatch_emit import ask_fanout as mod
from coordinator_core.ops.dispatch_emit.sizing_fire import ARM_XS, collect_fire_refusals
from coordinator_core.session_baton import store

SID = "sid-xs-child-writes"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "hub" / SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    assert PT.append_turn("fix these", session_id=SID)["ok"]
    return tmp_path


def _children(repo, asks):
    out = mod.fanout({"asks": asks, "session_id": SID}, repo)
    assert out["exit_code"] == 0, out
    return [yaml.safe_load((repo / r["sizing"]).read_text(encoding="utf-8")) for r in out["ready"]]


def _xs_refusals(sizing, writes=()):
    refusals = collect_fire_refusals(sizing, sizing_rel="s.yaml", arm=ARM_XS, writes=list(writes))
    return [r for r in refusals if r.startswith("XS needs `writes`")]


def test_entry_writes_clear_the_xs_footprint_check_for_that_child_only(repo):
    with_writes, without = _children(repo, [
        {"ask": "fix a", "writes": ["a.py", "b.py"]},
        {"ask": "fix b"},
    ])
    assert _xs_refusals(with_writes) == []
    assert len(_xs_refusals(without)) == 1


def test_explicit_args_writes_still_clear_a_child_without_entry_writes(repo):
    _, without = _children(repo, [{"ask": "fix a", "writes": ["a.py"]}, {"ask": "fix b"}])
    assert _xs_refusals(without, writes=["b.py"]) == []

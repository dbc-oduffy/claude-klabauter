"""ask_gate writes the resume snapshot on a row resume with a run_id and clears it otherwise."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate, resume_snapshot
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan  # noqa: F401

RUN = "ask-20261017T000000-aaaaaa"


@pytest.fixture(autouse=True)
def _no_bind(monkeypatch):
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)


def _call(repo, run_id=RUN):
    params = {"sizing_path": REL}
    if run_id:
        params["run_id"] = run_id
    return ask_gate._handler(params, repo_root=repo / ".git")


def _snap(repo, run_id=RUN):
    return resume_snapshot.read(repo, run_id, PLAN_REL)


def test_resume_writes_plan_ids_minus_resume_rows(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("A1", "coded"), ("A2", "open"), ("A3", "delivered_elsewhere"), ("A4", "open")])
    reply = _call(repo)
    assert reply["resume_rows"] == ["A2", "A4"]
    assert _snap(repo) == resume_snapshot.Snapshot(frozenset({"A1", "A3"}), frozenset({"A2", "A4"}))


def test_no_run_id_writes_nothing(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("A1", "coded"), ("A2", "open")])
    _call(repo, run_id=None)
    assert _snap(repo) is None


def test_no_resume_removes_an_earlier_snapshot(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("A1", "coded"), ("A2", "open")])
    _call(repo)
    assert _snap(repo) == resume_snapshot.Snapshot(frozenset({"A1"}), frozenset({"A2"}))
    (repo / PLAN_REL).unlink()
    reply = _call(repo)
    assert "resume_rows" not in reply
    assert _snap(repo) is None


def test_unsafe_run_id_writes_nothing(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("A1", "coded"), ("A2", "open")])
    _call(repo, run_id="../escape")
    assert not (repo / "scratch" / "escape").exists()

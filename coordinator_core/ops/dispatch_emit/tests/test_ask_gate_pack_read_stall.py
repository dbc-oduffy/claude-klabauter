"""dispatch.ask_gate: the landed-outside read gets one bounded retry, then a persisting stall halts `pack-read-stall`."""

from __future__ import annotations

import time

import pytest

from coordinator_core.git.git_objects import PackReadDeadline
from coordinator_core.ops.dispatch_emit import ask_gate, plan_row_coding
from coordinator_core.ops.dispatch_emit.halt_remedy_class import PACK_READ_STALL_HALT_KIND
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_refire_ledger_halted_sizing_resumes import _git

_ROWS = [{"id": "C1", "writes": ["a.py"]}]


@pytest.fixture(autouse=True)
def _git_repo(repo, monkeypatch):  # noqa: F811
    _git(repo, "init", "-q")
    monkeypatch.setattr("coordinator_core.git.git_state.head_sha", lambda root: "a" * 40)
    monkeypatch.setattr(time, "sleep", lambda *_: pytest.fail("a wait loop"))


def test_one_stall_is_absorbed_by_the_retry(repo, monkeypatch):  # noqa: F811
    calls = []

    def outside(*_a, **_k):
        calls.append(1)
        if len(calls) == 1:
            raise PackReadDeadline("stalled")
        return {"C1": ["b" * 40]}

    monkeypatch.setattr(plan_row_coding, "outside_landings", outside)
    assert plan_row_coding.rows_landed_outside_executor(repo, "plan.md", _ROWS) == {"C1": "b" * 40}
    assert len(calls) == 2


def test_a_persisting_stall_is_not_swallowed_by_the_reader(repo, monkeypatch):  # noqa: F811
    from coordinator_core.git.git_objects import PackReadStalled

    def outside(*_a, **_k):
        raise PackReadDeadline("stalled")

    monkeypatch.setattr(plan_row_coding, "outside_landings", outside)
    with pytest.raises(PackReadStalled):
        plan_row_coding.rows_landed_outside_executor(repo, "plan.md", _ROWS)


def test_gate_replies_a_pack_read_stall_halt_with_a_refire(repo, monkeypatch):  # noqa: F811
    from coordinator_core.git.git_objects import PackReadStalled

    def body(params, repo_root=None):
        raise PackReadStalled("pack read exceeded 4.0s")

    monkeypatch.setattr(ask_gate, "_gate_body", body)
    out = ask_gate._handler({"sizing_path": REL}, repo)
    assert "error" not in out
    assert out["halt"]["kind"] == PACK_READ_STALL_HALT_KIND
    assert "refire" in out["halt"]["next_action"] and REL in out["halt"]["next_action"]


def test_gate_does_not_catch_other_errors(repo, monkeypatch):  # noqa: F811
    def body(params, repo_root=None):
        raise PackReadDeadline("latched, not retried")

    monkeypatch.setattr(ask_gate, "_gate_body", body)
    with pytest.raises(PackReadDeadline):
        ask_gate._handler({"sizing_path": REL}, repo)

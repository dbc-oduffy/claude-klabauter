"""session_sizing_state: route/plan semantics and the fail-safe default."""

from __future__ import annotations

import pytest

from coordinator_core.hooks import flag_em_poll_in_flight as poll
from coordinator_core.hooks import nudge_unrouted_sizing as sizing
from coordinator_core.session.engine_route_state import ProbeFault, session_sizing_state


def _touched(monkeypatch, rels, route="dispatch", plans=()):
    monkeypatch.setattr(sizing, "_repo_root", lambda p: "/repo")
    monkeypatch.setattr(sizing, "_session_touched_sizing_files", lambda s, r: rels)
    monkeypatch.setattr(sizing, "_load_sizing_object", lambda r, rel: {"route": route})
    monkeypatch.setattr(sizing, "_session_touched_plan_files", lambda s, r: list(plans))


@pytest.mark.parametrize("route,plans,pending", [
    ("plan", [], True), ("spec-dispatch", [], True),
    ("plan", ["docs/plans/p.md"], False), ("dispatch", [], False),
])
def test_touched_sizing_reads_the_route(monkeypatch, route, plans, pending) -> None:
    _touched(monkeypatch, ["state/sizings/a.yaml"], route, plans)
    assert session_sizing_state({"cwd": "/repo"}, "s") == (True, pending)


@pytest.mark.parametrize("runs,expected", [(["r1"], True), ([], False)])
def test_no_sizing_falls_back_to_session_workflow_runs(monkeypatch, runs, expected) -> None:
    _touched(monkeypatch, [])
    monkeypatch.setattr(poll, "session_workflow_run_ids", lambda s: runs)
    assert session_sizing_state({"cwd": "/repo"}, "s") == (expected, False)


def test_any_exception_fails_safe(monkeypatch) -> None:
    def boom(p):
        raise RuntimeError("x")

    monkeypatch.setattr(sizing, "_repo_root", boom)
    state = session_sizing_state({"cwd": "/repo"}, "s")
    assert state == (True, False) and isinstance(state, ProbeFault)


def test_no_repo_root_fails_safe(monkeypatch) -> None:
    monkeypatch.setattr(sizing, "_repo_root", lambda p: None)
    state = session_sizing_state({"cwd": "/repo"}, "s")
    assert state == (True, False) and not isinstance(state, ProbeFault)

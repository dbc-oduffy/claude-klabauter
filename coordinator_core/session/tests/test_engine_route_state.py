"""session_sizing_state: route/plan semantics and the fail-safe default."""

from __future__ import annotations

import json

import pytest

from coordinator_core.hooks import flag_em_poll_in_flight as poll
from coordinator_core.hooks import nudge_unrouted_sizing as sizing
from coordinator_core.session.engine_route_state import ProbeFault, session_route_phase, session_sizing_state


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


def _route(monkeypatch, runs, live):
    from coordinator_core import warp_live

    monkeypatch.setattr(poll, "session_workflow_run_ids", lambda s: runs)
    monkeypatch.setattr(warp_live, "live_run_for_session", lambda root, sid: live)


def _marker(tmp_path, run_id, status=None):
    d = tmp_path / "state" / "warp" / run_id
    d.mkdir(parents=True)
    if status:
        (d / "halted.json").write_text(json.dumps({"status": status}), encoding="utf-8")


def test_phase_unfired_without_run_ids(monkeypatch, tmp_path) -> None:
    _route(monkeypatch, [], None)
    phase = session_route_phase(tmp_path, "s")
    assert phase == "unfired" and phase.run_id is None


def test_phase_in_flight_names_the_live_run(monkeypatch, tmp_path) -> None:
    _route(monkeypatch, ["w1"], "ask-1")
    phase = session_route_phase(tmp_path, "s")
    assert phase == "in_flight" and phase.run_id == "ask-1"


def test_phase_landed_green_with_no_marker(monkeypatch, tmp_path) -> None:
    _route(monkeypatch, ["w1"], None)
    _marker(tmp_path, "ask-1")
    phase = session_route_phase(tmp_path, "s", "ask-1")
    assert phase == "landed_green" and phase.run_id == "ask-1"


def test_phase_landed_green_with_landed_marker(monkeypatch, tmp_path) -> None:
    _route(monkeypatch, ["w1"], None)
    _marker(tmp_path, "ask-1", "landed")
    assert session_route_phase(tmp_path, "s", "ask-1") == "landed_green"


def test_phase_halted_on_halted_marker(monkeypatch, tmp_path) -> None:
    _route(monkeypatch, ["w1"], None)
    _marker(tmp_path, "ask-1", "halted")
    phase = session_route_phase(tmp_path, "s", "ask-1")
    assert phase == "halted" and phase.run_id == "ask-1"


def test_phase_unknown_without_a_remembered_engine_run(monkeypatch, tmp_path) -> None:
    _route(monkeypatch, ["w1"], None)
    assert session_route_phase(tmp_path, "s") == "unknown"


def test_phase_unknown_on_exception(monkeypatch, tmp_path) -> None:
    def boom(s):
        raise RuntimeError("x")

    monkeypatch.setattr(poll, "session_workflow_run_ids", boom)
    assert session_route_phase(tmp_path, "s") == "unknown"

"""halted_run_route: the route line, the marker-gated lookup, and the sizing lookup; none spawns."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run_route as route

SHA = "a" * 40


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("route helpers must not spawn a process")

    monkeypatch.setattr(subprocess, "Popen", boom)


def _marker(root: Path, run_id: str, status: str, sizing_rel: str = "state/sizings/2026-10-23-x.yaml") -> None:
    d = root / "state" / "warp" / run_id
    d.mkdir(parents=True)
    (d / "halted.json").write_text(json.dumps({"status": status, "sizing_rel": sizing_rel}), encoding="utf-8")


def test_route_line_names_op_run_and_slot():
    for sha in (None, SHA):
        assert "dispatch.record_halted_run_landed {run_id: r1, sha: " in route.route_line("r1", sha)
    assert "<the halt commit>" in route.route_line("r1")
    assert SHA in route.route_line("r1", SHA)


def test_halted_route_for_only_halted(tmp_path):
    assert route.halted_route_for(tmp_path, "r-absent") is None
    for status in ("landed", "resumed"):
        _marker(tmp_path, f"r-{status}", status)
        assert route.halted_route_for(tmp_path, f"r-{status}") is None
    _marker(tmp_path, "r-halted", "halted")
    assert route.halted_route_for(tmp_path, "r-halted") == route.route_line("r-halted")


def test_halted_route_for_unsafe_id(tmp_path):
    assert route.halted_route_for(tmp_path, "../x") is None


def _withheld_marker(root: Path, run_id: str, status: str, withheld: dict) -> None:
    d = root / "state" / "warp" / run_id
    d.mkdir(parents=True)
    body = {"status": status, "halted": "room", "plan": "docs/plans/p.md", "run_artifacts": {"withheld": withheld}}
    (d / "halted.json").write_text(json.dumps(body), encoding="utf-8")


def test_land_only_commit_route_payload_passes_the_halted_record_shape(tmp_path):
    from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM
    from coordinator_core.ops.dispatch_emit.halted_record_commit import is_halted_record_call
    from coordinator_core.ops.dispatch_emit.terminal_commit import _is_halted_record_call

    _withheld_marker(tmp_path, "r-held", "halted", {"DELIVERY.md": "kept"})
    payload = route.land_only_commit_route(tmp_path, "r-held")
    assert payload == {"halted": "room", "plan": "docs/plans/p.md"}
    params = {HALTED_RECORD_PARAM: payload}
    assert _is_halted_record_call(params) and is_halted_record_call(params)


def test_land_only_commit_route_none_unless_halted_with_withheld_work(tmp_path):
    _withheld_marker(tmp_path, "r-empty", "halted", {})
    _withheld_marker(tmp_path, "r-landed", "landed", {"a.py": "kept"})
    _withheld_marker(tmp_path, "r-resumed", "resumed", {"a.py": "kept"})
    for rid in ("r-empty", "r-landed", "r-resumed", "r-absent", "../x"):
        assert route.land_only_commit_route(tmp_path, rid) is None, rid
    assert route.land_only_commit_route(tmp_path, None) is None


def test_halted_route_for_sizing(tmp_path):
    sizing = "state/sizings/2026-10-23-x.yaml"
    assert route.halted_route_for_sizing(tmp_path, sizing) is None
    _marker(tmp_path, "ask-20261023T121013-e7c4ae", "halted", sizing)
    assert route.halted_route_for_sizing(tmp_path, sizing, SHA) == route.route_line(
        "ask-20261023T121013-e7c4ae", SHA
    )
    assert route.halted_route_for_sizing(tmp_path, "state/sizings/2026-10-23-other.yaml") is None

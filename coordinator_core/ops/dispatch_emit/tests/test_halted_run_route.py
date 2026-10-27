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


def test_halted_route_for_sizing(tmp_path):
    sizing = "state/sizings/2026-10-23-x.yaml"
    assert route.halted_route_for_sizing(tmp_path, sizing) is None
    _marker(tmp_path, "ask-20261023T121013-e7c4ae", "halted", sizing)
    assert route.halted_route_for_sizing(tmp_path, sizing, SHA) == route.route_line(
        "ask-20261023T121013-e7c4ae", SHA
    )
    assert route.halted_route_for_sizing(tmp_path, "state/sizings/2026-10-23-other.yaml") is None

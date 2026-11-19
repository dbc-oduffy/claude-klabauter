"""The emit's preblitz anchor scan runs under a step deadline: a scan that blocks parks the emit as
a determinate not-started reply naming the step, mints no run, and a re-send proceeds."""

from __future__ import annotations

import threading
import time

import pytest

from coordinator_core.ops import _anchor_check
from coordinator_core.ops.dispatch_emit import ask_compose, land_deadline, op
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_seed import REL, _put, repo  # noqa: F401

PLAN_REL = "docs/plans/2026-11-14-preblitz-fixture.md"
PLAN = """---
anchor_premises:
  - id: AP1
    claim: the fixture file holds one needle
    check:
      command: grep -c needle fixture.txt
      expect: "1"
---
# plan
"""


@pytest.fixture
def planned(repo, monkeypatch):  # noqa: F811
    _put(repo)
    sizing = repo / REL
    text = sizing.read_text(encoding="utf-8").replace("tshirt: XS", "tshirt: M").replace("route: dispatch", "route: plan")
    sizing.write_text(text, encoding="utf-8", newline="\n")
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    (repo / "fixture.txt").write_text("needle\n", encoding="utf-8")
    monkeypatch.setattr(ask_compose, "_static_plan_rel", lambda root, sizing: PLAN_REL)
    return repo


def _run_dirs(root):
    base = root / RECORD_DIR_ROOT
    return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []


@pytest.mark.deliberate_wall_clock(reason="parking inside the step deadline while a scan blocks is the behaviour under test")
def test_a_blocked_count_lines_parks_the_emit_not_started_naming_the_step(planned, monkeypatch):
    release = threading.Event()
    real = _anchor_check._count_lines
    monkeypatch.setattr(_anchor_check, "_count_lines", lambda *a, **k: release.wait(30) or real(*a, **k))
    monkeypatch.setattr(ask_compose, "PREBLITZ_STEP_SECS", 0.3)
    try:
        started = time.monotonic()
        reply = op._dispatch_emit({"ask": True, "sizing_path": REL}, repo_root=planned)
        spent = time.monotonic() - started
    finally:
        release.set()
    assert reply.get("status") == "not-started", {k: str(v)[:300] for k, v in reply.items()}
    assert reply["parked"] is True
    assert reply["step"] == ask_compose.PREBLITZ_STEP
    assert ask_compose.PREBLITZ_STEP in reply["error"]
    assert spent < 5.0
    assert _run_dirs(planned) == []


def test_a_resend_after_the_park_mints_one_run(planned, monkeypatch):
    release = threading.Event()
    real = _anchor_check._count_lines
    monkeypatch.setattr(_anchor_check, "_count_lines", lambda *a, **k: release.wait(30) or real(*a, **k))
    monkeypatch.setattr(ask_compose, "PREBLITZ_STEP_SECS", 0.3)
    try:
        parked = op._dispatch_emit({"ask": True, "sizing_path": REL}, repo_root=planned)
    finally:
        release.set()
    assert parked["status"] == "not-started"
    monkeypatch.setattr(_anchor_check, "_count_lines", real)
    monkeypatch.setattr(ask_compose, "PREBLITZ_STEP_SECS", 5.0)

    reply = op._dispatch_emit({"ask": True, "sizing_path": REL}, repo_root=planned)

    assert reply.get("status") != "not-started" and reply["run_id"]
    assert _run_dirs(planned) == [reply["run_id"]]


def test_a_scan_inside_its_deadline_is_unchanged(planned):
    assert ask_compose.preblitz_anchor_lines(str(planned), PLAN_REL) == []
    assert land_deadline.step_remaining() is None


def test_the_step_cap_is_below_the_warm_dispatch_timeout():
    from coordinator_core import ipc

    assert ask_compose.PREBLITZ_STEP_SECS < ipc._timeout_for("dispatch.emit")

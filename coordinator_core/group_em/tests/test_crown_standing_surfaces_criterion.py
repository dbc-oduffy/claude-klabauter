"""The crown-instruments prime exit criterion, promoted from its plan falsifier.

Criterion: every standing surface a crown reads states what it counted and
when, and no writer can silently destroy another crown-instrument's record.

This is the plan-altitude red-green guard, not a unit test. Its two legs were
authored against the criterion alone -- no ACs, no chunk bodies, no task spine
-- and baselined RED before any of the work existed, which is more than most
tests can say about their own falsifying power. The per-behaviour unit tests
live in `test_watch.py`, `test_watch_heartbeat.py` and `test_idle_report.py`;
what this adds is the conjunction, so that satisfying one leg while quietly
regressing the other cannot go unnoticed.

Leg 2 exercises the real `watch_heartbeat.stamp` against a throwaway
`repo_root`, never a mock -- so a rendering change alone cannot turn it green.

Spec backlink: docs/plans/2026-09-01-the-crowns-standing-surfaces-report-themselves.md
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_FALSIFIER = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "plans"
    / "2026-09-01-the-crowns-standing-surfaces-report-themselves.falsifier.py"
)


def _load():
    """Import the falsifier by path; it is not on any package path."""
    if not _FALSIFIER.exists():
        pytest.skip(f"falsifier not present at {_FALSIFIER}")
    spec = importlib.util.spec_from_file_location("_crown_criterion_falsifier", _FALSIFIER)
    if spec is None or spec.loader is None:
        pytest.skip(f"falsifier at {_FALSIFIER} is not importable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_leg1_every_standing_surface_states_what_it_counted_and_when():
    ok, detail = _load().leg1_states_when()
    assert ok, detail


def test_leg2_no_writer_silently_destroys_another_crowns_record():
    ok, detail = _load().leg2_no_silent_destroy()
    assert ok, detail

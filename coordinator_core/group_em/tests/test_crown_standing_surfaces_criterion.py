
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

"""plan.census_order: orders seeded CensusSlots by estimated census cost, memory only."""

from __future__ import annotations

import builtins
import subprocess
from pathlib import Path
from typing import NamedTuple, Optional

import pytest

from coordinator_core.ops import plan_census_order, plan_mise_prep_probe


class MemoEntry(NamedTuple):
    key: Optional[str]
    ran: tuple
    run_ms: int
    key_ms: Optional[int]


class CensusSlot(NamedTuple):
    memo: dict
    spawning: tuple
    judged_at: Optional[float]


@pytest.fixture
def root(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(plan_mise_prep_probe, "_CENSUS_MEMO", {})
    return tmp_path


def _seed(root: Path, plan: str, spawning=(), memo=None):
    plan_mise_prep_probe._CENSUS_MEMO[(str(root), plan)] = CensusSlot(memo or {}, tuple(spawning), None)


def _call(root: Path, plans):
    return plan_census_order._handler({"plans": plans}, root)


def test_cheaper_plan_first(root):
    _seed(root, "a.md", ["x"], {"x": MemoEntry(None, (), 900, None)})
    _seed(root, "b.md", ["y"], {"y": MemoEntry(None, (), 10, None)})
    assert _call(root, ["a.md", "b.md"])["plans"] == ["b.md", "a.md"]


def test_expected_hit_costs_key_ms_not_run_ms(root):
    _seed(root, "a.md", ["x"], {"x": MemoEntry("k", (), 5000, 7)})
    out = _call(root, ["a.md"])
    assert out["estimates_ms"] == {"a.md": 7}
    assert out["estimated"] == 1


def test_unmeasured_command_costs_median_run_ms(root):
    _seed(root, "a.md", ["x", "y", "z"], {
        "x": MemoEntry(None, (), 10, None),
        "y": MemoEntry(None, (), 30, None),
        "z": MemoEntry(None, (), 50, None),
    })
    _seed(root, "b.md", ["new"], {})
    assert _call(root, ["b.md", "a.md"])["estimates_ms"]["b.md"] == 30


def test_median_defaults_to_one_when_nothing_measured(root):
    _seed(root, "a.md", ["new1", "new2"], {})
    assert _call(root, ["a.md"])["estimates_ms"] == {"a.md": 2}


def test_tie_goes_to_more_never_judged_commands(root):
    _seed(root, "t1.md", ["x"], {"x": MemoEntry(None, (), 4, None)})
    _seed(root, "t2.md", ["y", "n"], {"y": MemoEntry(None, (), 1, None)})
    # measured runs {4, 1}: M = 2.5; t2 = 1 + 2.5 = 3.5 -> 4 (half-even), t1 = 4: a tie
    out = _call(root, ["t1.md", "t2.md"])
    assert out["estimates_ms"]["t1.md"] == out["estimates_ms"]["t2.md"] == 4
    assert out["plans"] == ["t2.md", "t1.md"]


def test_no_slot_plans_go_last_in_input_order(root):
    _seed(root, "c.md", ["x"], {"x": MemoEntry(None, (), 10, None)})
    out = _call(root, ["z.md", "c.md", "a.md"])
    assert out["plans"] == ["c.md", "z.md", "a.md"]
    assert out["estimated"] == 1
    assert out["estimates_ms"]["z.md"] is None


def test_bad_shape_slot_is_no_slot(root):
    plan_mise_prep_probe._CENSUS_MEMO[(str(root), "a.md")] = {"memo": {}}
    plan_mise_prep_probe._CENSUS_MEMO[(str(root), "b.md")] = CensusSlot({}, ("x",), None)
    assert _call(root, ["a.md", "b.md"])["plans"] == ["b.md", "a.md"]


def test_empty_memo_returns_input_order(root):
    plans = ["c.md", "a.md", "b.md"]
    out = _call(root, plans)
    assert out["plans"] == plans and out["estimated"] == 0


@pytest.mark.parametrize("bad", [None, "a.md", {"a": 1}, [1], 3])
def test_non_list_plans_raises(root, bad):
    with pytest.raises(ValueError):
        plan_census_order._handler({"plans": bad}, root)


def test_spawns_nothing_and_opens_no_file(root, monkeypatch):
    _seed(root, "a.md", ["x"], {"x": MemoEntry(None, (), 10, None)})

    def boom(*a, **k):
        raise AssertionError("must not be called")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(builtins, "open", boom)
    assert _call(root, ["a.md"])["plans"] == ["a.md"]

"""Reader-side tests for the ``deep_spawn_worklist`` RoutineSignal (C1).

Pins the emitter's cheap-by-construction contract (reads ONE small JSON file, never
walks the corpus or imports the worklist module) and its five computed_state states.

Spec backlink: state/dispatch-briefs/2026-08-26-the-worklist-gets-a-reader/C1.md
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections import routine_signals
from coordinator_core.ops.emit.sections.routine_signals import _deep_spawn_worklist_signal


def _ctx() -> EmitContext:
    return EmitContext(
        repo_root=Path("."),
        coordinator_root=Path("."),
        central_state_root=Path("./state"),
        git_branch="main",
        git_sha="a" * 40,
        git_sha_short="aaaaaaaa",
        observed_at="2026-08-26T00:00:00Z",
        hostname="test.local",
        repo_name="fixture-owner/fixture-repo",
    )


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_never_imports_the_worklist_module():
    """The emitter must not walk the corpus or import the worklist module -- the hard
    constraint the dispatch brief names as the whole point of this chunk. Pinned by
    asserting the worklist test module is not among this process's imported modules
    after calling the signal function (a corpus-walking import would register it)."""
    worklist_module_name = "coordinator_core.tests.test_deep_per_item_spawn_worklist"
    sys.modules.pop(worklist_module_name, None)
    _deep_spawn_worklist_signal(_ctx())
    assert worklist_module_name not in sys.modules, (
        "the routine_signals reader must never import the worklist module -- doing so "
        "would make collect() pay the corpus-walk cost this chunk exists to avoid"
    )


def test_missing_baseline_is_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(
        routine_signals, "_DEEP_SPAWN_WORKLIST_BASELINE_PATH", tmp_path / "absent.json"
    )
    record = _deep_spawn_worklist_signal(_ctx())
    assert record["kind"] == "deep_spawn_worklist"
    assert record["computed_state"] == "unknown"
    assert record["overdue"] is False
    assert record["inputs"] == {}


def test_stale_when_generated_at_older_than_fourteen_days(monkeypatch, tmp_path):
    baseline_path = tmp_path / "baseline.json"
    old = _iso(datetime.now(timezone.utc) - timedelta(days=20))
    baseline_path.write_text(
        json.dumps(
            {
                "generated_at": old,
                "total_sites": 5,
                "by_depth": {"1": 5},
                "site_keys": [["a.py", "f", "g"]],
                "new_since_last": [],
                "top": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(routine_signals, "_DEEP_SPAWN_WORKLIST_BASELINE_PATH", baseline_path)
    record = _deep_spawn_worklist_signal(_ctx())
    assert record["computed_state"] == "stale"
    assert record["overdue"] is False


def test_first_run_null_new_since_last_is_unknown(monkeypatch, tmp_path):
    baseline_path = tmp_path / "baseline.json"
    fresh = _iso(datetime.now(timezone.utc))
    baseline_path.write_text(
        json.dumps(
            {
                "generated_at": fresh,
                "total_sites": 3,
                "by_depth": {"1": 3},
                "site_keys": [["a.py", "f", "g"]],
                "new_since_last": None,
                "top": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(routine_signals, "_DEEP_SPAWN_WORKLIST_BASELINE_PATH", baseline_path)
    record = _deep_spawn_worklist_signal(_ctx())
    assert record["computed_state"] == "unknown"
    assert record["overdue"] is False
    assert record["inputs"]["new_count"] is None


def test_empty_new_since_last_is_quiet_with_no_top_rows(monkeypatch, tmp_path):
    baseline_path = tmp_path / "baseline.json"
    fresh = _iso(datetime.now(timezone.utc))
    baseline_path.write_text(
        json.dumps(
            {
                "generated_at": fresh,
                "total_sites": 3,
                "by_depth": {"1": 3},
                "site_keys": [["a.py", "f", "g"]],
                "new_since_last": [],
                "top": [{"key": ["a.py", "f", "g"], "depth": 1, "reachable_spawn_sites": 9}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(routine_signals, "_DEEP_SPAWN_WORKLIST_BASELINE_PATH", baseline_path)
    record = _deep_spawn_worklist_signal(_ctx())
    assert record["computed_state"] == "quiet"
    assert record["overdue"] is False
    assert record["inputs"]["top"] == [], (
        "quiet must render NO top rows -- a reader scanning signals must be able to "
        "skip it in one glance (per the dispatch brief)"
    )


def test_regrowth_when_new_since_last_nonempty(monkeypatch, tmp_path):
    baseline_path = tmp_path / "baseline.json"
    fresh = _iso(datetime.now(timezone.utc))
    baseline_path.write_text(
        json.dumps(
            {
                "generated_at": fresh,
                "total_sites": 4,
                "by_depth": {"1": 3, "2": 1},
                "site_keys": [["a.py", "f", "g"], ["b.py", "f", "g"]],
                "new_since_last": [["b.py", "f", "g"]],
                "top": [{"key": ["b.py", "f", "g"], "depth": 2, "reachable_spawn_sites": 5}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(routine_signals, "_DEEP_SPAWN_WORKLIST_BASELINE_PATH", baseline_path)
    record = _deep_spawn_worklist_signal(_ctx())
    assert record["computed_state"] == "regrowth"
    assert record["overdue"] is True
    assert record["inputs"]["new_count"] == 1
    assert len(record["inputs"]["top"]) == 1


def test_collect_emits_seven_records(monkeypatch, tmp_path):
    monkeypatch.setattr(
        routine_signals, "_DEEP_SPAWN_WORKLIST_BASELINE_PATH", tmp_path / "absent.json"
    )
    records, malformed = routine_signals.collect(_ctx())
    assert malformed == []
    assert len(records) == 7
    kinds = [r["kind"] for r in records]
    assert kinds[-1] == "deep_spawn_worklist"

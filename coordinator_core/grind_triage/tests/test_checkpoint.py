"""Checkpoint emission: thresholds cross once, in order, and survive a resume."""

from __future__ import annotations

import json

import pytest

from coordinator_core.grind_triage import checkpoint
from coordinator_core.grind_triage.contract import CHECKPOINT_PERCENTS


def _percents(events):
    return [e.percent for e in events]


@pytest.mark.parametrize(
    "settled,total,expected",
    [
        (0, 8, []),
        (1, 8, []),  # 12.5% — below 25
        (2, 8, [25]),  # exactly 25
        (4, 8, [25, 50]),  # a jump emits every crossed threshold
        (6, 8, [25, 50, 75]),
        (8, 8, [25, 50, 75]),  # 100% adds nothing beyond the three
        (1, 3, [25]),  # 33%
    ],
)
def test_due_events_thresholds(settled, total, expected):
    assert _percents(checkpoint.due_events(settled, total, set())) == expected


def test_due_events_skips_reported():
    assert _percents(checkpoint.due_events(6, 8, {25, 50})) == [75]


def test_empty_run_emits_nothing():
    assert checkpoint.due_events(0, 0, set()) == []


def test_emit_writes_one_line_per_threshold(tmp_path):
    events = checkpoint.emit_checkpoints(tmp_path, 4, 8, run_id="r1")
    assert _percents(events) == [25, 50]
    lines = (tmp_path / checkpoint.PROGRESS_FILE).read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines]
    assert [r["percent"] for r in rows] == [25, 50]
    assert rows[0] == {
        "kind": "grind_checkpoint", "percent": 25, "settled": 4, "total": 8, "run_id": "r1",
    }


def test_emit_is_idempotent_across_calls(tmp_path):
    checkpoint.emit_checkpoints(tmp_path, 2, 8)
    assert checkpoint.emit_checkpoints(tmp_path, 2, 8) == []
    later = checkpoint.emit_checkpoints(tmp_path, 8, 8)
    assert _percents(later) == [50, 75]
    lines = (tmp_path / checkpoint.PROGRESS_FILE).read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(CHECKPOINT_PERCENTS)


def test_reported_percents_ignores_garbage_lines(tmp_path):
    (tmp_path / checkpoint.PROGRESS_FILE).write_text(
        'not json\n{"percent": 25}\n[1]\n{"percent": "x"}\n', encoding="utf-8"
    )
    assert checkpoint.reported_percents(tmp_path) == {25}


def test_missing_progress_file_means_nothing_reported(tmp_path):
    assert checkpoint.reported_percents(tmp_path) == set()

"""
coordinator_core.ops.fleet.tests.test_sweep_status — Tier-T coverage for
`fleet.archive_sweep_status` (AC-3, state/handoffs/2026-08-25_roadmap-archival-
sweeps-03.md).

Imports the handler function directly from
`coordinator_core.ops.fleet.sweep_status`, never resolved by op key — same
rationale as `test_archive_terminal_handoffs.py`: resolving by key would race
any concurrent registration-key work in a peer chunk.

Coverage:
  - Missing receipt file degrades to an empty, healthy result (never raises).
  - Malformed lines (unparseable JSON, non-dict, missing required keys) are
    skipped without blinding the read to the well-formed rows around them.
  - Last-row-per-sweep summarization: `last_outcome`/`last_at`/`last_detail`
    reflect the most recently appended row for that sweep, not the first.
  - `consecutive_failures` counts only the trailing run of `failed` rows and
    resets on any other outcome (including `skipped-gated`/`skipped-contended`,
    which are neither success nor failure).
  - `last_success_at` only advances on `applied`/`nothing-to-do` rows.
  - `unhealthy`/`unhealthy_sweeps`/`healthy` are derived purely from
    `last_outcome == "failed"`.
  - Zero subprocess spawns (a `subprocess.run` spy asserts this directly,
    never inferred from timing).

Negative-spec:
  - Does NOT exercise the real op-registry dispatch path (`ipc.py`) — this
    tests the handler function in isolation, matching the sibling fleet op
    test files' own convention.
  - Does NOT test `_sweep_receipt.record_sweep_outcome`'s own write path —
    that module's existing tests own that; this file only ever reads
    fixtures it writes itself via plain `Path.write_text`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from coordinator_core.ops.fleet import _sweep_receipt
from coordinator_core.ops.fleet.sweep_status import _handler


def _write_receipt(common_dir: Path, rows: list) -> Path:
    path = _sweep_receipt.receipt_path(common_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for row in rows:
        lines.append(row if isinstance(row, str) else json.dumps(row))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_missing_receipt_is_empty_and_healthy(tmp_path):
    result = _handler({}, repo_root=tmp_path)
    assert result == {
        "exit_code": 0,
        "sweeps": [],
        "unhealthy_sweeps": [],
        "healthy": True,
    }


def test_repo_root_none_degrades_to_empty_healthy():
    result = _handler({}, repo_root=None)
    assert result["sweeps"] == []
    assert result["healthy"] is True


def test_malformed_lines_are_skipped_not_fatal(tmp_path):
    good = {"at": "2026-08-27T00:00:00Z", "sweep": "fleet.a", "outcome": "applied", "count": 1}
    rows = [
        "not json at all {{{",
        json.dumps(["not", "a", "dict"]),
        json.dumps({"sweep": "fleet.b"}),  # missing outcome
        json.dumps({"outcome": "applied"}),  # missing sweep
        json.dumps(good),
    ]
    _write_receipt(tmp_path, rows)

    result = _handler({}, repo_root=tmp_path)

    assert len(result["sweeps"]) == 1
    assert result["sweeps"][0]["sweep"] == "fleet.a"
    assert result["sweeps"][0]["last_outcome"] == "applied"
    assert result["healthy"] is True


def test_last_row_per_sweep_wins(tmp_path):
    rows = [
        {"at": "2026-08-25T00:00:00Z", "sweep": "fleet.a", "outcome": "applied", "count": 3},
        {"at": "2026-08-26T00:00:00Z", "sweep": "fleet.a", "outcome": "failed", "count": 0,
         "detail": "boom"},
    ]
    _write_receipt(tmp_path, rows)

    result = _handler({}, repo_root=tmp_path)

    entry = result["sweeps"][0]
    assert entry["last_outcome"] == "failed"
    assert entry["last_at"] == "2026-08-26T00:00:00Z"
    assert entry["last_detail"] == "boom"
    assert entry["unhealthy"] is True
    assert result["unhealthy_sweeps"] == [entry]
    assert result["healthy"] is False


def test_consecutive_failures_counts_trailing_run_only(tmp_path):
    rows = [
        {"at": "2026-08-20T00:00:00Z", "sweep": "fleet.a", "outcome": "failed"},
        {"at": "2026-08-21T00:00:00Z", "sweep": "fleet.a", "outcome": "applied"},
        {"at": "2026-08-22T00:00:00Z", "sweep": "fleet.a", "outcome": "failed"},
        {"at": "2026-08-23T00:00:00Z", "sweep": "fleet.a", "outcome": "failed"},
        {"at": "2026-08-24T00:00:00Z", "sweep": "fleet.a", "outcome": "failed"},
    ]
    _write_receipt(tmp_path, rows)

    result = _handler({}, repo_root=tmp_path)

    entry = result["sweeps"][0]
    assert entry["consecutive_failures"] == 3
    assert entry["last_success_at"] == "2026-08-21T00:00:00Z"


def test_skipped_outcomes_reset_but_do_not_count_as_failure_or_success(tmp_path):
    rows = [
        {"at": "2026-08-20T00:00:00Z", "sweep": "fleet.a", "outcome": "failed"},
        {"at": "2026-08-21T00:00:00Z", "sweep": "fleet.a", "outcome": "skipped-gated"},
        {"at": "2026-08-22T00:00:00Z", "sweep": "fleet.a", "outcome": "failed"},
    ]
    _write_receipt(tmp_path, rows)

    result = _handler({}, repo_root=tmp_path)

    entry = result["sweeps"][0]
    # the skipped-gated row breaks the streak from the first failed row —
    # only the single trailing failed row is counted.
    assert entry["consecutive_failures"] == 1
    assert entry["last_success_at"] is None
    assert entry["unhealthy"] is True


def test_multiple_sweeps_summarized_independently(tmp_path):
    rows = [
        {"at": "2026-08-20T00:00:00Z", "sweep": "fleet.a", "outcome": "applied"},
        {"at": "2026-08-20T00:00:01Z", "sweep": "fleet.b", "outcome": "failed"},
        {"at": "2026-08-21T00:00:00Z", "sweep": "fleet.a", "outcome": "nothing-to-do"},
    ]
    _write_receipt(tmp_path, rows)

    result = _handler({}, repo_root=tmp_path)

    by_sweep = {s["sweep"]: s for s in result["sweeps"]}
    assert by_sweep["fleet.a"]["unhealthy"] is False
    assert by_sweep["fleet.b"]["unhealthy"] is True
    assert result["healthy"] is False
    assert [s["sweep"] for s in result["unhealthy_sweeps"]] == ["fleet.b"]


def test_zero_subprocess_spawns(tmp_path):
    rows = [
        {"at": "2026-08-20T00:00:00Z", "sweep": "fleet.a", "outcome": "applied"},
        {"at": "2026-08-21T00:00:00Z", "sweep": "fleet.a", "outcome": "failed", "detail": "x"},
    ]
    _write_receipt(tmp_path, rows)

    with patch("subprocess.run", wraps=subprocess.run) as spy:
        _handler({}, repo_root=tmp_path)
        assert spy.call_count == 0

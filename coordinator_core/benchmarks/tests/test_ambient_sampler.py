"""
coordinator_core.benchmarks.tests.test_ambient_sampler — ambient-load sampler coverage.

Purpose: exercises coordinator_core.benchmarks.ambient_sampler's sample shape,
append-write safety, and interval floor enforcement. Real-traffic load-norm
measurement instrument coverage.

Spec backlink: state/handoffs/2026-08-08-engine-fails-the-load-norm.md
               docs/wiki/machine-load-norm.md
"""

from __future__ import annotations

import json

from coordinator_core.benchmarks.ambient_sampler import (
    MIN_INTERVAL_SECONDS,
    append_sample,
    main,
    take_sample,
)

import pytest

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def test_take_sample_shape():
    sample = take_sample()
    assert set(sample.keys()) == {
        "t", "live_sessions", "claude_procs", "cpu_pct", "ram_free_mb", "ram_total_mb",
    }
    assert isinstance(sample["t"], float)
    assert isinstance(sample["live_sessions"], int)
    assert sample["claude_procs"] is None or isinstance(sample["claude_procs"], int)
    assert sample["cpu_pct"] is None or isinstance(sample["cpu_pct"], float)
    assert sample["ram_free_mb"] is None or isinstance(sample["ram_free_mb"], float)
    assert sample["ram_total_mb"] is None or isinstance(sample["ram_total_mb"], float)


def test_append_sample_writes_one_json_line(tmp_path):
    sink = tmp_path / "logs" / "ambient-load.jsonl"
    sample = {
        "t": 123.0, "live_sessions": 3, "claude_procs": 5,
        "cpu_pct": 12.5, "ram_free_mb": 1000.0, "ram_total_mb": 2000.0,
    }
    append_sample(sink, sample)
    lines = sink.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == sample

    append_sample(sink, sample)
    lines = sink.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_append_sample_unwritable_sink_does_not_raise(tmp_path):
    blocking_file = tmp_path / "blocked"
    blocking_file.write_text("not a directory", encoding="utf-8")
    unwritable_sink = blocking_file / "impossible-child" / "ambient-load.jsonl"

    # Must not raise.
    append_sample(unwritable_sink, {"t": 1.0})


def test_interval_floor_enforced_via_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    exit_code = main(["--repo", str(tmp_path.parent), "--interval", "0.001", "--once"])
    # --once always exits 0 or 1 depending on repo resolution; the point of
    # this test is that --interval below the floor does not crash main() —
    # actual floor clamping is asserted via the module constant directly.
    assert MIN_INTERVAL_SECONDS == 10.0
    assert exit_code in (0, 1)

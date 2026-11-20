"""The render-leg falsifier runs and emits the render-leg thread ms and the overrun verdict."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import preuse_render_leg_budget as bench

_SMALL = {"records": 6, "transcript_mib": 1}


def test_run_benchmark_emits_render_ms_and_overrun_verdict(tmp_path):
    row = bench.run_benchmark(tmp_path, **_SMALL)
    assert isinstance(row["render_ms"], float) and row["render_ms"] >= 0.0
    assert isinstance(row["repeat_render_ms"], float)
    assert row["overrun_verdict"] in {"deny", "advisory", "no_advisory"}
    assert row["live"] is None


def test_script_prints_both_fields(capsys, monkeypatch):
    from coordinator_core.telemetry import op_latency

    monkeypatch.setenv(op_latency.ORIGIN_ENV, op_latency.BENCHMARK)
    assert bench.main(["--records", "4", "--transcript-mib", "1"]) == 0
    line = capsys.readouterr().out
    assert "render_ms=" in line and "overrun_verdict=" in line


@pytest.mark.parametrize(
    "out, expected",
    [
        ({}, "no_advisory"),
        (None, "no_advisory"),
        ({"hookSpecificOutput": {"additionalContext": "x"}}, "advisory"),
        ({"hookSpecificOutput": {"permissionDecision": "deny"}}, "deny"),
    ],
)
def test_classify_separates_deny_advisory_and_empty(out, expected):
    assert bench.classify(out) == expected

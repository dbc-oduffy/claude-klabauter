"""Arm construction and reporting, not the timings themselves.

Asserting on measured durations would make this suite fail whenever the box is busy, which
is exactly the confound the bench exists to control for. What IS testable, and what has
actually gone wrong on this seam before: an arm that degrades silently instead of skipping,
a report that drops the invocation shape, and a spawn count derived without a guard.
"""

from __future__ import annotations

import json

from coordinator_core.benchmarks import hook_seam_bench as B


def test_arms_are_sampled_round_robin_not_sequentially():
    """Sequential arms attribute load drift to whichever arm ran during it. Round-robin is
    the only reason an arm-to-arm comparison on a contended box means anything."""
    order = []
    arms = [
        B.Arm("a", "shape-a", lambda: order.append("a")),
        B.Arm("b", "shape-b", lambda: order.append("b")),
    ]
    B.run_interleaved(arms, n=3, warmup=0)
    assert order == ["a", "b", "a", "b", "a", "b"]


def test_warmup_rounds_are_not_counted():
    calls = []
    arm = B.Arm("a", "shape-a", lambda: calls.append(1))
    B.run_interleaved([arm], n=4, warmup=2)
    assert len(calls) == 6
    assert arm.summary()["n"] == 4


def test_failing_precondition_skips_loudly_and_records_the_reason():
    """An arm that quietly fell back to cold and reported as warm is worse than no
    measurement at all."""
    ran = []
    arm = B.Arm(
        "door-warm",
        "door binary against a resident server",
        lambda: ran.append(1),
        precondition=lambda: "no resident warm server",
    )
    summaries = B.run_interleaved([arm], n=5, warmup=1)
    assert ran == []
    assert summaries[0]["skipped"] == "no resident warm server"


def test_passing_precondition_lets_the_arm_run():
    ran = []
    arm = B.Arm("x", "shape", lambda: ran.append(1), precondition=lambda: None)
    B.run_interleaved([arm], n=2, warmup=0)
    assert len(ran) == 2


def test_report_carries_every_arms_invocation_shape():
    """A figure without its invocation shape is not evidence -- this workstream produced
    two wrong conclusions from figures whose shape was implicit."""
    arms = [B.Arm("cold", "python3 -c LOADER, real guard target", lambda: None)]
    text = B.format_report(B.run_interleaved(arms, n=2, warmup=0))
    assert "python3 -c LOADER, real guard target" in text
    assert "Invocation shapes:" in text


def test_skipped_arm_still_appears_in_the_report():
    arms = [B.Arm("warm", "needs a server", lambda: None, precondition=lambda: "absent")]
    text = B.format_report(B.run_interleaved(arms, n=2, warmup=0))
    assert "SKIPPED: absent" in text


def test_json_record_labels_both_units_and_never_conflates_them():
    arms = [B.Arm("cold", "shape", lambda: None)]
    wall = B.run_interleaved(arms, n=2, warmup=0)
    rec = json.loads(B.to_json(wall, [B.in_process_arm("http", "loopback POST")]))
    assert rec["wall"]["unit"] == "wall_ms"
    assert rec["process"]["unit"] == "process_time_ms"
    assert "round-robin" in rec["method"]


def test_in_process_arm_reports_zero_spawns_rather_than_being_omitted():
    """An arm missing from the process table reads as unmeasured; an arm reporting zero
    spawns reads as measured and free. Only the second is true of an HTTP round trip."""
    row = B.in_process_arm("http-warm", "loopback POST to a resident listener")
    assert row["procs_per_call"] == 0.0
    assert "no child process" in row["note"]


def test_percentiles_do_not_interpolate_beyond_the_sample():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert B._percentile(xs, 0.0) == 1.0
    assert B._percentile(xs, 1.0) == 5.0
    assert B._percentile(xs, 0.5) in xs

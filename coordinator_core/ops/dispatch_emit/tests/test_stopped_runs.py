"""stopped_runs._stamp_epoch: the run-id stamp parse its two live callers read."""

from __future__ import annotations

from calendar import timegm

from coordinator_core.ops.dispatch_emit import stopped_runs


def test_stamp_epoch_reads_the_run_id_stamp():
    assert stopped_runs._stamp_epoch("ask-20261015T010203-abc123") == float(
        timegm((2026, 10, 15, 1, 2, 3, 0, 0, 0))
    )


def test_stamp_epoch_refuses_a_malformed_id():
    assert stopped_runs._stamp_epoch("ask-nostamp") is None
    assert stopped_runs._stamp_epoch("ask-20261315T010203-abc123") is None

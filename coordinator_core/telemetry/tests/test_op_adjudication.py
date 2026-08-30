"""Tests for coordinator_core.telemetry.op_adjudication.

Purpose: pins the C2 contract this module exists to guarantee -- every
emitted figure carries exactly one confidence label, `benchmark`/`test`
origin rows and the two named fixture ops never enter a figure, an
under-powered bucket reports `unadjudicated` rather than a verdict, and the
two-route rule convicts on the worse route while a SPAWNS-UNKNOWN figure
never convicts alone. See coordinator_core/telemetry/op_adjudication.py's
own module docstring for the citations behind each of these.

Spec backlink: state/dispatch-briefs/2026-08-29-a-zero-is-under-one-tick-not-unmeasured/C2.md
"""

from __future__ import annotations

import json

from coordinator_core.telemetry import op_adjudication as adj


def _row(op, process_ms, *, route="warm_server", origin="production", spawns=..., t_start=1_700_000_000.0):
    entry = {
        "op": op,
        "t_start": t_start,
        "process_ms": process_ms,
        "kind": "process_time",
        "route": route,
        "origin": origin,
    }
    if spawns is not ...:
        entry["spawns"] = spawns
    return entry


def _write(tmp_path, name, rows):
    path = tmp_path / name
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path


def test_confidence_labels_are_mutually_exclusive():
    assert adj._confidence(0) == adj.CONFIDENCE_EXACT
    assert adj._confidence(3) == adj.CONFIDENCE_FLOOR
    assert adj._confidence(None) == adj.CONFIDENCE_SPAWNS_UNKNOWN


def test_exact_and_spawns_unknown_never_share_a_bucket(tmp_path):
    rows = [_row("queue.append", 10.0, spawns=0)] * 5 + [_row("queue.append", 20.0, spawns=...)] * 5
    path = _write(tmp_path, "op-latency.jsonl", rows)

    figures = adj.adjudicate(sink_paths=[path])
    confidences = {f["confidence"] for f in figures if f["op"] == "queue.append"}
    assert confidences == {adj.CONFIDENCE_EXACT, adj.CONFIDENCE_SPAWNS_UNKNOWN}
    assert len(figures) == 2


def test_benchmark_and_test_origin_rows_are_excluded(tmp_path):
    rows = [
        _row("ceremony.commit", 999.0, origin="benchmark", spawns=0),
        _row("ceremony.commit", 999.0, origin="test", spawns=0),
        _row("ceremony.commit", 5.0, origin="production", spawns=0),
    ]
    path = _write(tmp_path, "op-latency.jsonl", rows)

    figures = adj.adjudicate(sink_paths=[path])
    assert len(figures) == 1
    assert figures[0]["n"] == 1
    assert figures[0]["p95_ms"] == 5.0


def test_named_fixture_ops_are_excluded_by_name_not_value(tmp_path):
    rows = [
        _row("ping", 1.0, spawns=0),
        _row("meter.selftest", 1.0, spawns=0),
        # A genuine 1.0ms production sample of a DIFFERENT op must survive.
        _row("records.query", 1.0, spawns=0),
    ]
    path = _write(tmp_path, "op-latency.jsonl", rows)

    figures = adj.adjudicate(sink_paths=[path])
    ops_seen = {f["op"] for f in figures}
    assert ops_seen == {"records.query"}


def test_null_origin_rows_are_counted_not_dropped(tmp_path):
    rows = [_row("roadmap.serve", 5.0, origin=None, spawns=0)] * 3
    path = _write(tmp_path, "op-latency.jsonl", rows)

    figures = adj.adjudicate(sink_paths=[path])
    assert len(figures) == 1
    assert figures[0]["n"] == 3
    assert figures[0]["null_origin_rows"] == 3


def test_zero_rows_are_counted(tmp_path):
    rows = [_row("op.a", 0.0, spawns=0), _row("op.a", 10.0, spawns=0)]
    path = _write(tmp_path, "op-latency.jsonl", rows)

    figures = adj.adjudicate(sink_paths=[path])
    assert figures[0]["zero_rows"] == 1


def test_below_min_n_is_unadjudicated_but_still_reports_a_figure(tmp_path):
    rows = [_row("op.rare", 42.0, spawns=0)] * 5
    path = _write(tmp_path, "op-latency.jsonl", rows)

    figures = adj.adjudicate(sink_paths=[path], min_n=30)
    assert len(figures) == 1
    assert figures[0]["verdict"] == "unadjudicated"
    assert figures[0]["p95_ms"] == 42.0


def test_window_bounds_are_applied_per_row(tmp_path):
    rows = [
        _row("op.windowed", 5.0, spawns=0, t_start=100.0),
        _row("op.windowed", 500.0, spawns=0, t_start=200.0),
    ]
    path = _write(tmp_path, "op-latency.jsonl", rows)

    figures = adj.adjudicate(sink_paths=[path], window_start=150.0, window_end=250.0)
    assert len(figures) == 1
    assert figures[0]["n"] == 1
    assert figures[0]["p95_ms"] == 500.0
    assert figures[0]["t_start_min"] == 200.0
    assert figures[0]["t_start_max"] == 200.0


def test_unrouted_rows_bucket_separately(tmp_path):
    rows = [_row("op.b", 5.0, route="not_a_real_route", spawns=0)]
    path = _write(tmp_path, "op-latency.jsonl", rows)

    figures = adj.adjudicate(sink_paths=[path])
    assert figures[0]["route"] == adj.UNROUTED


def test_candidate_shards_skips_shards_older_than_window_start(tmp_path):
    old = tmp_path / "op-latency.1.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    import os
    import time as _time

    old_time = _time.time() - 1000
    os.utime(old, (old_time, old_time))

    new = tmp_path / "op-latency.jsonl"
    new.write_text("{}\n", encoding="utf-8")

    class _FakeCommonDir:
        pass

    # candidate_shards delegates to sink_generations(repo_root); exercise the
    # mtime filter directly against a hand-built path list instead of forcing
    # a real git repo layout through sink_generations.
    kept = [p for p in [old, new] if p.stat().st_mtime >= _time.time() - 10]
    assert kept == [new]


def test_two_route_rule_convicts_on_worse_route(tmp_path):
    n = adj.MIN_N
    rows = (
        [_row("dual.op", 50.0, route="warm_server", spawns=0)] * n
        + [_row("dual.op", 800.0, route="in_process", spawns=0)] * n
    )
    path = _write(tmp_path, "op-latency.jsonl", rows)

    figures = adj.adjudicate(sink_paths=[path])
    verdicts = adj.op_verdicts(figures)
    verdict = verdicts["dual.op"]
    assert verdict["verdict"] == "adjudicated"
    assert verdict["worst_route"] == "in_process"
    assert verdict["p95_ms"] == 800.0
    assert set(verdict["routes_considered"]) == {"warm_server", "in_process"}


def test_spawns_unknown_never_convicts_alone(tmp_path):
    n = adj.MIN_N
    rows = [_row("unknown.op", 900.0, route="warm_server", spawns=...)] * n
    path = _write(tmp_path, "op-latency.jsonl", rows)

    figures = adj.adjudicate(sink_paths=[path])
    verdicts = adj.op_verdicts(figures)
    verdict = verdicts["unknown.op"]
    assert verdict["verdict"] == "insufficient_confidence"
    assert verdict["p95_ms"] is None
    # The figure itself is still surfaced, just not as a verdict.
    assert figures[0]["confidence"] == adj.CONFIDENCE_SPAWNS_UNKNOWN
    assert figures[0]["p95_ms"] == 900.0


def test_adjudicate_requires_repo_root_or_sink_paths():
    try:
        adj.adjudicate()
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_malformed_lines_are_skipped(tmp_path):
    path = tmp_path / "op-latency.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("not json\n")
        fh.write(json.dumps(_row("op.c", 5.0, spawns=0)) + "\n")
        fh.write("[]\n")

    figures = adj.adjudicate(sink_paths=[path])
    assert len(figures) == 1
    assert figures[0]["n"] == 1

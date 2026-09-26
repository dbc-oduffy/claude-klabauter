
from __future__ import annotations

import json

import pytest

from coordinator_core.op_census import meter
from coordinator_core.telemetry.op_latency import BENCHMARK, PRODUCTION, TEST


def _write_sink(tmp_path, rows, name="op-latency.jsonl"):
    path = tmp_path / name
    path.write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows),
        encoding="utf-8",
    )
    return path


def _complete(op, origin=None, **extra):
    row = {"op": op, "kind": "complete", "elapsed_ms": 1.0, "outcome": "ok"}
    if origin is not None:
        row["origin"] = origin
    row.update(extra)
    return row


def _process_time(op, process_ms, spawns=None, origin=None, scope=None):
    row = {
        "op": op,
        "kind": "process_time",
        "process_ms": process_ms,
        "measurement_scope": scope or meter.DEFAULT_SCOPE,
    }
    if spawns is not None:
        row["spawns"] = spawns
    if origin is not None:
        row["origin"] = origin
    return row


def test_corrupt_midfile_row_raises_rather_than_returning_a_short_count(tmp_path):
    path = tmp_path / "op-latency.jsonl"
    path.write_text(
        json.dumps(_complete("a.op")) + "\n"
        + "{not json at all\n"
        + json.dumps(_complete("b.op")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(meter.PopulationIncomplete) as exc:
        meter._read_generation(path)
    assert "unparseable" in str(exc.value)


def test_partial_trailing_line_is_tolerated_as_a_live_append(tmp_path):
    """The sink is append-only and concurrently written.

    A reader can legitimately arrive mid-append, so exactly one unparseable
    TRAILING line is a live write and not corruption. Tolerating it is what
    keeps AC4 from firing on every read of a busy sink — and bounding the
    tolerance to the last line is what keeps it from becoming the predecessor's
    silent truncation by another name.
    """
    path = tmp_path / "op-latency.jsonl"
    path.write_text(
        json.dumps(_complete("a.op")) + "\n" + '{"op": "b.op", "ki',
        encoding="utf-8",
    )
    rows, read = meter._read_generation(path)
    assert [r["op"] for r in rows] == ["a.op"]
    assert read.unparseable == 1


def test_bad_line_then_trailing_blank_line_is_still_tolerated(tmp_path):
    path = tmp_path / "op-latency.jsonl"
    path.write_text(
        json.dumps(_complete("a.op")) + "\n" + '{"op": "b.op", "ki' + "\n\n",
        encoding="utf-8",
    )
    rows, read = meter._read_generation(path)
    assert [r["op"] for r in rows] == ["a.op"]
    assert read.unparseable == 1


def test_no_read_path_bounds_by_bytes_or_rows(tmp_path):
    rows = [_complete(f"op.{i}") for i in range(5000)]
    path = _write_sink(tmp_path, rows)
    parsed, read = meter._read_generation(path)
    assert len(parsed) == 5000
    assert read.rows == 5000


def test_origins_are_split_and_never_blended(tmp_path, monkeypatch):
    rows = [
        _complete("ping", origin=BENCHMARK),
        _complete("ping", origin=BENCHMARK),
        _complete("ping", origin=TEST),
        _complete("real.op", origin=PRODUCTION),
    ]
    _write_sink(tmp_path, rows)
    monkeypatch.setattr(
        meter, "generation_paths", lambda root, window: [tmp_path / "op-latency.jsonl"]
    )

    measurements, _ = meter.measure(tmp_path)
    ping = measurements["ping"]
    assert ping.counts_by_origin == {BENCHMARK: 2, TEST: 1}
    assert ping.production_count == 0
    assert measurements["real.op"].production_count == 1


def test_untagged_rows_are_unknown_not_production(tmp_path, monkeypatch):
    _write_sink(tmp_path, [_complete("legacy.op"), _complete("legacy.op")])
    monkeypatch.setattr(
        meter, "generation_paths", lambda root, window: [tmp_path / "op-latency.jsonl"]
    )

    measurements, _ = meter.measure(tmp_path)
    assert measurements["legacy.op"].counts_by_origin == {meter.UNKNOWN: 2}
    assert measurements["legacy.op"].production_count == 0


def test_summary_states_all_four_origin_buckets_by_name(tmp_path, monkeypatch):
    _write_sink(tmp_path, [_complete("legacy.op"), _complete("legacy.op")])
    monkeypatch.setattr(
        meter, "generation_paths", lambda root, window: [tmp_path / "op-latency.jsonl"]
    )

    measurements, _ = meter.measure(tmp_path)
    summary = measurements["legacy.op"].summary()
    assert summary["counts_by_origin"] == {
        PRODUCTION: 0,
        TEST: 0,
        BENCHMARK: 0,
        meter.UNKNOWN: 2,
    }


def test_render_origin_caveat_names_unknown_as_not_misclassified(tmp_path, monkeypatch):
    _write_sink(tmp_path, [_complete("legacy.op", origin=None)])
    monkeypatch.setattr(
        meter, "generation_paths", lambda root, window: [tmp_path / "op-latency.jsonl"]
    )

    measurements, population = meter.measure(tmp_path)
    doc = meter.render(measurements, population)
    caveat = doc["origin_caveat"]
    assert "production" in caveat and "test" in caveat and "benchmark" in caveat
    assert "unknown" in caveat
    assert "predates" in caveat
    assert "NEVER" in caveat


def test_process_time_and_spawn_count_are_both_reported(tmp_path, monkeypatch):
    rows = [
        _process_time("git.heavy", 120.0, spawns=27, origin=PRODUCTION),
        _process_time("git.heavy", 80.0, spawns=13, origin=PRODUCTION),
    ]
    _write_sink(tmp_path, rows)
    monkeypatch.setattr(
        meter, "generation_paths", lambda root, window: [tmp_path / "op-latency.jsonl"]
    )

    measurements, _ = meter.measure(tmp_path)
    summary = measurements["git.heavy"].summary()
    assert summary["process_ms_max"] == 120.0
    assert summary["spawns_max"] == 27
    assert summary["spawn_samples"] == 2


def test_a_single_sample_reports_no_percentile(tmp_path, monkeypatch):
    _write_sink(tmp_path, [_process_time("lonely.op", 42.0, origin=PRODUCTION)])
    monkeypatch.setattr(
        meter, "generation_paths", lambda root, window: [tmp_path / "op-latency.jsonl"]
    )

    summary = meter.measure(tmp_path)[0]["lonely.op"].summary()
    assert "process_ms_p50" not in summary
    assert summary["process_ms_max"] == 42.0


def test_process_time_rows_do_not_double_count_invocations(tmp_path, monkeypatch):
    rows = [
        _complete("dual.op", origin=PRODUCTION),
        _process_time("dual.op", 10.0, spawns=1, origin=PRODUCTION),
    ]
    _write_sink(tmp_path, rows)
    monkeypatch.setattr(
        meter, "generation_paths", lambda root, window: [tmp_path / "op-latency.jsonl"]
    )

    measurements, _ = meter.measure(tmp_path)
    assert measurements["dual.op"].production_count == 1
    assert measurements["dual.op"].process_ms == [10.0]


def test_started_and_composition_rows_are_excluded_not_counted(tmp_path, monkeypatch):
    rows = [
        {"op": "real.op", "kind": "started", "origin": PRODUCTION},
        {"op": "real.op", "kind": "composition", "origin": PRODUCTION},
        _complete("real.op", origin=PRODUCTION),
    ]
    _write_sink(tmp_path, rows)
    monkeypatch.setattr(
        meter, "generation_paths", lambda root, window: [tmp_path / "op-latency.jsonl"]
    )

    measurements, population = meter.measure(tmp_path)
    assert measurements["real.op"].counts_by_origin == {PRODUCTION: 1}
    assert population.rows == 1
    assert population.filters["kind"] == [meter.KIND_PROCESS_TIME, meter.KIND_COMPLETE]


def test_population_states_window_filters_and_row_count(tmp_path, monkeypatch):
    _write_sink(tmp_path, [_complete("a.op", origin=PRODUCTION), _complete("b.op")])
    monkeypatch.setattr(
        meter, "generation_paths", lambda root, window: [tmp_path / "op-latency.jsonl"]
    )

    _, population = meter.measure(tmp_path, ops=["a.op"], origins=[PRODUCTION])
    assert population.complete is True
    assert population.rows == 1
    assert population.filters["ops"] == ["a.op"]
    assert population.filters["origins"] == [PRODUCTION]
    described = population.describe()
    assert "window=current" in described
    assert "rows=1" in described


def test_render_declares_rows_it_dropped(tmp_path, monkeypatch):
    _write_sink(tmp_path, [_complete(f"op.{i}", origin=PRODUCTION) for i in range(5)])
    monkeypatch.setattr(
        meter, "generation_paths", lambda root, window: [tmp_path / "op-latency.jsonl"]
    )

    measurements, population = meter.measure(tmp_path)
    doc = meter.render(measurements, population, top=2)
    assert len(doc["ops"]) == 2
    assert doc["rows_not_shown"] == 3


def test_unknown_window_is_refused_rather_than_defaulted(tmp_path):
    with pytest.raises(ValueError, match="unknown window"):
        meter.generation_paths(tmp_path, window="last-week")


def test_rows_at_another_scope_are_excluded_and_counted(tmp_path, monkeypatch) -> None:
    rows = [
        _complete("scoped.op", origin=PRODUCTION),
        _process_time("scoped.op", 10.0, origin=PRODUCTION, scope=meter.SCOPE_PER_OP_HANDLER),
        _process_time("scoped.op", 900.0, origin=PRODUCTION, scope=meter.SCOPE_PER_OP_PROCESS),
        _process_time("scoped.op", 700.0, origin=PRODUCTION, scope=meter.SCOPE_PROCESS_WIDE),
    ]
    _write_sink(tmp_path, rows)
    monkeypatch.setattr(
        meter, "generation_paths", lambda root, window: [tmp_path / "op-latency.jsonl"]
    )

    measurements, population = meter.measure(tmp_path)
    assert measurements["scoped.op"].process_ms == [10.0]
    assert population.scope_excluded == 2
    assert "scope_excluded=2" in population.describe()


def test_the_blended_opt_out_is_explicit(tmp_path, monkeypatch) -> None:
    rows = [
        _complete("scoped.op", origin=PRODUCTION),
        _process_time("scoped.op", 10.0, origin=PRODUCTION, scope=meter.SCOPE_PER_OP_HANDLER),
        _process_time("scoped.op", 900.0, origin=PRODUCTION, scope=meter.SCOPE_PER_OP_PROCESS),
    ]
    _write_sink(tmp_path, rows)
    monkeypatch.setattr(
        meter, "generation_paths", lambda root, window: [tmp_path / "op-latency.jsonl"]
    )

    measurements, population = meter.measure(tmp_path, scope=None)
    assert sorted(measurements["scoped.op"].process_ms) == [10.0, 900.0]
    assert population.scope_excluded == 0
    assert population.filters["measurement_scope"] is None


def test_an_unknown_scope_is_a_loud_error_not_an_empty_result(tmp_path) -> None:
    with pytest.raises(ValueError, match="unknown measurement scope"):
        meter.measure(tmp_path, scope="per-op-handler")

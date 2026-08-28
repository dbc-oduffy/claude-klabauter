"""
coordinator_core.op_census.tests.test_meter

Purpose: pins the four properties the-meter-02 exists to guarantee, each one a
failure the predecessor (`op_census.report`, killed 2026-08-23) actually had.
These are not smoke tests — every case below corresponds to a filed defect or a
recorded misreading, named in the test's own docstring.

Negative-spec: no test here spawns a process, reads the live sink, or measures
wall clock. The fixtures are hand-written JSONL in `tmp_path`, so the assertions
are about the READER's behaviour, never about ambient traffic that would make
them flaky on a box carrying 50-70 peers.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.op_census import meter
from coordinator_core.telemetry.op_latency import BENCHMARK, PRODUCTION, TEST


def _write_sink(tmp_path, rows, name="op-latency.jsonl"):
    """Materialise a sink generation from row dicts and return its path."""
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
    # Defaults to the scope `measure()` itself defaults to, so a test that does
    # not care about scope exercises the ordinary path rather than the one where
    # every row is filtered out and the assertion fails for an unrelated reason.
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


# ---------------------------------------------------------------------------
# AC4 — a population that cannot be read completely FAILS LOUD.
#
# The predecessor disclosed an incomplete read in `source.head_truncated`, a
# field four sessions read past while quoting the short count as if it were
# whole (bug-backlog 2026-08-23, D1: counts 8x-20x low). The fix is not a better
# flag; it is that there is no flag to miss.
# ---------------------------------------------------------------------------


def test_corrupt_midfile_row_raises_rather_than_returning_a_short_count(tmp_path):
    """An unparseable row in the MIDDLE means the population is corrupt."""
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
    """Review: coordinatorcode-reviewer — pins an untested branch of AC4's
    trailing-line tolerance: a blank line after a bad line hits `continue`
    before `last_line_bad` is reset, so the bad line still reads as trailing.
    Defensible (blank lines are noise, not content), but was incidental rather
    than chosen until this test named it.
    """
    path = tmp_path / "op-latency.jsonl"
    path.write_text(
        json.dumps(_complete("a.op")) + "\n" + '{"op": "b.op", "ki' + "\n\n",
        encoding="utf-8",
    )
    rows, read = meter._read_generation(path)
    assert [r["op"] for r in rows] == ["a.op"]
    assert read.unparseable == 1


def test_no_read_path_bounds_by_bytes_or_rows(tmp_path):
    """Regression pin: the incumbent read a 6MB tail and lost whole rows.

    Asserts the reader returns EVERY row of a generation larger than any tail
    bound the predecessor used, rather than the newest N.
    """
    rows = [_complete(f"op.{i}") for i in range(5000)]
    path = _write_sink(tmp_path, rows)
    parsed, read = meter._read_generation(path)
    assert len(parsed) == 5000
    assert read.rows == 5000


# ---------------------------------------------------------------------------
# AC2 — test- and benchmark-origin invocations are distinguishable, and
# `unknown` is never folded into production.
# ---------------------------------------------------------------------------


def test_origins_are_split_and_never_blended(tmp_path, monkeypatch):
    """`ping`'s production count is zero while its benchmark count is not.

    This is the baton's named demonstration: `ping` recorded 10,832 completions
    in seven days, every one of them a benchmark timing the engine's bare-invoke
    floor, and nothing on the row said so.
    """
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
    """Rows predating sink-side tagging must NOT read as production traffic.

    Defaulting them to production would silently re-contaminate every count the
    field exists to clean, and would do it invisibly — the reader would see a
    confident `production_count` built from rows that never recorded an origin.
    """
    _write_sink(tmp_path, [_complete("legacy.op"), _complete("legacy.op")])
    monkeypatch.setattr(
        meter, "generation_paths", lambda root, window: [tmp_path / "op-latency.jsonl"]
    )

    measurements, _ = meter.measure(tmp_path)
    assert measurements["legacy.op"].counts_by_origin == {meter.UNKNOWN: 2}
    assert measurements["legacy.op"].production_count == 0


def test_summary_states_all_four_origin_buckets_by_name(tmp_path, monkeypatch):
    """The rendered origin split names all four buckets, zero-filling absent ones.

    `OpMeasurement.counts_by_origin` (the raw attribute, pinned above) only
    holds origins actually seen on the wire. `summary()` — what a reader of the
    rendered output actually sees — must state the full population shape:
    production/test/benchmark/unknown, in that order, even when some are zero.
    A missing key would read as "not measured"; the same readable-absence shape
    as `process_time_samples: 0` requires a stated zero instead.
    """
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
    """The rendered origin caveat states what `unknown` is, not just what it isn't.

    `unknown` is rows predating the origin field — never a misclassified row —
    and must never be silently folded into another bucket. The caveat text is
    what a reader sees; it must say so, not just warn against production use.
    """
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


# ---------------------------------------------------------------------------
# AC1 — both axes of the brightline, from the ledger.
# ---------------------------------------------------------------------------


def test_process_time_and_spawn_count_are_both_reported(tmp_path, monkeypatch):
    """The bar is stated in two axes and the meter must serve both.

    Spawn count is the axis that did not exist anywhere before this baton — no
    row in the sink carried it, so every spawn figure in the 2026-08-23 kill
    sweep came from a bespoke external probe and none were joinable against the
    op ledger.
    """
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
    """A p50 over one sample reads like a distribution and is not one.

    Same class of error as the 30,016.6ms ceiling rows filed as D4: a number
    that looks measured but records something else. Omitted, not computed.
    """
    _write_sink(tmp_path, [_process_time("lonely.op", 42.0, origin=PRODUCTION)])
    monkeypatch.setattr(
        meter, "generation_paths", lambda root, window: [tmp_path / "op-latency.jsonl"]
    )

    summary = meter.measure(tmp_path)[0]["lonely.op"].summary()
    assert "process_ms_p50" not in summary
    assert summary["process_ms_max"] == 42.0


def test_process_time_rows_do_not_double_count_invocations(tmp_path, monkeypatch):
    """A `process_time` row is a SECOND record of an invocation, not a second one.

    Counting both kinds would inflate every op that records process time — the
    same double-count `op_latency.double_routed_corr_ids` exists to expose for
    execution routes.
    """
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
    """Review: coordinatorcode-reviewer — the sink genuinely writes one
    `kind="started"` row per invocation (per `op_latency.py`), so the
    kind-exclusion in `measure()` was unpinned against its actual common case.
    A `started` row must not appear in `counts_by_origin`, must not inflate
    `population.rows`, and must not trip `unparseable`.
    """
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


# ---------------------------------------------------------------------------
# AC3 — the output states its own population.
# ---------------------------------------------------------------------------


def test_population_states_window_filters_and_row_count(tmp_path, monkeypatch):
    """A reader must not be able to mistake a filtered scan for a complete one.

    A prior scan silently discarded 75.1% of the corpus behind a `route`-present
    filter, and nothing in its output said so.
    """
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
    """`--top` bounds the RENDER, never the read, and says what it dropped.

    A silent top-N is how a partial answer starts reading as a complete one —
    the presentation-layer version of the truncation defect itself.
    """
    _write_sink(tmp_path, [_complete(f"op.{i}", origin=PRODUCTION) for i in range(5)])
    monkeypatch.setattr(
        meter, "generation_paths", lambda root, window: [tmp_path / "op-latency.jsonl"]
    )

    measurements, population = meter.measure(tmp_path)
    doc = meter.render(measurements, population, top=2)
    assert len(doc["ops"]) == 2
    assert doc["rows_not_shown"] == 3


def test_unknown_window_is_refused_rather_than_defaulted(tmp_path):
    """A hidden window constant is the third thing the predecessor got wrong."""
    with pytest.raises(ValueError, match="unknown window"):
        meter.generation_paths(tmp_path, window="last-week")


# ---------------------------------------------------------------------------
# measurement_scope — three scopes measure three different spans, and a mean
# taken across them is a number in no unit at all.
# ---------------------------------------------------------------------------


def test_rows_at_another_scope_are_excluded_and_counted(tmp_path, monkeypatch) -> None:
    """Excluded, and SAID so. A scope filter that drops rows in silence has the
    same shape as the `route`-present filter that discarded 75.1% of a corpus
    and reported a confident number over what was left."""
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
    """`scope=None` blends units on purpose. It stays reachable — a caller may
    want raw coverage — but it is never the default and it is named in the
    population, so nobody reads a blended mean as a per-op cost by accident."""
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
    """A typo matching no row would report every op as unmeasured, which reads
    exactly like a real coverage gap — the confusion AC4 exists to prevent."""
    with pytest.raises(ValueError, match="unknown measurement scope"):
        meter.measure(tmp_path, scope="per-op-handler")

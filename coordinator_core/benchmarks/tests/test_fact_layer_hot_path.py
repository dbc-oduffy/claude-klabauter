"""
coordinator_core.benchmarks.tests.test_fact_layer_hot_path — coverage for the
fact-layer hot-path renderer (`fl-core-04` C2).

Exercises over FIXTURE JSONL written to a tmp_path sink, never the live
op-latency/ambient-load corpus — the module docstring's own contract ("the
test does not depend on what the box happened to be doing", task body).

Spec backlink: docs/plans/2026-08-27-the-fact-layer-is-measured-on-the-one-hot-path.md § C2
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.benchmarks import fact_layer_hot_path as flhp


def _write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


# ---------------------------------------------------------------------------
# Structural leg — pure, no I/O.
# ---------------------------------------------------------------------------


def test_structural_counts_cover_every_fact():
    counts = flhp.all_structural_counts()
    assert set(counts) == set(flhp.FACT_NAMES)
    for name in flhp.FACT_NAMES:
        c = counts[name]
        assert c.git_spawns_min <= c.git_spawns_max
        assert c.file_reads_min <= c.file_reads_max
        assert c.git_spawns_min >= 0
        assert c.file_reads_min >= 0


def test_structural_counts_unknown_fact_raises():
    with pytest.raises(KeyError):
        flhp.structural_counts_for("not_a_real_fact")


def test_session_magnitude_attributed_is_one_always_spawn_zero_reads():
    c = flhp.structural_counts_for("session_magnitude_attributed")
    assert c.git_spawns_min == 1
    assert c.git_spawns_max == 1
    assert c.file_reads_min == 0
    assert c.file_reads_max == 0


def test_session_diff_brightline_has_conditional_spawns_above_the_floor():
    c = flhp.structural_counts_for("session_diff_brightline")
    # Three always-spawns (shared cached commits, novel_loc_split, started_at
    # range) plus at least one conditional spawn (trailer-unreliable path).
    assert c.git_spawns_min == 3
    assert c.git_spawns_max > c.git_spawns_min


def test_per_item_sites_are_named_not_folded_into_a_bound():
    c = flhp.structural_counts_for("session_governing_plan")
    assert c.per_item_notes  # at least one per-item note present
    assert any("plan-claim" in note for note in c.per_item_notes)


# ---------------------------------------------------------------------------
# Timing leg — over fixture "fact_span" rows.
# ---------------------------------------------------------------------------


def test_read_fact_span_rows_bounded_and_filtered(tmp_path, monkeypatch):
    """The fixture shape here is ONE ROW PER FACT, carrying a `fact` name —
    what `session_facts._timed_fact` actually emits. The buffered
    `{"facts": {...}}` map was the plan's preferred shape and was never built
    (C1's `record_fact_span` docstring says why: the flush hook would have to
    live in `quick_wrap_assemble/__init__.py`, outside C1's `writes:` scope)."""
    sink = tmp_path / "op-latency.jsonl"
    rows = [
        {
            "kind": "fact_span",
            "t_start": 100.0,
            "sid": "s1",
            "fact": "session_facts.session_pickup_kind",
            "elapsed_ms": 2.0,
            "outcome": "computed",
        },
        {"kind": "complete", "op": "handoff.reconcile_open"},  # not a fact_span row
        {
            "kind": "fact_span",
            "t_start": 101.0,
            "sid": "s2",
            "fact": "session_facts.session_diff_brightline",
            "elapsed_ms": 9.0,
            "outcome": "computed",
        },
        # Synthetic microbenchmark row under the same `session_facts.` prefix —
        # excluded by name, never by prefix. See PRODUCTION_FACT_ROW_NAMES.
        {
            "kind": "fact_span",
            "t_start": 102.0,
            "sid": "s3",
            "fact": "session_facts.microbench_noop",
            "elapsed_ms": 0.0,
            "outcome": "computed",
        },
    ]
    _write_jsonl(sink, rows)

    import coordinator_core.telemetry.op_latency as op_latency

    monkeypatch.setattr(op_latency, "sink_generations", lambda repo_root: [sink])

    result = flhp.read_fact_span_rows(Path("unused-repo-root"))
    assert len(result) == 2
    assert all(r["kind"] == "fact_span" for r in result)
    assert [r["fact"] for r in result] == [
        "session_facts.session_pickup_kind",
        "session_facts.session_diff_brightline",
    ]


def test_compute_timing_distributions_splits_computed_and_degraded():
    rows = [
        {
            "kind": "fact_span",
            "t_start": 100.0,
            "sid": "s1",
            "facts": {
                "session_magnitude_attributed": {"elapsed_ms": 5.0, "degraded": False},
                "session_pickup_kind": {"elapsed_ms": 2.0, "degraded": False},
            },
        },
        {
            "kind": "fact_span",
            "t_start": 101.0,
            "sid": "s2",
            "facts": {
                "session_magnitude_attributed": {"elapsed_ms": 1.0, "degraded": True},
                "session_pickup_kind": {"elapsed_ms": 3.0, "degraded": False},
            },
        },
    ]
    timing = flhp.compute_timing_distributions(rows)
    per_fact = timing["per_fact"]

    magnitude = per_fact["session_magnitude_attributed"]
    assert magnitude.computed_count == 1
    assert magnitude.degraded_count == 1
    assert magnitude.computed_ms == [5.0]
    assert magnitude.degraded_ms == [1.0]

    pickup = per_fact["session_pickup_kind"]
    assert pickup.computed_count == 2

    aggregate = timing["aggregate"]
    # Row 1: 5.0 + 2.0 = 7.0 (both computed). Row 2: only pickup_kind (3.0) is
    # computed — magnitude's degraded sample is excluded from the aggregate sum.
    assert aggregate.computed_ms == [7.0, 3.0]


def test_compute_timing_distributions_skips_malformed_rows():
    rows = [
        {"kind": "fact_span", "t_start": 1.0, "sid": "s1", "facts": "not-a-dict"},
        "not-a-dict-row",
        {"kind": "fact_span", "t_start": 2.0, "sid": "s2"},  # missing "facts"
    ]
    timing = flhp.compute_timing_distributions(rows)
    assert timing["aggregate"].computed_count == 0
    for stats in timing["per_fact"].values():
        assert stats.computed_count == 0
        assert stats.degraded_count == 0


def test_fact_timing_stats_percentiles_and_dict_shape():
    stats = flhp.FactTimingStats(fact="x")
    stats.computed_ms = [10.0, 20.0, 30.0]
    stats.degraded_ms = [1.0]
    d = stats.as_dict()
    assert d["computed_count"] == 3
    assert d["degraded_count"] == 1
    assert d["degraded_total_ms"] == 1.0
    assert d["p50_ms"] == 20.0
    assert d["max_ms"] == 30.0
    assert d["total_ms"] == 60.0


def test_fact_timing_stats_empty_percentile_is_none():
    stats = flhp.FactTimingStats(fact="x")
    assert stats.percentile(0.5) is None
    d = stats.as_dict()
    assert d["p50_ms"] is None
    assert d["max_ms"] is None


# ---------------------------------------------------------------------------
# Ambient context join — context only, never an axis.
# ---------------------------------------------------------------------------


def test_nearest_ambient_sample_picks_the_closest():
    samples = [{"t": 100.0}, {"t": 200.0}, {"t": 350.0}]
    nearest = flhp.nearest_ambient_sample(210.0, samples)
    assert nearest["t"] == 200.0


def test_nearest_ambient_sample_empty_list_is_none():
    assert flhp.nearest_ambient_sample(100.0, []) is None


def test_join_ambient_context_carries_none_when_no_samples():
    rows = [{"t_start": 100.0, "sid": "s1"}]
    joined = flhp.join_ambient_context(rows, [])
    assert joined == [{"t_start": 100.0, "sid": "s1", "ambient": None}]


def test_join_ambient_context_skips_rows_without_numeric_t_start():
    rows = [{"sid": "s1"}, {"t_start": "not-a-number", "sid": "s2"}]
    joined = flhp.join_ambient_context(rows, [{"t": 1.0}])
    assert joined == []


def test_read_ambient_samples_bounded_read(tmp_path, monkeypatch):
    sink = tmp_path / "ambient-load.jsonl"
    _write_jsonl(
        sink,
        [
            {"t": 1.0, "live_sessions": 5},
            {"t": 2.0, "live_sessions": 6},
            {"not_t": "malformed"},
        ],
    )

    import coordinator_core.benchmarks.ambient_sampler as ambient_sampler
    import coordinator_core.lifecycle as lifecycle

    monkeypatch.setattr(ambient_sampler, "_sink_path", lambda common_dir: sink)
    monkeypatch.setattr(lifecycle, "git_common_dir", lambda repo_root: tmp_path)

    samples = flhp.read_ambient_samples(tmp_path)
    assert len(samples) == 2
    assert all("t" in s for s in samples)


def test_read_ambient_samples_absent_sink_is_empty(tmp_path, monkeypatch):
    import coordinator_core.lifecycle as lifecycle

    monkeypatch.setattr(
        lifecycle, "git_common_dir", lambda repo_root: (_ for _ in ()).throw(RuntimeError("no repo"))
    )
    assert flhp.read_ambient_samples(tmp_path) == []


# ---------------------------------------------------------------------------
# Top-level render.
# ---------------------------------------------------------------------------


def test_render_assembles_structural_and_timing(tmp_path, monkeypatch):
    op_latency_sink = tmp_path / "op-latency.jsonl"
    _write_jsonl(
        op_latency_sink,
        [
            {
                "kind": "fact_span",
                "t_start": 100.0,
                "sid": "s1",
                "facts": {"session_pickup_kind": {"elapsed_ms": 4.0, "degraded": False}},
            }
        ],
    )
    ambient_sink = tmp_path / "ambient-load.jsonl"
    _write_jsonl(ambient_sink, [{"t": 100.0, "live_sessions": 3}])

    import coordinator_core.benchmarks.ambient_sampler as ambient_sampler
    import coordinator_core.lifecycle as lifecycle
    import coordinator_core.telemetry.op_latency as op_latency

    monkeypatch.setattr(op_latency, "sink_generations", lambda repo_root: [op_latency_sink])
    monkeypatch.setattr(ambient_sampler, "_sink_path", lambda common_dir: ambient_sink)
    monkeypatch.setattr(lifecycle, "git_common_dir", lambda repo_root: tmp_path)

    report = flhp.render(tmp_path)
    d = report.as_dict()

    assert set(d["structural"]) == set(flhp.FACT_NAMES)
    assert d["timing"]["per_fact"]["session_pickup_kind"]["computed_count"] == 1
    assert d["ambient_context"][0]["ambient"]["live_sessions"] == 3
    assert d["fact_with_no_production_consumer"] == "session_magnitude_attributed"


def test_render_include_ambient_false_skips_the_join(tmp_path, monkeypatch):
    op_latency_sink = tmp_path / "op-latency.jsonl"
    _write_jsonl(op_latency_sink, [])

    import coordinator_core.telemetry.op_latency as op_latency

    monkeypatch.setattr(op_latency, "sink_generations", lambda repo_root: [op_latency_sink])

    report = flhp.render(tmp_path, include_ambient=False)
    assert report.ambient_context == []


def test_per_fact_rows_are_grouped_by_sid_into_a_ceremony_aggregate():
    """The shape C1 actually emits. The aggregate must be the SUM across one
    ceremony's facts, not one row per fact — an aggregate built per-row would
    report the facade at the cost of its cheapest single fact."""
    rows = [
        {
            "kind": "fact_span",
            "sid": "s1",
            "fact": "session_facts.session_pickup_kind",
            "elapsed_ms": 10.0,
            "outcome": "computed",
        },
        {
            "kind": "fact_span",
            "sid": "s1",
            "fact": "session_facts.session_diff_brightline",
            "elapsed_ms": 90.0,
            "outcome": "computed",
        },
        {
            "kind": "fact_span",
            "sid": "s2",
            "fact": "session_facts.session_pickup_kind",
            "elapsed_ms": 20.0,
            "outcome": "computed",
        },
    ]

    result = flhp.compute_timing_distributions(rows)

    assert result["per_fact"]["session_pickup_kind"].computed_ms == [10.0, 20.0]
    assert result["per_fact"]["session_diff_brightline"].computed_ms == [90.0]
    assert sorted(result["aggregate"].computed_ms) == [20.0, 100.0]


def test_a_degraded_per_fact_row_lands_in_the_degraded_population():
    rows = [
        {
            "kind": "fact_span",
            "sid": "s1",
            "fact": "session_facts.session_fold_sidecars",
            "elapsed_ms": 3.0,
            "outcome": "degraded",
        },
    ]

    result = flhp.compute_timing_distributions(rows)

    stats = result["per_fact"]["session_fold_sidecars"]
    assert stats.degraded_ms == [3.0]
    assert stats.computed_ms == []
    assert result["aggregate"].computed_ms == []


def test_a_per_fact_row_without_a_sid_is_excluded_from_the_aggregate():
    rows = [
        {
            "kind": "fact_span",
            "sid": None,
            "fact": "session_facts.session_terminal_sizings",
            "elapsed_ms": 55.0,
            "outcome": "computed",
        },
    ]

    result = flhp.compute_timing_distributions(rows)

    assert result["per_fact"]["session_terminal_sizings"].computed_ms == [55.0]
    assert result["aggregate"].computed_ms == []

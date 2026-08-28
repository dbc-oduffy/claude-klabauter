"""Tests for coordinator_core.op_census.occupancy_scan.

Never asserts a timing (module docstring / task body). Every test pins the
fixture population and asserts structure: reconciliation counts, the
outcome partition, the liveness oracle's third state, and the windowing
oracle's `hooks.track_touched_files`-shaped worked example (module
docstring § Windowing).
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.op_census import occupancy_scan as scan_mod
from coordinator_core.op_census.occupancy_scan import (
    Liveness,
    Shape,
    Window,
    classify_liveness,
    live_registry_op_names,
    scan_occupancy,
)


def _row(**kwargs) -> str:
    base = {"route": "in_process"}
    base.update(kwargs)
    return json.dumps(base)


def _fake_common_dir(tmp_path):
    fake_common_dir = tmp_path / ".git"
    fake_common_dir.mkdir()
    return fake_common_dir


def _patch_git(monkeypatch, tmp_path):
    fake_common_dir = _fake_common_dir(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir", lambda repo_root: fake_common_dir
    )
    sink = fake_common_dir / "coordinator-sessions" / "logs" / "op-latency.jsonl"
    sink.parent.mkdir(parents=True)
    return sink


# --- fixture-shape coverage (task body's named fixture set) ----------------


def test_started_with_no_complete_counts_as_reconciliation_gap(tmp_path, monkeypatch):
    sink = _patch_git(monkeypatch, tmp_path)
    sink.write_text(
        _row(kind="started", op="foo", t_start=1.0, corr_id="a") + "\n",
        encoding="utf-8",
    )

    result = scan_occupancy(
        tmp_path, live_registry=frozenset({"foo"}), poisoned_modules={}
    )

    foo = result.ops["foo"]
    assert foo.started_count == 1
    assert foo.complete_count == 0
    assert foo.ok_count == 0
    assert foo.occupancy_ms == 0.0


def test_complete_with_no_elapsed_ms_is_not_a_crash_and_not_counted(tmp_path, monkeypatch):
    sink = _patch_git(monkeypatch, tmp_path)
    sink.write_text(
        _row(kind="complete", op="foo", outcome="ok", t_start=1.0) + "\n",
        encoding="utf-8",
    )

    result = scan_occupancy(
        tmp_path, live_registry=frozenset({"foo"}), poisoned_modules={}
    )

    foo = result.ops["foo"]
    assert foo.complete_count == 1
    assert foo.ok_count == 0
    assert foo.occupancy_ms == 0.0


def test_unparseable_line_is_counted_and_read_continues(tmp_path, monkeypatch):
    sink = _patch_git(monkeypatch, tmp_path)
    lines = [
        "{not json",
        _row(kind="complete", op="foo", outcome="ok", elapsed_ms=10.0, t_start=1.0),
    ]
    sink.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = scan_occupancy(
        tmp_path, live_registry=frozenset({"foo"}), poisoned_modules={}
    )

    assert result.stamp.unparseable_rows == 1
    assert result.stamp.total_rows_read == 2
    assert result.ops["foo"].ok_count == 1


def test_op_appearing_in_two_generations_is_merged_under_full_history(tmp_path, monkeypatch):
    sink = _patch_git(monkeypatch, tmp_path)
    sink.write_text(
        _row(kind="complete", op="foo", outcome="ok", elapsed_ms=10.0, t_start=2.0) + "\n",
        encoding="utf-8",
    )
    rotated = sink.with_name("op-latency.1.jsonl")
    rotated.write_text(
        _row(kind="complete", op="foo", outcome="ok", elapsed_ms=20.0, t_start=1.0) + "\n",
        encoding="utf-8",
    )

    result = scan_occupancy(
        tmp_path,
        window=Window.FULL_HISTORY,
        live_registry=frozenset({"foo"}),
        poisoned_modules={},
    )

    foo = result.ops["foo"]
    assert foo.ok_count == 2
    assert foo.occupancy_ms == 30.0
    assert len(result.stamp.generation_paths) == 2


def test_windowing_excludes_early_breach_late_clean_op_under_current_generation(
    tmp_path, monkeypatch
):
    sink = _patch_git(monkeypatch, tmp_path)
    # Current (live) generation: clean.
    sink.write_text(
        _row(kind="complete", op="hooks.track_touched_files", outcome="ok", elapsed_ms=50.0, t_start=2.0)
        + "\n",
        encoding="utf-8",
    )
    # Rotated (older) generation: the pre-fix breach.
    rotated = sink.with_name("op-latency.1.jsonl")
    rotated.write_text(
        _row(
            kind="complete",
            op="hooks.track_touched_files",
            outcome="ok",
            elapsed_ms=6939.7,
            t_start=1.0,
        )
        + "\n",
        encoding="utf-8",
    )

    current = scan_occupancy(
        tmp_path,
        window=Window.CURRENT_GENERATION,
        live_registry=frozenset({"hooks.track_touched_files"}),
        poisoned_modules={},
    )
    assert current.ops["hooks.track_touched_files"].max_observed_ms == 50.0
    assert len(current.stamp.generation_paths) == 1

    full = scan_occupancy(
        tmp_path,
        window=Window.FULL_HISTORY,
        live_registry=frozenset({"hooks.track_touched_files"}),
        poisoned_modules={},
    )
    assert full.ops["hooks.track_touched_files"].max_observed_ms == 6939.7
    assert len(full.stamp.generation_paths) == 2


# --- stamp / reconciliation -------------------------------------------------


def test_stamp_is_populated(tmp_path, monkeypatch):
    sink = _patch_git(monkeypatch, tmp_path)
    sink.write_text(
        _row(kind="complete", op="foo", outcome="ok", elapsed_ms=10.0, t_start=1.0) + "\n",
        encoding="utf-8",
    )

    result = scan_occupancy(
        tmp_path, live_registry=frozenset({"foo"}), poisoned_modules={}
    )

    stamp = result.stamp
    assert stamp.window is Window.CURRENT_GENERATION
    assert len(stamp.generation_paths) == 1
    assert len(stamp.generation_byte_sizes) == 1
    assert stamp.generation_byte_sizes[0] > 0
    assert stamp.total_rows_read == 1
    assert stamp.unparseable_rows == 0
    assert stamp.read_time_secs >= 0.0
    assert stamp.liveness_poisoned_modules == {}


def test_reconciliation_counts_are_exact_with_paired_and_unpaired_rows(tmp_path, monkeypatch):
    sink = _patch_git(monkeypatch, tmp_path)
    lines = [
        _row(kind="started", op="foo", t_start=1.0, corr_id="a"),
        _row(kind="complete", op="foo", outcome="ok", elapsed_ms=5.0, t_start=1.0, corr_id="a"),
        _row(kind="started", op="foo", t_start=2.0, corr_id="b"),
    ]
    sink.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = scan_occupancy(
        tmp_path, live_registry=frozenset({"foo"}), poisoned_modules={}
    )

    foo = result.ops["foo"]
    assert foo.started_count == 2
    assert foo.complete_count == 1
    assert foo.ok_count == 1


# --- outcome partition (AC6b) ------------------------------------------------


def test_occupancy_sums_ok_rows_only(tmp_path, monkeypatch):
    sink = _patch_git(monkeypatch, tmp_path)
    lines = [
        _row(kind="complete", op="foo", outcome="ok", elapsed_ms=10.0, t_start=1.0),
        _row(kind="complete", op="foo", outcome="error", elapsed_ms=1000.0, t_start=1.0),
        _row(kind="complete", op="foo", outcome="timeout", elapsed_ms=5000.0, t_start=1.0),
    ]
    sink.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = scan_occupancy(
        tmp_path, live_registry=frozenset({"foo"}), poisoned_modules={}
    )

    foo = result.ops["foo"]
    assert foo.ok_count == 1
    assert foo.error_count == 1
    assert foo.timeout_count == 1
    assert foo.occupancy_ms == 10.0


def test_max_observed_vs_max_completed_diverge_on_a_timeout_row(tmp_path, monkeypatch):
    sink = _patch_git(monkeypatch, tmp_path)
    lines = [
        _row(kind="complete", op="foo", outcome="ok", elapsed_ms=10.0, t_start=1.0),
        _row(kind="complete", op="foo", outcome="timeout", elapsed_ms=5000.0, t_start=1.0),
    ]
    sink.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = scan_occupancy(
        tmp_path, live_registry=frozenset({"foo"}), poisoned_modules={}
    )

    foo = result.ops["foo"]
    assert foo.max_observed_ms == 5000.0
    assert foo.max_completed_ms == 10.0


# --- shape (AC15) ------------------------------------------------------------


def test_shape_ceiling_dominated_when_non_ok_share_exceeds_bar(tmp_path, monkeypatch):
    sink = _patch_git(monkeypatch, tmp_path)
    lines = [
        _row(kind="complete", op="foo", outcome="ok", elapsed_ms=10.0, t_start=1.0),
        _row(kind="complete", op="foo", outcome="timeout", elapsed_ms=1000.0, t_start=1.0),
    ]
    sink.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = scan_occupancy(
        tmp_path, live_registry=frozenset({"foo"}), poisoned_modules={}
    )

    assert result.ops["foo"].shape is Shape.CEILING_DOMINATED


def test_shape_tail_driven_when_mean_over_p50_bar_is_hit(tmp_path, monkeypatch):
    sink = _patch_git(monkeypatch, tmp_path)
    values = [10.0] * 9 + [1000.0]
    lines = [
        _row(kind="complete", op="foo", outcome="ok", elapsed_ms=v, t_start=1.0)
        for v in values
    ]
    sink.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = scan_occupancy(
        tmp_path, live_registry=frozenset({"foo"}), poisoned_modules={}
    )

    assert result.ops["foo"].shape is Shape.TAIL_DRIVEN


def test_shape_broad_when_neither_bar_is_hit(tmp_path, monkeypatch):
    sink = _patch_git(monkeypatch, tmp_path)
    values = [10.0] * 10
    lines = [
        _row(kind="complete", op="foo", outcome="ok", elapsed_ms=v, t_start=1.0)
        for v in values
    ]
    sink.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = scan_occupancy(
        tmp_path, live_registry=frozenset({"foo"}), poisoned_modules={}
    )

    assert result.ops["foo"].shape is Shape.BROAD


# --- n * p50 twin -------------------------------------------------------------


def test_n_times_p50_is_the_robust_twin_of_occupancy(tmp_path, monkeypatch):
    sink = _patch_git(monkeypatch, tmp_path)
    values = [10.0, 20.0, 30.0]
    lines = [
        _row(kind="complete", op="foo", outcome="ok", elapsed_ms=v, t_start=1.0)
        for v in values
    ]
    sink.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = scan_occupancy(
        tmp_path, live_registry=frozenset({"foo"}), poisoned_modules={}
    )

    foo = result.ops["foo"]
    assert foo.occupancy_ms == 60.0
    assert foo.p50_ok_ms == 20.0
    assert foo.n_times_p50_ms == 60.0


# --- liveness oracle (AC1b) ---------------------------------------------------


def test_classify_liveness_three_known_answers():
    # AC1b: coverage.gate dead, hooks.track_touched_files live,
    # handoff.reconcile_open DEAD as of the K-108 cut (2026-08-27, d20d56893)
    # -- against a real combined registry read.
    #
    # This third answer was LIVE when the test was written and flipped when the
    # 200ms sweep cut the op. It is asserted DEAD rather than deleted because a
    # known-answer test earns its keep from answers that can change: an oracle
    # that only ever sees live ops never demonstrates it can report a dead one
    # against the real registry.
    #
    # The op reads DEAD by an IMPORT-GRAPH ACCIDENT and that is worth knowing
    # here, because it means this assertion is load-bearing in a way the other
    # two are not. `coordinator_core/ops/handoff_reconcile.py` still executes a
    # module-level `register_op("handoff.reconcile_open", _handler)`; it is
    # absent from the runtime registry only because nothing imports the module
    # any more. If any future edit re-imports it, this goes red and the message
    # is "the cut is incomplete", not "the test is stale".
    registry = live_registry_op_names()
    result = classify_liveness(
        ["coverage.gate", "hooks.track_touched_files", "handoff.reconcile_open"],
        live_registry=registry,
        poisoned_modules={},
    )
    assert result["coverage.gate"] is Liveness.DEAD
    assert result["hooks.track_touched_files"] is Liveness.LIVE
    assert result["handoff.reconcile_open"] is Liveness.DEAD


def test_classify_liveness_reports_unclassifiable_when_a_module_is_poisoned():
    registry = frozenset({"alive.op"})
    result = classify_liveness(
        ["alive.op", "vanished.op"],
        live_registry=registry,
        poisoned_modules={"coordinator_core.hooks.broken": "ImportError: boom"},
    )
    assert result["alive.op"] is Liveness.LIVE
    assert result["vanished.op"] is Liveness.UNCLASSIFIABLE


def test_scan_occupancy_liveness_defaults_to_unclassifiable_not_live_when_poisoned(
    tmp_path, monkeypatch
):
    sink = _patch_git(monkeypatch, tmp_path)
    sink.write_text(
        _row(kind="complete", op="never.registered", outcome="ok", elapsed_ms=1.0, t_start=1.0)
        + "\n",
        encoding="utf-8",
    )

    result = scan_occupancy(
        tmp_path,
        live_registry=frozenset(),
        poisoned_modules={"coordinator_core.ops.broken": "ImportError: boom"},
    )

    assert result.ops["never.registered"].liveness is Liveness.UNCLASSIFIABLE


def test_scan_occupancy_liveness_dead_when_absent_and_nothing_poisoned(tmp_path, monkeypatch):
    sink = _patch_git(monkeypatch, tmp_path)
    sink.write_text(
        _row(kind="complete", op="never.registered", outcome="ok", elapsed_ms=1.0, t_start=1.0)
        + "\n",
        encoding="utf-8",
    )

    result = scan_occupancy(
        tmp_path, live_registry=frozenset(), poisoned_modules={}
    )

    assert result.ops["never.registered"].liveness is Liveness.DEAD


# --- routed vs routeless (correction 2026-08-23, § Routed vs routeless) ------


def test_unrouted_row_is_included_not_excluded(tmp_path, monkeypatch):
    """A routeless row is a pre-route-field row (see module docstring's
    § Routed vs routeless), not a distinct or untrustworthy execution path —
    it is accumulated exactly like a routed one, never dropped."""
    sink = _patch_git(monkeypatch, tmp_path)
    lines = [
        json.dumps(
            {"kind": "complete", "op": "foo", "outcome": "ok", "elapsed_ms": 10.0, "t_start": 1.0}
        ),  # no "route" at all
        _row(kind="complete", op="foo", outcome="ok", elapsed_ms=20.0, t_start=1.0),
    ]
    sink.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = scan_occupancy(
        tmp_path, live_registry=frozenset({"foo"}), poisoned_modules={}
    )

    foo = result.ops["foo"]
    assert foo.ok_count == 2
    assert foo.occupancy_ms == 30.0
    assert result.stamp.total_rows_read == 2


def test_routed_and_routeless_counts_and_maxes_are_surfaced_per_op(tmp_path, monkeypatch):
    sink = _patch_git(monkeypatch, tmp_path)
    lines = [
        json.dumps(
            {"kind": "complete", "op": "foo", "outcome": "ok", "elapsed_ms": 999.0, "t_start": 1.0}
        ),  # routeless, and the LARGER max
        _row(kind="complete", op="foo", outcome="ok", elapsed_ms=5.0, t_start=1.0),
    ]
    sink.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = scan_occupancy(
        tmp_path, live_registry=frozenset({"foo"}), poisoned_modules={}
    )

    foo = result.ops["foo"]
    assert foo.routed_count == 1
    assert foo.routeless_count == 1
    assert foo.routed_max_observed_ms == 5.0
    assert foo.routeless_max_observed_ms == 999.0
    # The union max is unaffected by route -- it is the worst thing seen.
    assert foo.max_observed_ms == 999.0


def test_stamp_reports_corpus_level_routeless_share(tmp_path, monkeypatch):
    sink = _patch_git(monkeypatch, tmp_path)
    lines = [
        json.dumps(
            {"kind": "complete", "op": "foo", "outcome": "ok", "elapsed_ms": 1.0, "t_start": 1.0}
        ),
        json.dumps(
            {"kind": "complete", "op": "bar", "outcome": "ok", "elapsed_ms": 1.0, "t_start": 1.0}
        ),
        _row(kind="complete", op="baz", outcome="ok", elapsed_ms=1.0, t_start=1.0),
    ]
    sink.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = scan_occupancy(
        tmp_path,
        live_registry=frozenset({"foo", "bar", "baz"}),
        poisoned_modules={},
    )

    assert result.stamp.routeless_complete_rows == 2
    assert result.stamp.routed_complete_rows == 1
    assert result.stamp.routeless_share == pytest.approx(2 / 3)


# --- admitted_on (AC10: imported, not restated) -------------------------------


def test_admitted_on_field_uses_the_imported_predicate_max_breach(tmp_path, monkeypatch):
    sink = _patch_git(monkeypatch, tmp_path)
    sink.write_text(
        _row(kind="complete", op="foo", outcome="ok", elapsed_ms=5000.0, t_start=1.0) + "\n",
        encoding="utf-8",
    )

    result = scan_occupancy(
        tmp_path, live_registry=frozenset({"foo"}), poisoned_modules={}
    )

    assert result.ops["foo"].admitted_on == ["max"]


def test_admitted_on_field_is_empty_when_neither_axis_breaches(tmp_path, monkeypatch):
    sink = _patch_git(monkeypatch, tmp_path)
    sink.write_text(
        _row(kind="complete", op="foo", outcome="ok", elapsed_ms=10.0, t_start=1.0) + "\n",
        encoding="utf-8",
    )

    result = scan_occupancy(
        tmp_path, live_registry=frozenset({"foo"}), poisoned_modules={}
    )

    assert result.ops["foo"].admitted_on == []


# --- wall-clock cutoff windowing (AC4b) --------------------------------------


def test_wall_clock_cutoff_requires_since():
    import pytest as _pytest

    with _pytest.raises(ValueError):
        scan_occupancy(
            None,  # never reached -- since is required before repo_root is read
            window=Window.WALL_CLOCK_CUTOFF,
            live_registry=frozenset(),
            poisoned_modules={},
        )


def test_wall_clock_cutoff_excludes_pre_fix_breach_but_full_history_column_shows_it(
    tmp_path, monkeypatch
):
    """AC4/AC4b worked example: `hooks.track_touched_files`'s historical
    6,939.7ms breach predates its own fix and must not surface under a
    wall-clock cutoff that excludes it, while the full-history column beside
    it must still show the number -- proving the window excludes it
    deliberately rather than hiding the defect."""
    sink = _patch_git(monkeypatch, tmp_path)
    now = 1_000_000.0
    seven_days = 7 * 24 * 60 * 60.0
    cutoff = now - seven_days

    # Inside the 7-day window: clean.
    sink.write_text(
        _row(
            kind="complete",
            op="hooks.track_touched_files",
            outcome="ok",
            elapsed_ms=50.0,
            t_start=now - 10.0,
        )
        + "\n",
        encoding="utf-8",
    )
    # Outside the 7-day window (older generation): the pre-fix breach.
    rotated = sink.with_name("op-latency.1.jsonl")
    rotated.write_text(
        _row(
            kind="complete",
            op="hooks.track_touched_files",
            outcome="ok",
            elapsed_ms=6939.7,
            t_start=cutoff - 86400.0,
        )
        + "\n",
        encoding="utf-8",
    )

    result = scan_occupancy(
        tmp_path,
        window=Window.WALL_CLOCK_CUTOFF,
        since=cutoff,
        live_registry=frozenset({"hooks.track_touched_files"}),
        poisoned_modules={},
    )

    op = result.ops["hooks.track_touched_files"]
    assert op.max_observed_ms == 50.0
    assert op.max_observed_ms_full_history == 6939.7
    assert result.stamp.since_epoch == cutoff
    assert result.stamp.since_iso is not None
    # Both generations are physically read, per the window's own contract.
    assert len(result.stamp.generation_paths) == 2


def test_wall_clock_cutoff_row_outside_window_excluded_from_reconciliation(
    tmp_path, monkeypatch
):
    sink = _patch_git(monkeypatch, tmp_path)
    cutoff = 1000.0
    sink.write_text(
        _row(kind="started", op="foo", t_start=1.0, corr_id="a") + "\n",
        encoding="utf-8",
    )

    result = scan_occupancy(
        tmp_path,
        window=Window.WALL_CLOCK_CUTOFF,
        since=cutoff,
        live_registry=frozenset({"foo"}),
        poisoned_modules={},
    )

    assert "foo" not in result.ops or result.ops["foo"].started_count == 0


def test_full_history_columns_mirror_windowed_when_no_cutoff_applied(
    tmp_path, monkeypatch
):
    sink = _patch_git(monkeypatch, tmp_path)
    sink.write_text(
        _row(kind="complete", op="foo", outcome="ok", elapsed_ms=10.0, t_start=1.0) + "\n",
        encoding="utf-8",
    )

    result = scan_occupancy(
        tmp_path, live_registry=frozenset({"foo"}), poisoned_modules={}
    )

    foo = result.ops["foo"]
    assert foo.max_observed_ms == foo.max_observed_ms_full_history == 10.0
    assert foo.max_completed_ms == foo.max_completed_ms_full_history == 10.0


def test_admitted_on_matches_module_level_predicate_directly(tmp_path, monkeypatch):
    from coordinator_core.op_budget_suspension import admitted_on

    sink = _patch_git(monkeypatch, tmp_path)
    sink.write_text(
        _row(kind="complete", op="foo", outcome="ok", elapsed_ms=3000.0, t_start=1.0) + "\n",
        encoding="utf-8",
    )

    result = scan_occupancy(
        tmp_path, live_registry=frozenset({"foo"}), poisoned_modules={}
    )
    foo = result.ops["foo"]

    assert foo.admitted_on == admitted_on(foo.max_observed_ms, foo.occupancy_secs)

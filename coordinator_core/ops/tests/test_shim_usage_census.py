"""Tests for coordinator_core.ops.shim_usage_census -- see that module's
docstring for the cheapness/failure-discipline rationale this pins (spec
backlink: docs/plans/2026-08-16-a-process-per-predicate.md, chunk C9,
AC8)."""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.ops import shim_usage_census


def test_record_invocation_appends_one_line_and_census_reports_it(tmp_path):
    now = time.time()
    shim_usage_census.record_invocation("baton-assemble", repo_root=tmp_path, now=now)

    series_path = tmp_path / "state" / "shim-usage-census.jsonl"
    lines = series_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row == {"name": "baton-assemble", "ts": now}

    report = shim_usage_census.census(["baton-assemble", "never-called"], repo_root=tmp_path)
    assert report["baton-assemble"]["invoked"] is True
    assert report["baton-assemble"]["count"] == 1
    assert report["baton-assemble"]["first_ts"] == now
    assert report["baton-assemble"]["last_ts"] == now
    assert report["never-called"]["invoked"] is False
    assert report["never-called"]["count"] == 0


def test_record_invocation_never_truncates_across_calls(tmp_path):
    for i in range(5):
        shim_usage_census.record_invocation("baton-assemble", repo_root=tmp_path, now=float(i))

    series_path = tmp_path / "state" / "shim-usage-census.jsonl"
    lines = series_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5

    report = shim_usage_census.census(["baton-assemble"], repo_root=tmp_path)
    assert report["baton-assemble"]["count"] == 5
    assert report["baton-assemble"]["first_ts"] == 0.0
    assert report["baton-assemble"]["last_ts"] == 4.0


def test_census_never_invoked_target_reports_false_without_series_file(tmp_path):
    # Review: coordinator:code-reviewer -- P2, no series file on disk must
    # be distinguishable from a series that affirmatively shows zero rows;
    # series_present=False is the "we have not been watching yet" signal.
    report = shim_usage_census.census(["baton-assemble"], repo_root=tmp_path)
    assert report["baton-assemble"] == {
        "invoked": False,
        "count": 0,
        "first_ts": None,
        "last_ts": None,
        "series_present": False,
    }


def test_census_distinguishes_no_series_from_empty_series(tmp_path):
    # Review: coordinator:code-reviewer -- P2. A series file that exists
    # but has no rows for this name must report series_present=True while
    # invoked stays False -- distinct from the no-file-at-all case above.
    series_path = tmp_path / "state" / "shim-usage-census.jsonl"
    series_path.parent.mkdir(parents=True, exist_ok=True)
    series_path.write_text('{"name": "some-other-shim", "ts": 1.0}\n', encoding="utf-8")

    report = shim_usage_census.census(["baton-assemble"], repo_root=tmp_path)
    assert report["baton-assemble"]["invoked"] is False
    assert report["baton-assemble"]["series_present"] is True


def test_census_ignores_malformed_lines_and_unknown_names(tmp_path):
    series_path = tmp_path / "state" / "shim-usage-census.jsonl"
    series_path.parent.mkdir(parents=True, exist_ok=True)
    series_path.write_text(
        "not json at all\n"
        '{"name": "sizing-assemble", "ts": 1.0}\n'
        '{"name": "some-other-shim-not-in-targets", "ts": 2.0}\n'
        "\n",
        encoding="utf-8",
    )

    report = shim_usage_census.census(["sizing-assemble"], repo_root=tmp_path)
    assert report["sizing-assemble"]["invoked"] is True
    assert report["sizing-assemble"]["count"] == 1


def test_record_invocation_never_raises_when_series_path_unwritable(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(shim_usage_census, "open", lambda *a, **k: _boom(), raising=False)
    # record_invocation must swallow this, not propagate.
    shim_usage_census.record_invocation("baton-assemble", repo_root=tmp_path)


def test_record_invocation_never_raises_when_repo_root_unresolvable(monkeypatch):
    monkeypatch.setattr(shim_usage_census, "show_toplevel", lambda: None)
    # No repo_root passed -- falls through to _resolve_repo_root(), which
    # must degrade to "not recorded" rather than raising.
    shim_usage_census.record_invocation("baton-assemble")


def test_record_invocation_never_raises_on_non_serializable_name(tmp_path):
    # Review: coordinator:code-reviewer -- nit, malformed `name` was named
    # in the dispatch brief's risk list but not exercised. json.dumps
    # raises on a non-serializable object; the outer except Exception must
    # swallow it, same as any other write failure.
    shim_usage_census.record_invocation(object(), repo_root=tmp_path)  # type: ignore[arg-type]

    series_path = tmp_path / "state" / "shim-usage-census.jsonl"
    assert not series_path.exists()


def test_record_invocation_write_cost_is_far_below_a_rev_parse_spawn(tmp_path):
    """C7's measured floor: a `git rev-parse` spawn costs ~13.6ms; the
    in-process seam this plan replaces it with resolves in 0.14ms. A
    census write on the same hot path must be far below that 13.6ms spawn
    cost, or it is not worth recording per the dispatch brief's cheapness
    constraint -- this is a generous ceiling (not a precision benchmark;
    see plan's own "no block-sampled benchmarks" caution), just a floor
    check that the write is not accidentally doing something expensive
    (fsync, lock acquisition, a spawn) that would eat the C8/C9 win."""
    n = 200
    start = time.perf_counter()
    for i in range(n):
        shim_usage_census.record_invocation("baton-assemble", repo_root=tmp_path, now=float(i))
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    per_call_ms = elapsed_ms / n
    assert per_call_ms < 5.0, f"per-invocation census write cost {per_call_ms:.4f}ms too high"


def test_census_reads_targets_written_by_multiple_simulated_callers(tmp_path):
    # Simulate several "sessions" each recording their own invocation of
    # the same shim, interleaved -- append-only means every one lands as
    # its own complete line with no interleaving corruption.
    for i in range(20):
        shim_usage_census.record_invocation(
            "review-assemble", repo_root=tmp_path, now=float(i)
        )
        shim_usage_census.record_invocation(
            "plan-assemble", repo_root=tmp_path, now=float(i) + 0.5
        )

    report = shim_usage_census.census(["review-assemble", "plan-assemble"], repo_root=tmp_path)
    assert report["review-assemble"]["count"] == 20
    assert report["plan-assemble"]["count"] == 20

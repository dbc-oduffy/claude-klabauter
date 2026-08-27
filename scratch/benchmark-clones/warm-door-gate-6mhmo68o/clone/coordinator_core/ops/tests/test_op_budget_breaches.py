"""coordinator_core.ops.tests.test_op_budget_breaches — the
"op_census.breaches" op's contract.

Purpose: the surface exists to make an over-budget op findable so it can be
deleted. These guard the ways it could stop doing that while still returning
a well-formed dict — a caller widening the bar, the operator line teaching
the habit the surface exists to end, the op going unreachable through the
lazy-dispatch seam, or the read bound going unbounded.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.ipc import CallerFacingValidationError
from coordinator_core.ops import op_budget_breaches
from coordinator_core.ops.op_budget_breaches import (
    DEFAULT_TOP_N,
    breach_report,
    headline_for,
)

BASE_T = 1_755_000_000.0


def _complete(op, elapsed_ms, *, outcome="ok", t_start=BASE_T):
    return {
        "kind": "complete",
        "op": op,
        "t_start": t_start,
        "elapsed_ms": elapsed_ms,
        "outcome": outcome,
    }


def test_registered_under_its_op_name():
    from coordinator_core import ipc

    assert "op_census.breaches" in ipc._REGISTRY


def test_reachable_through_the_lazy_dispatch_seam():
    """Registering the decorator is necessary but not sufficient — an op
    absent from `_registry_map.OP_MODULE_MAP` or from `_EAGER_OP_MODULES` is
    present-but-dead to `coordinator-invoke`."""
    from coordinator_core.ops import _registry_map
    from coordinator_core.ops import __init__ as ops_init  # noqa: F401
    from coordinator_core import ops as ops_pkg

    assert _registry_map.OP_MODULE_MAP["op_census.breaches"] == (
        "coordinator_core.ops.op_budget_breaches"
    )
    assert "coordinator_core.ops.op_budget_breaches" in {
        module for module, _reason in ops_pkg._EAGER_OP_MODULES
    }


def test_scoped_like_its_census_sibling():
    """`repo_root` decides which git common dir the sink is read from; a
    "none" scope would report another worktree's breaches as this one's."""
    from coordinator_core.op_scopes import OP_KEY_SCOPE

    assert OP_KEY_SCOPE["op_census.breaches"] == "show_top"


def test_all_four_registration_surfaces_carry_it():
    """The registration quad — `_REGISTRY`, `_OP_KEY_SCOPE`, `OP_MODULE_MAP`,
    `OP_CLASSIFICATION`. A miss on the last one fails dispatch closed."""
    from coordinator_core.authz.registration_quad import check_registration_quad

    offenders = [v for v in check_registration_quad() if v.op_key == "op_census.breaches"]

    assert offenders == [], [v.surfaces_missing for v in offenders]


def test_classified_compute_only():
    """It opens the sink read-only and writes nothing — see the five-question
    affirmation beside its `OP_CLASSIFICATION` entry."""
    from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass

    assert OP_CLASSIFICATION["op_census.breaches"] is OpClass.COMPUTE_ONLY


def test_a_caller_cannot_widen_the_bar():
    """The habit being banned is handing a slow op more grace. A bar the
    caller supplies would let any op be reported compliant against a number
    it already fits."""
    for banned in ("bar_ms", "budget_ms", "threshold_ms"):
        with pytest.raises(CallerFacingValidationError):
            op_budget_breaches._read_params({banned: 30_000.0})


def test_top_n_is_validated():
    assert op_budget_breaches._read_params({}) == DEFAULT_TOP_N
    assert op_budget_breaches._read_params({"top_n": 3}) == 3
    assert op_budget_breaches._read_params({"top_n": None}) is None
    for bad in (0, -1, "5", True, 2.5):
        with pytest.raises(CallerFacingValidationError):
            op_budget_breaches._read_params({"top_n": bad})


def test_headline_never_names_a_timeout_as_the_remedy():
    """There is no shape of this message in which "raise the timeout" is
    correct — that text is the habit the surface exists to remove."""
    summary = breach_report(entries=[_complete("op.a", 30_000.0)], now=BASE_T)
    text = summary["headline"].lower()

    assert "op.a" in summary["headline"]
    for banned in ("timeout", "increase", "raise", "grace", "budget to", "retry"):
        assert banned not in text, f"headline names {banned!r}: {summary['headline']}"


def test_headline_states_the_fact_once_and_then_the_alternative():
    """Register (`docs/wiki/guard-messaging.md` § Register): one
    content-bearing fact, stated once, plus a terse imperative alternative.
    No apology, no reassurance, no argument for its own standing."""
    summary = breach_report(entries=[_complete("op.a", 30_000.0)], now=BASE_T)
    text = summary["headline"]

    assert "delete" in text.lower()
    for banned in ("sorry", "unfortunately", "please note", "don't worry", "harmless", "as normal"):
        assert banned not in text.lower()
    assert len(text.encode("utf-8")) <= 220


def test_headline_on_a_clean_population_asserts_nothing_extra():
    summary = breach_report(entries=[_complete("op.a", 12.0)], now=BASE_T)

    assert summary["totals"]["breaching_ops"] == 0
    assert "No op over" in summary["headline"]


def test_report_asserts_its_own_process_time_against_both_bars():
    summary = breach_report(entries=[_complete("op.a", 30_000.0)], now=BASE_T)
    self_assessment = summary["self_assessment"]

    assert self_assessment["brightline_budget_ms"] == 500.0
    assert self_assessment["per_process_bar_ms"] == 200.0
    assert self_assessment["under_brightline"] is True
    assert isinstance(self_assessment["handler_total_ms"], float)


def test_source_block_reports_the_read_bound_honestly():
    summary = breach_report(entries=[_complete("op.a", 12.0)], now=BASE_T)

    assert summary["source"]["max_rows"] == op_budget_breaches.MAX_TELEMETRY_ROWS
    assert summary["source"]["rows_capped"] is False
    assert summary["source"]["rows_read"] == 1


def test_reads_only_the_current_generation(tmp_path, monkeypatch):
    """Rotated generations here run to tens of megabytes. Walking them would
    put this op's own cost over the bar it reports against."""
    seen = {}

    def _fake_sink_generations(repo_root):
        seen["called"] = True
        return [tmp_path / "op-latency.jsonl", tmp_path / "op-latency.1.jsonl"]

    monkeypatch.setattr(op_budget_breaches, "sink_generations", _fake_sink_generations)
    (tmp_path / "op-latency.jsonl").write_text("", encoding="utf-8")

    summary = breach_report(repo_root=tmp_path)

    assert seen["called"] is True
    assert summary["source"]["generations_read"] == 1
    assert summary["source"]["generation"] == "op-latency.jsonl"


def test_the_tail_bound_keeps_the_newest_rows_and_says_so(tmp_path, monkeypatch):
    """A breach view needs recency. The bound must drop the OLDEST rows, and
    must declare that it did — a bounded read reported as a whole-population
    one is how a surface like this quietly starts lying."""
    import json

    sink = tmp_path / "op-latency.jsonl"
    with open(sink, "w", encoding="utf-8") as fh:
        for i in range(400):
            fh.write(json.dumps(_complete("op.old", 9_000.0, t_start=BASE_T + i)) + "\n")
        for i in range(400):
            fh.write(json.dumps(_complete("op.new", 9_000.0, t_start=BASE_T + 1000 + i)) + "\n")

    monkeypatch.setattr(op_budget_breaches, "sink_generations", lambda _root: [sink])
    monkeypatch.setattr(op_budget_breaches, "MAX_TAIL_BYTES", sink.stat().st_size // 4)

    summary = breach_report(repo_root=tmp_path, now=BASE_T + 100_000.0)
    ops = {row["op"] for row in summary["ops"]}

    assert ops == {"op.new"}
    assert summary["source"]["head_truncated"] is True


def test_a_whole_generation_under_the_tail_bound_is_not_marked_truncated(tmp_path, monkeypatch):
    import json

    sink = tmp_path / "op-latency.jsonl"
    sink.write_text(json.dumps(_complete("op.a", 9_000.0)) + "\n", encoding="utf-8")
    monkeypatch.setattr(op_budget_breaches, "sink_generations", lambda _root: [sink])

    summary = breach_report(repo_root=tmp_path, now=BASE_T)

    assert summary["source"]["head_truncated"] is False
    assert summary["source"]["rows_read"] == 1


def test_a_missing_sink_degrades_to_an_empty_report(tmp_path, monkeypatch):
    monkeypatch.setattr(
        op_budget_breaches, "sink_generations", lambda _root: [tmp_path / "absent.jsonl"]
    )

    summary = breach_report(repo_root=tmp_path, now=BASE_T)

    assert summary["source"]["rows_read"] == 0
    assert summary["totals"]["breaching_ops"] == 0


def test_truncated_read_refuses_a_trend_direction(tmp_path, monkeypatch):
    """A tail read must not report a DIRECTION, only unqualified figures.

    `_trend` splits the rows it is handed into two halves, so on a truncated
    read both halves sit inside the tail: an op that got dramatically worse
    before the window is flat against itself within it, and reports "flat".
    That is the false-pass `TREND_MIN_ATTEMPTS_PER_HALF` refuses on the sample
    -size axis, and it is refused here on the window axis for the same reason.

    Regression: `ceremony.scoped_git_commit` read "flat" off a 6MB tail while
    a full-generation read showed hourly p50 going 3-8s to 85.4s the same day.
    """
    sink = tmp_path / "op-latency.jsonl"
    rows = [_complete("op.steady", 1_000.0, t_start=BASE_T + i) for i in range(200)]
    with open(sink, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    monkeypatch.setattr(op_budget_breaches, "MAX_TAIL_BYTES", 2_048)
    monkeypatch.setattr(
        op_budget_breaches, "_current_generation_paths", lambda _root: [sink]
    )

    report = breach_report(repo_root=tmp_path)

    assert report["source"]["head_truncated"] is True, "fixture must truncate"
    assert report["ops"], "fixture must produce a breaching op"
    for row in report["ops"]:
        assert row["trend"] == op_budget_breaches.TREND_WINDOW_LIMITED
    assert "flat" not in report["headline"]


def test_untruncated_read_still_reports_a_real_direction(tmp_path, monkeypatch):
    """The refusal is scoped to the truncated case — a whole-generation read
    keeps its direction, or the fix would have deleted the field outright."""
    sink = tmp_path / "op-latency.jsonl"
    rows = [_complete("op.steady", 1_000.0, t_start=BASE_T + i) for i in range(200)]
    with open(sink, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    monkeypatch.setattr(
        op_budget_breaches, "_current_generation_paths", lambda _root: [sink]
    )

    report = breach_report(repo_root=tmp_path)

    assert report["source"]["head_truncated"] is False
    for row in report["ops"]:
        assert row["trend"] != op_budget_breaches.TREND_WINDOW_LIMITED

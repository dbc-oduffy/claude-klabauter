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
from coordinator_core.op_census.kill_ledger_inventory import LedgerAbsent
from coordinator_core.ops import op_budget_breaches
from coordinator_core.ops.op_budget_breaches import (
    DEAD_DIAL_LEDGER_ABSENT,
    DEAD_DIAL_LEDGER_OK,
    DEAD_DIAL_MIN_ATTEMPTS,
    DEFAULT_TOP_N,
    breach_report,
    dead_dial_findings,
    headline_for,
)

BASE_T = 1_755_000_000.0


def _complete(op, elapsed_ms, *, outcome="ok", t_start=BASE_T, caller=None, error_code=None):
    row = {
        "kind": "complete",
        "op": op,
        "t_start": t_start,
        "elapsed_ms": elapsed_ms,
        "outcome": outcome,
    }
    if caller is not None:
        row["caller"] = caller
    if error_code is not None:
        row["error_code"] = error_code
    return row


def _method_not_found(op, *, caller, t_start=BASE_T):
    """A completed `-32601 METHOD_NOT_FOUND` dial — the shape a caller still
    naming a dead or nonexistent op writes to the sink."""
    return _complete(op, 0.5, outcome="error", t_start=t_start, caller=caller, error_code=-32601)


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


def test_headline_names_its_unit_and_claims_no_cpu_attribution():
    """The banner reports WALL CLOCK and must say so, never implying CPU.

    `stolen_ms` is summed wall clock past the bar, not process time. This line
    read "N s stolen from the box" until 2026-08-31, which asserts a cost
    attribution `elapsed_ms` cannot support -- and it misled twice in one day:
    `memo.transition` was reported as one of the worst thieves on the box
    (140.7s stolen, 227/354 over the bar) while the job-object primitive
    measured it at 187.5ms process / 6 procs per call, under the bar the whole
    time. Two sessions proposed rebuilding or killing an op on this signal and
    one retracted a published verdict.

    The bar constant is PROCESS_TIME_BAR_MS, so a banner that compares a wall
    clock figure against it without naming the axis invites exactly that
    misread. Naming the unit is the honest interim named by
    state/bug-backlog/2026-08-30-the-op-census-ranks-breaches-by-wall-clock.yaml;
    it is not the fix, which needs a per-op process figure the sink cannot yet
    supply.
    """
    text = breach_report(entries=[_complete("op.a", 30_000.0)], now=BASE_T)["headline"]

    assert "wall-clock" in text.lower(), text
    # The retired framing claimed CPU this unit never measured.
    assert "stolen" not in text.lower(), text
    # The conviction is still demanded, and still gated on the right axis.
    assert "process time" in text.lower(), text
    assert "delete" in text.lower(), text


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


# ---------------------------------------------------------------------------
# Dead-dial detection — a caller still dialling an op the registry no longer
# (or never did) serve. Two directions, both required: the detector must fire
# on the real incident shape and stay silent on this repo's real noise shape.
# ---------------------------------------------------------------------------


def _warm_start_incident_rows():
    """Fixture rows shaped like `.git/coordinator-sessions/logs/op-latency.1.jsonl`
    — the rotated generation holding the `session.warm_start` incident: 73
    METHOD_NOT_FOUND completions over 33 hours from a looping SessionStart
    hook, real-shaped with two attributed callers."""
    rows = [
        _method_not_found(
            "session.warm_start", caller="coordinator_core.ipc.dispatch_from_hook", t_start=BASE_T + i
        )
        for i in range(70)
    ]
    rows += [
        _method_not_found(
            "session.warm_start", caller="coordinator_core.invoke.__main__", t_start=BASE_T + 1000 + i
        )
        for i in range(3)
    ]
    return rows


def _current_generation_noise_rows():
    """Fixture rows shaped like this repo's current `op-latency.jsonl` naive
    matches: every one a human CLI typo (count <= 2, non-test caller) or a
    test fixture dialling a synthetic name (test-prefixed caller). None must
    survive the detector."""
    return [
        _method_not_found("ops.list", caller="coordinator_core.invoke.__main__", t_start=BASE_T),
        _method_not_found("ops.list", caller="coordinator_core.invoke.__main__", t_start=BASE_T + 1),
        _method_not_found(
            "ceremony.run_commit_pipeline", caller="coordinator_core.invoke.__main__", t_start=BASE_T
        ),
        _method_not_found("engine.health", caller="coordinator_core.invoke.__main__", t_start=BASE_T),
        _method_not_found("warm.status", caller="coordinator_core.invoke.__main__", t_start=BASE_T),
        _method_not_found(
            "no.such.op", caller="coordinator_core.tests.test_dispatch_message", t_start=BASE_T
        ),
        _method_not_found(
            # Caller does NOT start with TEST_CALLER_PREFIX, so this row is
            # excluded by the count threshold (1 < DEAD_DIAL_MIN_ATTEMPTS),
            # not by caller-class filtering — the synthetic-looking op name
            # is not what protects it here.
            "test.this_op_does_not_exist_anywhere",
            caller="coordinator_core.ipc.dispatch_from_hook",
            t_start=BASE_T,
        ),
        _method_not_found(
            "ceremony.scoped_git_commit",
            caller="coordinator_core.tests.test_publish_lane_budget",
            t_start=BASE_T,
        ),
    ]


def test_fires_on_the_warm_start_incident_shape():
    findings = dead_dial_findings(_warm_start_incident_rows())

    assert [f["op"] for f in findings] == ["session.warm_start"]
    assert findings[0]["attempts"] == 73
    assert findings[0]["caller"] == "coordinator_core.ipc.dispatch_from_hook"
    assert findings[0]["first_seen"] == BASE_T
    assert findings[0]["last_seen"] == BASE_T + 1002


def test_stays_silent_on_todays_real_noise_shape():
    """Every one of this repo's current-generation naive -32601 matches —
    four human CLI typos plus two test-fixture dials plus one test-caller
    dial — is excluded, by count for the typos and by caller class for the
    test dials."""
    findings = dead_dial_findings(_current_generation_noise_rows())

    assert findings == []


def test_threshold_boundary():
    below = [
        _method_not_found("op.rare", caller="some.caller", t_start=BASE_T + i)
        for i in range(DEAD_DIAL_MIN_ATTEMPTS - 1)
    ]
    at = [
        _method_not_found("op.frequent", caller="some.caller", t_start=BASE_T + i)
        for i in range(DEAD_DIAL_MIN_ATTEMPTS)
    ]

    assert dead_dial_findings(below) == []
    assert [f["op"] for f in dead_dial_findings(at)] == ["op.frequent"]


def test_test_caller_rows_are_excluded_even_past_the_count_threshold():
    rows = [
        _method_not_found("op.hammered_by_a_test", caller="coordinator_core.tests.some_module", t_start=BASE_T + i)
        for i in range(DEAD_DIAL_MIN_ATTEMPTS + 5)
    ]

    assert dead_dial_findings(rows) == []


def test_a_successful_completion_clears_the_op_even_with_many_failures():
    rows = [
        _method_not_found("op.mixed", caller="some.caller", t_start=BASE_T + i)
        for i in range(DEAD_DIAL_MIN_ATTEMPTS + 5)
    ]
    rows.append(_complete("op.mixed", 5.0, caller="some.caller", t_start=BASE_T + 999))

    assert dead_dial_findings(rows) == []


def test_breach_report_joins_ledger_fate_for_a_dead_dial(monkeypatch):
    """The `session.warm_start` case: the finding names the fate the ledger
    records for the still-dialled op."""

    class _FateEntry:
        def __init__(self, key, title, op_keys, fate_values):
            self.key, self.title, self.op_keys, self.fate_values = key, title, op_keys, fate_values

    monkeypatch.setattr(
        op_budget_breaches,
        "fate_entries",
        lambda: [_FateEntry("K-061", "`session.warm_start`", ["session.warm_start"], ["DEAD"])],
    )

    summary = breach_report(entries=_warm_start_incident_rows(), now=BASE_T)
    dead_dials = summary["dead_dials"]

    assert dead_dials["ledger_status"] == DEAD_DIAL_LEDGER_OK
    assert dead_dials["findings"][0]["op"] == "session.warm_start"
    assert dead_dials["findings"][0]["fate"] == "DEAD"


def test_ledger_absent_is_a_distinguishable_result_not_an_empty_finding_list(monkeypatch):
    """A published mirror without claude-klabauter's `state/` corpus must not read as
    "no findings" — that would turn it into a silent all-clear."""

    def _raise():
        raise LedgerAbsent("no ledger here")

    monkeypatch.setattr(op_budget_breaches, "fate_entries", lambda: _raise())

    summary = breach_report(entries=_warm_start_incident_rows(), now=BASE_T)
    dead_dials = summary["dead_dials"]

    assert dead_dials["ledger_status"] == DEAD_DIAL_LEDGER_ABSENT
    assert dead_dials["findings"][0]["op"] == "session.warm_start"
    assert dead_dials["findings"][0]["fate"] is None


def test_breach_report_reports_no_dead_dials_on_a_clean_population():
    summary = breach_report(entries=[_complete("op.a", 12.0)], now=BASE_T)

    assert summary["dead_dials"]["ledger_status"] == DEAD_DIAL_LEDGER_OK
    assert summary["dead_dials"]["findings"] == []


def test_empty_findings_with_an_absent_ledger_is_not_asserted_ok(monkeypatch):
    """`ledger_status: "ok"` must reflect the ledger actually being present,
    even when `dead_dial_findings` never touches it because there are zero
    qualifying rows — a published mirror with no dead-dial findings must not
    claim `ok` for a ledger it never looked at."""
    monkeypatch.setattr(op_budget_breaches, "KILL_LEDGER", op_budget_breaches.KILL_LEDGER.parent / "no-such-kill-ledger.md")

    summary = breach_report(entries=[_complete("op.a", 12.0)], now=BASE_T)

    assert summary["dead_dials"]["ledger_status"] == DEAD_DIAL_LEDGER_ABSENT
    assert summary["dead_dials"]["findings"] == []


def test_split_caller_tie_reports_the_full_caller_breakdown():
    """A 6/6 split-caller leak (the `ops.list` shape) must not lose the
    non-selected caller — `callers` carries the full breakdown even though
    `caller` picks one for the top-level field."""
    rows = [
        _method_not_found("ops.list", caller="coordinator_core.ops._pool_dispatch_worker", t_start=BASE_T + i)
        for i in range(6)
    ]
    rows += [
        _method_not_found("ops.list", caller="coordinator_core.invoke.__main__", t_start=BASE_T + 100 + i)
        for i in range(6)
    ]

    findings = dead_dial_findings(rows)

    assert [f["op"] for f in findings] == ["ops.list"]
    finding = findings[0]
    assert finding["attempts"] == 12
    assert finding["callers"] == {
        "coordinator_core.ops._pool_dispatch_worker": 6,
        "coordinator_core.invoke.__main__": 6,
    }
    assert finding["caller"] in finding["callers"]


def test_headline_does_not_tell_a_network_arm_to_delete_itself():
    """An arm the op named `*.network` spends its time on a remote, so
    "delete it or rebuild it under the bar" is advice it cannot take —
    and this line renders at every session boot.
    `state/audits/2026-08-31-push-outstanding-lands-under-the-bar-in-
    process-time.md` measured the local half of the worked example at
    UNDER 1ms, ~630x UNDER the bar, while this surface called it the worst
    offender on the box."""
    summary = breach_report(
        entries=[_complete("push.outstanding.network", 30_000.0)], now=BASE_T
    )
    text = summary["headline"]

    assert "push.outstanding.network" in text
    assert "delete" not in text.lower()
    assert "round trip" in text.lower()
    # The breach itself is still reported, not suppressed: the numbers were
    # never the wrong part, only the imperative.
    assert summary["totals"]["breaching_ops"] == 1


def test_network_remedy_does_not_become_a_denylist_of_op_names():
    """The suffix travels with the emitting op, so an arbitrary op opting in
    needs no edit here — and an op that did NOT opt in still gets the
    delete-or-rebuild imperative, whatever it is called."""
    assert "round trip" in op_budget_breaches._remedy_for("anything.at.all.network")
    for op in ("push.outstanding", "network.thing"):
        remedy = op_budget_breaches._remedy_for(op)
        assert "delete" in remedy.lower(), remedy
        assert "rebuild" in remedy.lower(), remedy
        # The kill bar is gated on the axis that can carry a conviction, never
        # softened: this line asks for process time BEFORE the delete, and must
        # not drift into offering a wider budget instead of the delete.
        assert "process time" in remedy.lower(), remedy


def test_network_headline_still_obeys_the_standing_register_rules():
    """The new branch is not exempt from what governs the other one."""
    summary = breach_report(
        entries=[_complete("push.outstanding.network", 30_000.0)], now=BASE_T
    )
    text = summary["headline"]

    for banned in ("timeout", "increase", "raise", "grace", "budget to", "retry"):
        assert banned not in text.lower(), f"headline names {banned!r}: {text}"
    for banned in ("sorry", "unfortunately", "please note", "harmless", "as normal"):
        assert banned not in text.lower()
    assert len(text.encode("utf-8")) <= 220

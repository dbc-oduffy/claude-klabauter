"""Tests for the requirement_register module: rollup, ship refusal, write-back, stall report."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops import requirement_register_stall as rrs


def _row(rid, status="open", **kw):
    row = {
        "id": rid, "source_text": f"text {rid}", "anchor": "§1", "surface": "ui",
        "status": status, "claimed_by": [], "wired": False, "met_by": [],
        "last_progress_at": None, "ruling": None,
    }
    row.update(kw)
    return row


def _sizing(rows, tail="", head="schema: sizing-object\n# keep me\nstatus: routed  # note\n"):
    block = yaml.safe_dump(
        {"requirement_register": {"sources": [{"kind": "pm"}], "rows": rows}}, sort_keys=False)
    return head + block + tail


RULING = {"source": "pm", "quote": "defer it", "on": "2026-10-09"}
P1, P2 = "docs/plans/p.md", "docs/plans/q.md"
NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)


def test_rollup_counts_all_five_statuses():
    rows = [_row("a"), _row("b", "partial"), _row("c", "met"), _row("d", "deferred"),
            _row("e", "waived"), _row("f", "met", wired=True, claimed_by=[P1])]
    assert rr.rollup(rows, NOW) == {
        "open": 1, "partial": 1, "met": 2, "deferred": 1, "waived": 1,
        "unclaimed": 5, "unwired": 1, "coverage": {"met": 2, "total": 6},
        "computed_at": "2026-10-09T12:00:00Z",
    }


@pytest.mark.parametrize("row,needle", [
    (_row("x"), "open"),
    (_row("x", "partial"), "partial"),
    (_row("x", "met", wired=False), "met but not wired"),
    (_row("x", "deferred"), "without a recorded ruling"),
    (_row("x", "waived"), "without a recorded ruling"),
])
def test_ship_refusal_names_each_blocking_class(row, needle):
    reason = rr.ship_refusal(rr.Register(rows=[_row("ok", "met", wired=True), row]))
    assert reason and "row x" in reason and needle in reason


def test_ship_refusal_none_when_met_wired_or_ruled():
    rows = [_row("a", "met", wired=True), _row("b", "deferred", ruling=RULING),
            _row("c", "waived", ruling=RULING)]
    assert rr.ship_refusal(rr.Register(rows=rows)) is None


def test_ship_text_writes_status_only_when_shippable():
    ok = _sizing([_row("a", "met", wired=True)])
    new, refusal = rr.ship_text(ok)
    assert refusal is None and "status: shipped  # note" in new
    new, refusal = rr.ship_text(_sizing([_row("a")]))
    assert new is None and "row a" in refusal
    assert rr.ship_text("schema: sizing-object\nstatus: routed\n") == (None, None)


def test_read_register_tolerant():
    assert rr.read_register("status: routed\n") is None
    assert rr.read_register("requirement_register:\n  rows: nope\n") is None
    assert rr.read_register("requirement_register: [unclosed\n") is None
    assert rr.read_register(_sizing([_row("a")])).rows[0]["id"] == "a"


def test_claimed_rows_keyed_on_plan_path():
    reg = rr.Register(rows=[_row("a", claimed_by=[P1]), _row("b", claimed_by=[P2, P1]),
                            _row("c", claimed_by=[P2]), _row("d")])
    assert [r["id"] for r in rr.claimed_rows(reg, P1)] == ["a", "b"]
    assert [r["id"] for r in rr.claimed_rows(reg, "docs\\plans\\p.md")] == ["a", "b"]
    assert rr.claimed_rows(reg, "") == []


def test_apply_verdicts_preserves_bytes_outside_block_and_recomputes():
    rows = [_row("a", claimed_by=[P1]), _row("b", claimed_by=[P1]), _row("c", claimed_by=[P1]),
            _row("d", claimed_by=[P2])]
    text = _sizing(rows, tail="exit_criterion:\n  statement: x  # inline\n\nplan: p.md\n")
    judged = [
        {"id": "a", "status": "met", "wired": True, "observed_ref": "cli:x"},
        {"id": "b", "status": "met", "wired": False},
        {"id": "d", "status": "met", "wired": True, "observed_ref": "ignored"},
    ]
    new = rr.apply_verdicts(text, judged, P1, "abc123", NOW)
    head, tail = text.split("requirement_register:")[0], text.split("exit_criterion:")[1]
    assert new.startswith(head) and new.endswith("exit_criterion:" + tail)
    reg = {r["id"]: r for r in rr.read_register(new).rows}
    assert reg["a"]["status"] == "met" and reg["a"]["met_by"] == [f"{P1}@abc123"]
    assert reg["a"]["last_progress_at"] == "2026-10-09T12:00:00Z"
    assert reg["b"]["status"] == "partial" and reg["b"]["met_by"] == []
    assert reg["c"]["status"] == "open" and reg["c"]["last_progress_at"] is None
    assert reg["d"]["status"] == "open" and reg["d"]["claimed_by"] == [P2]
    block = yaml.safe_load(new)["requirement_register"]
    assert block["sources"] == [{"kind": "pm"}]
    assert block["rollup"]["coverage"] == {"met": 1, "total": 4}


def test_apply_verdicts_appends_met_by_and_skips_ruled_rows():
    rows = [_row("a", "met", claimed_by=[P1], wired=True, met_by=[f"{P2}@old"]),
            _row("b", "deferred", claimed_by=[P1], ruling=RULING)]
    new = rr.apply_verdicts(
        _sizing(rows),
        [{"id": "a", "status": "met", "wired": True}, {"id": "b", "status": "met", "wired": True}],
        P1, "new", NOW)
    reg = {r["id"]: r for r in rr.read_register(new).rows}
    assert reg["a"]["met_by"] == [f"{P2}@old", f"{P1}@new"] and reg["a"]["status"] == "met"
    assert reg["b"]["status"] == "deferred" and reg["b"]["wired"] is False


def test_plan_claims_reads_sizing_and_rows():
    fm = "plan_id: x\nregister_claims:\n  sizing: state/sizings/s.yaml\n  rows: [a, 'T1.2']\n"
    assert rr.plan_claims(fm) == ("state/sizings/s.yaml", frozenset({"a", "T1.2"}))
    assert rr.plan_claims("plan_id: x\n") == (None, frozenset())
    assert rr.plan_claims(": : bad [") == (None, frozenset())


def test_claimed_rows_extra_ids_union():
    reg = rr.Register(rows=[_row("a", claimed_by=[P1]), _row("b"), _row("c")])
    assert [r["id"] for r in rr.claimed_rows(reg, P1, ["b"])] == ["a", "b"]


def test_blocking_unclaimed_skips_claimed_met_ruled_and_other_plans():
    reg = rr.Register(rows=[
        _row("mine"), _row("done", "met", wired=True),
        _row("def", "deferred", ruling=RULING), _row("other", claimed_by=[P2]),
        _row("unwired", "met", wired=False), _row("bare", "waived"),
    ])
    assert rr.blocking_unclaimed(reg, ["mine"]) == "unwired"
    assert rr.blocking_unclaimed(
        rr.Register(rows=[r for r in reg.rows if r["id"] not in ("unwired", "bare")]), ["mine"]
    ) is None
    assert rr.blocking_unclaimed(rr.Register(rows=[_row("bare", "waived")]), []) == "bare"


def test_apply_verdicts_claim_ids_moves_row_claimed_only_by_plan():
    rows = [_row("a")]
    new = rr.apply_verdicts(
        _sizing(rows), [{"id": "a", "status": "met", "wired": True}], P1, "sha", NOW, ["a"])
    row = rr.read_register(new).rows[0]
    assert row["status"] == "met" and row["met_by"] == [f"{P1}@sha"]
    same = rr.apply_verdicts(_sizing(rows), [{"id": "a", "status": "met", "wired": True}], P1, "sha", NOW)
    assert rr.read_register(same).rows[0]["status"] == "open"


def test_judge_evidence_includes_register_claims_rows_from_claims_sizing(tmp_path):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "state" / "sizings" / "root.yaml").write_text(
        _sizing([_row("a"), _row("b")]), encoding="utf-8")
    (tmp_path / "state" / "sizings" / "seed.yaml").write_text("schema: sizing-object\n", encoding="utf-8")
    plan = ('---\nsizing_object: "state/sizings/seed.yaml"\nregister_claims:\n'
            '  sizing: state/sizings/root.yaml\n  rows: [b]\n---\nbody\n')
    ev = rr.judge_evidence(plan, tmp_path, P1)
    assert [r["id"] for r in ev.rows] == ["b"]


def test_apply_verdicts_no_register_is_identity():
    assert rr.apply_verdicts("status: routed\n", [], "p", "s", datetime.now()) == "status: routed\n"


def _tree(tmp_path, rows, plan_status="executing", plan_age_days=10):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings" / "s.yaml").write_text(_sizing(rows), encoding="utf-8")
    (tmp_path / "state" / "sizings" / "plain.yaml").write_text("status: routed\n", encoding="utf-8")
    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.write_text(
        f'---\nplan_id: "p1"\ncreated: 2026-09-01\nstatus: {plan_status}\n'
        'sizing_object: "state/sizings/s.yaml"\n---\nbody\n', encoding="utf-8")
    old = (datetime(2026, 10, 9) - timedelta(days=plan_age_days)).timestamp()
    os.utime(plan, (old, old))
    return tmp_path


def test_stall_report_names_stale_stuck_and_unclaimed(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    root = _tree(tmp_path, [
        _row("stale", "partial", claimed_by=[P1], last_progress_at="2026-09-20T00:00:00Z"),
        _row("fresh", "partial", claimed_by=[P1], last_progress_at="2026-10-08T00:00:00Z"),
        _row("loose"),
    ])
    report = rr.stall_report(root, date(2026, 10, 9))
    assert len(report.stale_rows) == 1 and "row stale" in report.stale_rows[0]
    assert len(report.stuck_plans) == 1 and "docs/plans/p.md" in report.stuck_plans[0]
    assert len(report.unclaimed_rows) == 1 and "row loose" in report.unclaimed_rows[0]
    point = rr.stall_judgment_point(report, id="j-x")
    assert point["id"] == "j-x" and point["reportable"] is True and point["recommendation"]
    assert all(s in point["evidence"] for s in ("row stale", "p.md", "row loose"))


@pytest.mark.parametrize("closed", ["shipped", "declined", "superseded"])
def test_closed_sizing_contributes_no_unclaimed_rows(tmp_path, closed):
    root = _tree(tmp_path, [_row("loose")])
    head = f"schema: sizing-object\nstatus: {closed}\n"
    (root / "state" / "sizings" / "s.yaml").write_text(_sizing([_row("loose")], head=head), encoding="utf-8")
    assert rr.stall_report(root, date(2026, 10, 9)).unclaimed_rows == []


def test_row_claimed_by_plan_register_claims_is_not_unclaimed(tmp_path):
    root = _tree(tmp_path, [_row("mine"), _row("loose")])
    (root / "docs" / "plans" / "q.md").write_text(
        '---\nplan_id: "q1"\ncreated: 2026-10-08\nstatus: draft\n'
        'register_claims:\n  sizing: "state/sizings/s.yaml"\n  rows: ["mine"]\n---\nbody\n',
        encoding="utf-8")
    report = rr.stall_report(root, date(2026, 10, 9))
    assert len(report.unclaimed_rows) == 1 and "row loose" in report.unclaimed_rows[0]


def test_register_claims_for_another_sizing_does_not_claim(tmp_path):
    root = _tree(tmp_path, [_row("mine")])
    (root / "docs" / "plans" / "q.md").write_text(
        '---\nplan_id: "q1"\ncreated: 2026-10-08\nstatus: draft\n'
        'register_claims:\n  sizing: "state/sizings/other.yaml"\n  rows: ["mine"]\n---\nbody\n',
        encoding="utf-8")
    assert len(rr.stall_report(root, date(2026, 10, 9)).unclaimed_rows) == 1


def test_stale_row_falls_back_to_plan_created(tmp_path):
    root = _tree(tmp_path, [_row("r", claimed_by=[P1])], plan_age_days=0)
    report = rr.stall_report(root, date(2026, 10, 9))
    assert len(report.stale_rows) == 1 and report.stuck_plans == []


def test_young_plan_not_stuck(tmp_path):
    root = _tree(tmp_path, [_row("r", "met", claimed_by=[P1], wired=True)],
                 plan_age_days=1)
    assert not rr.stall_report(root, date(2026, 10, 9))


def test_no_register_means_empty_and_zero_plan_reads(tmp_path, monkeypatch):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "state" / "sizings" / "a.yaml").write_text("status: routed\n", encoding="utf-8")
    monkeypatch.setattr(rr, "_plan_frontmatter", lambda *a, **k: pytest.fail("plan read"))
    report = rr.stall_report(tmp_path, date(2026, 10, 9))
    assert not report and rr.stall_judgment_point(report, id="j") is None


def test_judgment_point_caps_at_fifteen_with_tail():
    report = rr.StallReport(unclaimed_rows=[f"r{i}" for i in range(20)])
    ev = rr.stall_judgment_point(report, id="j")["evidence"]
    assert "r14" in ev and "r15" not in ev and "+5 more" in ev


def test_judge_evidence_inlines_rows_and_pm_words(tmp_path):
    long_text = 'quote " and\nnewline ' + "x" * 2000
    rows = [_row("a", claimed_by=[P1], source_text=long_text, ruling=RULING),
            _row("b", claimed_by=[P2])]
    sizing = _sizing(
        rows, head=("schema: sizing-object\nintent: 'say it'\nintent_source: pm-verbatim\n"
                    "exit_criterion:\n  accepted:\n    pm_quote: 'yes'\n"
                    "  amendments:\n    - pm_quote: also\n"))
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "state" / "sizings" / "s.yaml").write_text(sizing, encoding="utf-8")
    plan = '---\nplan_id: "p1"\nsizing_object: "state/sizings/s.yaml"\n---\nbody\n'
    ev = rr.judge_evidence(plan, tmp_path, P1)
    assert [r["id"] for r in ev.rows] == ["a"] and ev.rows[0]["source_text"] == long_text
    assert ev.pm_words == [("exit_criterion.accepted.pm_quote", "yes"),
                           ("exit_criterion.amendments[0].pm_quote", "also"),
                           ("intent", "say it")]
    assert ev.rulings == [("pm", "a", "defer it")]
    assert not rr.judge_evidence("---\nplan_id: x\n---\n", tmp_path, P1)


def test_the_stall_report_op_returns_the_lines_and_the_ceremony_point(tmp_path):
    root = _tree(tmp_path, [_row("loose")])
    reply = rrs._stall_report_op({"today": "2026-10-09", "jp_id": "jp_x"}, root)
    assert reply["stalled"] is True and len(reply["unclaimed_rows"]) == 1
    assert reply["judgment_point"]["id"] == "jp_x"
    quiet = rrs._stall_report_op({}, tmp_path / "empty")
    assert quiet["stalled"] is False and quiet["judgment_point"] is None
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        rrs._stall_report_op({"today": "Oct 9"}, root)


def test_the_stall_report_op_returns_a_routing_action_per_nonempty_bucket(tmp_path):
    root = _tree(tmp_path, [_row("loose")])
    reply = rrs._stall_report_op({"today": "2026-10-09"}, root)
    actions = reply["routing_actions"]
    assert set(actions) == {"stuck_plans", "unclaimed_rows"}
    assert "engine-ask state/sizings/s.yaml" in actions["unclaimed_rows"]
    assert "sizing.decline" in actions["unclaimed_rows"]
    evidence = reply["judgment_point"]["evidence"]
    assert evidence.count("route:") == 2
    assert actions["stuck_plans"] in reply["judgment_point"]["question"]


NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def test_unruled_rows_needs_a_non_empty_mapping_ruling():
    rows = [_row("a", "deferred"), _row("b", "waived", ruling=RULING),
            _row("c", "deferred", ruling="bare"), _row("d", "open"), _row("e", "waived", ruling={})]
    assert rr.unruled_rows(rows) == ["a", "c", "e"]
    assert rr.unruled_rows([_row("b", "waived", ruling=RULING)]) == []


def test_ship_refusal_refuses_a_non_mapping_ruling():
    reg = rr.Register(rows=[_row("a", "deferred", ruling="because")])
    assert "without a recorded ruling" in rr.ship_refusal(reg)


def test_unknown_row_ids_dedupes_in_input_order():
    reg = rr.Register(rows=[_row("a"), _row("b")])
    assert rr.unknown_row_ids(reg, ["z", "a", 7, "z", "y"]) == ["z", "7", "y"]
    assert rr.unknown_row_ids(reg, ["a", "b"]) == []


def test_duplicate_row_ids_first_seen_order():
    rows = [_row("b"), _row("a"), _row("b"), _row("a"), _row("b"), _row("c")]
    assert rr.duplicate_row_ids(rows) == ["b", "a"]
    assert rr.duplicate_row_ids([_row("a"), _row("b")]) == []


def test_load_register_yaml_keeps_timestamps_as_strings_and_raises_on_bad_yaml():
    out = rr.load_register_yaml("rows:\n  - id: a\n    last_progress_at: 2026-10-09T01:02:03Z\n")
    assert out["rows"][0]["last_progress_at"] == "2026-10-09T01:02:03Z"
    with pytest.raises(yaml.YAMLError):
        rr.load_register_yaml("rows: [unclosed")


def test_write_register_text_appends_at_eof_when_absent():
    head = "schema: sizing-object\nstatus: routed\n"
    out = rr.write_register_text(head, {"sources": [{"kind": "pm"}], "rows": [_row("a")]}, NOW)
    assert out.startswith(head) and out.endswith("\n")
    assert rr.read_register(out).rows[0]["id"] == "a"


def test_write_register_text_replaces_block_and_keeps_tail():
    text = _sizing([_row("old")], tail="after: 1\n")
    out = rr.write_register_text(text, {"sources": [{"kind": "pm"}], "rows": [_row("new")]}, NOW)
    assert out.startswith("schema: sizing-object\n# keep me\nstatus: routed  # note\n")
    assert out.endswith("after: 1\n")
    assert [r["id"] for r in rr.read_register(out).rows] == ["new"]


def test_write_register_text_discards_input_rollup_and_keeps_key_order():
    block = {"sources": [{"kind": "pm"}], "rollup": {"open": 99}, "rows": [_row("a"), _row("b")]}
    out = rr.write_register_text("x: 1\n", block, NOW)
    reg = rr.read_register(out)
    assert list(reg.raw) == ["sources", "rows", "rollup"]
    assert reg.rollup["open"] == 2 and reg.rollup["computed_at"] == "2026-10-09T12:00:00Z"
    assert list(reg.rows[0]) == list(_row("a"))


def test_write_register_text_keeps_crlf():
    text = _sizing([_row("old")], tail="after: 1\n").replace("\n", "\r\n")
    out = rr.write_register_text(text, {"sources": [], "rows": [_row("a")]}, NOW)
    assert "\r\n" in out and "\n" not in out.replace("\r\n", "")
    assert out.endswith("after: 1\r\n")


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_requirement_register_module_scope_does_not_import_ipc():
    code = ("import sys; import coordinator_core.ops.requirement_register; "
            "sys.exit(1 if 'coordinator_core.ipc' in sys.modules else 0)")
    proc = subprocess.run(
        [sys.executable, "-c", code], check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0


def test_surfaces_append_visual_last_and_judge_schema_admits_it():
    import json
    from coordinator_core.ops.review_mint.execute_review import _widen_judge_schema

    assert rr.SURFACES == ("ui", "api", "cli", "internal", "visual")
    judge = json.loads(_widen_judge_schema(json.dumps({"type": "object", "properties": {}})))
    enum = judge["properties"]["register_rows"]["items"]["properties"]["surface"]["enum"]
    assert enum == list(rr.SURFACES) and "visual" in enum


def _cov(verbatims, rows=None, unrelated=None):
    doc = {"pm_verbatims": verbatims}
    if rows is not None or unrelated is not None:
        doc["requirement_register"] = {"rows": rows or []}
        if unrelated is not None:
            doc["requirement_register"][rr.UNRELATED_TURNS_KEY] = unrelated
    return doc


def test_coverage_gaps_substring_after_whitespace_normalisation():
    doc = _cov(
        [{"turn": 3, "text": "use the  _shape_\nthat comes back  from the build"}, {"turn": 5, "text": "hello"}],
        rows=[_row("a", source_text=" use the _shape_ that comes back ")],
    )
    assert rr.coverage_gaps(doc) == [5]


def test_coverage_gaps_unrelated_turn_covers():
    doc = _cov([{"turn": 2, "text": "ok thanks"}], rows=[], unrelated=[{"turn": 2, "reason": "chat"}])
    assert rr.coverage_gaps(doc) == []


def test_coverage_gaps_no_register_leaves_every_turn_uncovered():
    assert rr.coverage_gaps(_cov([{"turn": 4, "text": "x"}, {"turn": 1, "text": "y"}])) == [1, 4]


def test_coverage_gaps_no_pm_verbatims_is_empty():
    assert rr.coverage_gaps({"requirement_register": {"rows": [_row("a")]}}) == []


def test_coverage_gaps_row_in_no_turn_is_legal_and_turnless_skipped():
    doc = _cov([{"text": "no turn number"}, {"turn": 0, "text": "alpha"}], rows=[_row("a", source_text="alpha"), _row("z", source_text="nowhere")])
    assert rr.coverage_gaps(doc) == []


@pytest.mark.parametrize(
    "doc, expected",
    [
        ({"em_analysis": {"ask_parent": "ask-x"}}, True),
        ({"em_analysis": {"ask_parent": ""}}, False),
        ({"em_analysis": {"ask_parent": None}}, False),
        ({"em_analysis": {}}, False),
        ({"em_analysis": "text"}, False),
        ({}, False),
    ],
)
def test_is_sibling_slice_reads_non_empty_ask_parent(doc, expected):
    assert rr.is_sibling_slice(doc) is expected


# The pm-verbatim intent cites turn 29 (so it is owed); R1's text does not cover it.
_TURN_SIZING = (
    "intent_source: pm-verbatim\nintent: shared parent ask\n"
    "pm_verbatims:\n  - turn: 29\n    text: shared parent ask\n"
    "requirement_register:\n  rows:\n    - id: R1\n      source_text: slice own intent\n"
)


def test_uncovered_turns_owes_a_sibling_slice_nothing_but_lists_it_otherwise():
    plain = rr.uncovered_turns(_TURN_SIZING)
    assert plain == [(29, "shared parent ask")]
    slice_text = "em_analysis:\n  ask_parent: ask-x\n" + _TURN_SIZING
    assert rr.uncovered_turns(slice_text) == []


def test_judge_evidence_of_a_slice_carries_no_uncovered_turn(tmp_path):
    slice_text = "em_analysis:\n  ask_parent: ask-x\n" + _TURN_SIZING
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "state" / "sizings" / "s.yaml").write_text(slice_text, encoding="utf-8")
    plan = '---\nplan_id: "p1"\nsizing_object: "state/sizings/s.yaml"\n---\nbody\n'
    assert rr.judge_evidence(plan, tmp_path, P1).uncovered_turns == []
    (tmp_path / "state" / "sizings" / "s.yaml").write_text(_TURN_SIZING, encoding="utf-8")
    assert rr.judge_evidence(plan, tmp_path, P1).uncovered_turns == [(29, "shared parent ask")]


def _scoped(doc, **kw):
    return [t for t, _ in rr.uncovered_turns(yaml.safe_dump(doc), **kw)]


def test_cited_turns_by_row_text_anchor_and_pm_verbatim_intent():
    doc = _cov(
        [{"turn": 1, "text": "alpha beta"}, {"turn": 2, "text": "gamma"}, {"turn": 3, "text": "delta eps"},
         {"turn": 4, "text": "unrelated chatter"}],
        rows=[_row("a", source_text="alpha", anchor="x"), _row("b", source_text="zzz", anchor="pm_verbatims turn 2")],
    )
    doc["intent_source"] = "pm-verbatim"
    doc["intent"] = "delta  eps"
    assert rr.cited_turns(doc) == {1, 2, 3}


def test_em_elaborated_intent_cites_nothing():
    doc = _cov([{"turn": 1, "text": "alpha"}], rows=[_row("a", source_text="zzz")])
    doc["intent_source"] = "em-elaborated"
    doc["intent"] = "alpha"
    assert rr.cited_turns(doc) == set()


def test_uncited_turn_is_not_owed_without_a_run_start():
    doc = _cov([{"turn": 7, "text": "box sequencing"}], rows=[_row("a", source_text="zzz")])
    assert _scoped(doc) == []
    assert rr.coverage_gaps(doc) == [7]


def test_cited_but_uncovered_turn_is_owed():
    doc = _cov([{"turn": 7, "text": "box sequencing"}], rows=[_row("a", source_text="zzz")])
    doc["intent_source"] = "pm-verbatim"
    doc["intent"] = "box sequencing"
    assert _scoped(doc) == [7]


def test_post_start_turn_is_owed_and_pre_start_is_not():
    start = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc).timestamp()
    doc = _cov(
        [{"turn": 1, "text": "early", "ts": "2026-10-16T11:59:59Z"},
         {"turn": 2, "text": "late", "ts": "2026-10-16T12:00:00Z"},
         {"turn": 3, "text": "no ts"}, {"turn": 4, "text": "bad ts", "ts": "soon"}],
        rows=[_row("a", source_text="zzz")],
    )
    assert _scoped(doc, run_started_at=start) == [2]
    assert _scoped(doc) == []


def test_unrelated_turns_waives_a_post_start_turn():
    doc = _cov(
        [{"turn": 2, "text": "late", "ts": "2026-10-16T12:00:00Z"}], rows=[_row("a", source_text="zzz")],
        unrelated=[{"turn": 2, "reason": "chat"}],
    )
    assert _scoped(doc, run_started_at=0.0) == []


def test_unrelated_turn_errors_refuses_unembedded_turn_and_empty_reason():
    doc = _cov([{"turn": 1, "text": "a"}, {"turn": 2, "text": "b"}])
    reg = {rr.UNRELATED_TURNS_KEY: [{"turn": 9, "reason": "r"}, {"turn": 1, "reason": "  "}, {"turn": 2, "reason": "ok"}]}
    errs = rr.unrelated_turn_errors(doc, reg)
    assert len(errs) == 2 and "turn 9" in errs[0] and "turn 1" in errs[1]
    assert rr.unrelated_turn_errors(doc, {"rows": []}) == []


_LOADER_DOC = (
    "stamp: 2026-10-14T10:00:00Z\n"
    "day: 2026-10-14\n"
    "quoted: '2026-10-14T10:00:00Z'\n"
    "block: |\n  line one\n  line two\n"
    "rows:\n- id: R1\n  met_by: [a, b]\n- id: R2\n  met_by: []\n"
)
_LOADER_EXPECT = {
    "stamp": "2026-10-14T10:00:00Z",
    "day": "2026-10-14",
    "quoted": "2026-10-14T10:00:00Z",
    "block": "line one\nline two\n",
    "rows": [{"id": "R1", "met_by": ["a", "b"]}, {"id": "R2", "met_by": []}],
}


@pytest.fixture
def _fresh_loader():
    rr._str_loader_cache.clear()
    yield
    rr._str_loader_cache.clear()


def test_loader_uses_c_base_when_libyaml_present(_fresh_loader):
    if not getattr(yaml, "__with_libyaml__", False):
        pytest.skip("libyaml not available")
    assert issubclass(rr._loader(), yaml.CSafeLoader)


def test_loader_falls_back_to_safeloader_without_libyaml(_fresh_loader, monkeypatch):
    monkeypatch.setattr(yaml, "__with_libyaml__", False, raising=False)
    cls = rr._loader()
    assert issubclass(cls, yaml.SafeLoader) and not issubclass(cls, yaml.CSafeLoader)


@pytest.mark.parametrize("libyaml", [True, False])
def test_loader_parses_identically_on_both_bases(_fresh_loader, monkeypatch, libyaml):
    if libyaml and not getattr(yaml, "__with_libyaml__", False):
        pytest.skip("libyaml not available")
    monkeypatch.setattr(yaml, "__with_libyaml__", libyaml, raising=False)
    assert rr._load(_LOADER_DOC) == _LOADER_EXPECT


def test_constraint_row_recorded_honoured_unblocks_ship(tmp_path):
    """A standing-constraint row is recorded honoured through the existing amend verb (met, wired,
    with an evidence ref in met_by); ship_refusal stops naming it."""
    import coordinator_core.ops.sizing_record_register as record

    (tmp_path / ".git").mkdir()
    rel = "state/sizings/20260101-c.yaml"
    constraint = {"id": "T4", "surface": "internal", "status": "open",
                  "source_text": "never add a git spawn to the commit path"}
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_text(
        "schema: sizing-object\nintent: i\nestimate:\n  tshirt: M\n  provisional: true\n"
        "route: spec-dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\n"
        "premise:\n  provenance: read\n  evidence: fixture\n", encoding="utf-8")
    sources = [{"doc": "docs/x.md", "ref": "abc"}]
    seed = record._handler(
        {"sizing": rel, "register": {"sources": sources, "rows": [constraint]}}, repo_root=tmp_path / ".git")
    assert seed["exit_code"] == 0, seed
    assert rr.ship_refusal(rr.read_register(path.read_text(encoding="utf-8"))) == "row T4 (internal) is open"

    honoured = {"rows": [{"id": "T4", "status": "met", "wired": True, "met_by": ["git:1e60562b55"]}]}
    reply = record._handler(
        {"sizing": rel, "register": honoured, "amend": True}, repo_root=tmp_path / ".git")
    assert reply["exit_code"] == 0, reply
    assert rr.ship_refusal(rr.read_register(path.read_text(encoding="utf-8"))) is None


def test_apply_verdicts_repeat_for_the_same_sha_is_byte_identical():
    rows = [_row("a", claimed_by=[P1])]
    once = rr.apply_verdicts(_sizing(rows), [{"id": "a", "status": "met", "wired": True}], P1, "sha1", NOW)
    again = rr.apply_verdicts(once, [{"id": "a", "status": "met", "wired": True}], P1, "sha1", NOW + timedelta(minutes=5))
    assert again == once
    newer = rr.apply_verdicts(once, [{"id": "a", "status": "met", "wired": True}], P1, "sha2", NOW + timedelta(minutes=5))
    assert newer != once

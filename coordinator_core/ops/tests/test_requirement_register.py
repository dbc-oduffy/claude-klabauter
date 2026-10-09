"""Tests for the requirement_register module: rollup, ship refusal, write-back, stall report."""
from __future__ import annotations

import os
import subprocess
from datetime import date, datetime, timedelta, timezone

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr


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
    reply = rr._stall_report_op({"today": "2026-10-09", "jp_id": "jp_x"}, root)
    assert reply["stalled"] is True and len(reply["unclaimed_rows"]) == 1
    assert reply["judgment_point"]["id"] == "jp_x"
    quiet = rr._stall_report_op({}, tmp_path / "empty")
    assert quiet["stalled"] is False and quiet["judgment_point"] is None
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        rr._stall_report_op({"today": "Oct 9"}, root)

"""backfill.plan: zero, one, several gaps; malformed input yields a reason, never a raise."""
import json

from coordinator_core.workday_start.backfill import plan, plan_with_reason

ROW = "{d}\t3\tbase\ttip\ta,b,c"


def test_zero_gaps():
    assert plan("", "") == []
    assert plan("", json.dumps({"backfilled": [], "skipped": ["2026-10-01"]})) == []


def test_one_gap():
    (e,) = plan(ROW.format(d="2026-10-08"), "")
    assert e["key"] == "day:2026-10-08"
    assert e["argv"] == ["workday-complete", "--for-date", "2026-10-08", "--only"]
    assert e["inputs"]["kind"] == "day" and e["inputs"]["commits"] == 3


def test_several_oldest_first_and_week_dates_merged():
    scan = "\n".join(ROW.format(d=d) for d in ("2026-10-09", "2026-10-07"))
    week = json.dumps({"backfilled": ["2026-10-08", "2026-10-09"], "skipped": []})
    assert [e["key"] for e in plan(scan, week)] == ["day:2026-10-07", "day:2026-10-08", "day:2026-10-09"]


def test_malformed_gives_reason():
    for scan, week in (("garbage", ""), ("", "not json"), ("", "[1]"), ("", '{"backfilled": ["x"]}')):
        entries, reason = plan_with_reason(scan, week)
        assert entries == [] and reason

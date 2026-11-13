"""The covered-row replay probe reports a well-formed, internally consistent cost for an in-memory lessons-shaped routing."""
import json

from coordinator_core.ops.dispatch_emit import grind_covered_replay as probe

_LESSONS = {
    "triage": {
        "kind": "triage",
        "edges": {"discard": "verify-routing-discard", "script-edit": "verify-routing-fix", "dedupe": "verify-routing-fix"},
        "on_fail": "needs-judgment",
    },
    "verify-routing-discard": {"kind": "verify", "edges": {"pass": "refute-close"}, "on_fail": "verify-failed"},
    "verify-routing-fix": {"kind": "verify", "edges": {"pass": "fix"}, "on_fail": "verify-failed"},
    "refute-close": {"kind": "refute-close", "edges": {"confirmed": "commit"}, "on_fail": "needs-judgment"},
    "fix": {"kind": "fix", "edges": {"done": "verify"}, "on_fail": "baton"},
    "verify": {"kind": "verify", "edges": {"pass": "commit"}, "on_fail": "verify-failed"},
    "commit": {"kind": "commit", "edges": {}, "on_fail": "commit-failed"},
}


def test_report_carries_the_four_measures_and_covers_exactly_the_three_covered_rows():
    report = probe.replay(_LESSONS)
    for key in ("agent_calls_total", "agent_calls_per_covered_row", "covered_row_verify_calls", "covered_row_handbacks"):
        assert key in report
    assert sorted(report["covered_row_verify_calls"]) == sorted(probe.COVERED_ROWS)
    assert sorted(report["covered_row_handbacks"]) == sorted(probe.COVERED_ROWS)


def test_total_is_the_sum_of_the_per_kind_counts_and_every_covered_row_is_verified():
    report = probe.replay(_LESSONS)
    assert report["agent_calls_total"] == sum(report["agent_calls_by_kind"].values())
    assert all(n >= 1 for n in report["covered_row_verify_calls"].values())


def test_the_six_ordinary_rows_always_land_and_each_covered_row_ends_closed_or_handed_back_once():
    report = probe.replay(_LESSONS)
    assert report["agent_calls_by_kind"]["commit"] == 1
    for rid, types in report["covered_row_handbacks"].items():
        assert len(types) <= 1, rid
        assert all(t == "rejected-after-retry" for t in types) or not types


def test_a_covered_row_found_at_the_discard_node_is_not_reported_covered():
    routing = {**_LESSONS, "triage": {**_LESSONS["triage"], "edges": {"script-edit": "verify-routing-discard", "discard": "verify-routing-discard"}}}
    reply = probe._verify_reply("r0", "verify-routing-discard", routing)
    assert reply == {"outcome": "pass"}
    assert probe._verify_reply("r0", "verify-routing-fix", _LESSONS)["covered_by"] == probe.COVERED_BY


def test_a_post_fix_verify_of_the_fix_covered_row_reports_covered_with_an_undo_and_ordinary_rows_pass():
    reply = probe._verify_reply("r2", "verify", _LESSONS)
    assert reply["outcome"] == "fail" and reply["covered_by"] == probe.COVERED_BY
    assert reply["undo"]["outcome"] == "undone"
    assert probe._verify_reply("r5", "verify", _LESSONS) == {"outcome": "pass"}
    assert probe._verify_reply("r5", "verify-routing-fix", _LESSONS) == {"outcome": "pass"}


def test_main_prints_the_report_as_json_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(probe, "lessons_routing", lambda: _LESSONS)
    assert probe.main() == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == probe.replay(_LESSONS)

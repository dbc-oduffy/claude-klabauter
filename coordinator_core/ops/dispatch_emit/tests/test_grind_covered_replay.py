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


def test_a_wiki_new_row_covered_at_verify_routing_closes_without_rejected_after_retry_in_two_calls():
    """Live F10 sequence on the real lessons routing: triage wiki-new, verify-routing-fix fails with covered_by,
    the retire route's own verify also fails with covered_by. A covered flip is terminal."""
    routing = {**_LESSONS, "triage": {**_LESSONS["triage"], "edges": {**_LESSONS["triage"]["edges"], "wiki-new": "verify-routing-fix"}}}
    out = probe.replay_covered_flip(routing)
    handed = [h["type"] for h in out["result"]["handed_back"] if h["row"] == "r0"]
    assert "rejected-after-retry" not in handed
    assert handed == []
    assert len(out["calls_after_triage"]) <= 2


def test_a_discard_row_whose_routing_verify_agrees_it_is_covered_closes_in_exactly_one_verify_call():
    calls = []

    def agent(kind, unit):
        calls.append(kind)
        if kind == "triage":
            return [{"row": "r0", "verdict": "discard", "tshirt_size": "S", "tradeoff": ""}]
        if kind == "verify":
            return {"outcome": "pass", "covered_by": probe.COVERED_BY}
        if kind == "refute-close":
            return {"confirmed": [{"row": "r0", "new_path": "archive/r0"}]}
        return {}

    result = probe.gc.run_admission(
        [("b0", ["r0"])], _LESSONS, "triage", reserve=probe.gc.batch_reserve(1),
        max_agent_calls=None, budget=probe._Budget(), agent=agent,
    )
    assert calls.count("verify") == 1
    assert not [h for h in result["handed_back"] if h["row"] == "r0" and h["type"] != "commit-failed"]


def test_main_prints_the_report_as_json_and_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(probe, "lessons_routing", lambda: _LESSONS)
    assert probe.main() == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == probe.replay(_LESSONS)

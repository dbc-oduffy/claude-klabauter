"""Pins for the in-run resolver (decide.py): placement, handback removal, routing, agent results."""
from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ceremony_common.run import decide
from coordinator_core.ceremony_common.run.contract import (
    DECISIONS_FILE,
    HANDBACK_DISPOSITIONS,
    NEVER_OFFERED_DISPOSITIONS,
    Decision,
)
from coordinator_core.contract.decision_object.judgment import build_disposition, build_judgment_point

DECIDE_SOURCE = Path(decide.__file__)
TABLE = decide.RuleTable(
    point_rules=((decide.RECOMMENDATION, decide.recommendation),),
    exhausted_rule="no-live-disposition",
)


def _point(pid, dispositions, *, recommendation=None, reportable=None, evidence="ev"):
    return build_judgment_point(
        recommendation,
        id=pid,
        question=f"question {pid}",
        dispositions=[build_disposition(v) for v in dispositions],
        evidence=evidence,
        reason="r" if recommendation is None else "",
        reportable=reportable,
    )


def _resolve(points, directives=(), context=None):
    return decide.resolve(points, list(directives), rules=TABLE, context=context or {})


def _ids(items):
    return [i["id"] if isinstance(i, dict) else i.point_id for i in items]


def test_every_point_lands_in_exactly_one_list():
    points = [
        _point("j-rec", ["pin", "leave"], recommendation={"disposition": "pin", "rationale": "because"}),
        _point("j-handback-rec", ["surface_to_pm", "leave"],
               recommendation={"disposition": "surface_to_pm", "rationale": "x"}),
        _point("j-open", ["close_stubs", "leave_as_is"]),
        _point("j-only-person", ["surface_to_pm", "pm_reviews_manually"]),
        _point("j-note", ["defer"], recommendation={"disposition": "defer", "rationale": "n"}, reportable=True),
    ]
    out = _resolve(points)
    placed = _ids(out.decisions) + _ids(out.observations) + _ids(out.judgment_entries)
    assert sorted(placed) == sorted(p["id"] for p in points)
    assert len(placed) == len(set(placed))


def test_recommended_point_is_decided_by_recommendation():
    out = _resolve([_point("j-rec", ["pin", "leave"],
                           recommendation={"disposition": "pin", "rationale": "because"})])
    (decision,) = out.decisions
    assert (decision.decision, decision.rule, decision.decided_by) == ("pin", "recommendation", "rule")
    assert decision.rationale == "because"
    assert not out.judgment_entries


def test_recommendation_rule_declines_a_handback_or_undeclared_value():
    handback = _point("j-hb", ["surface_to_pm", "leave"],
                      recommendation={"disposition": "surface_to_pm", "rationale": "x"})
    assert decide.recommendation(handback, context={}) is None
    assert decide.recommendation(_point("j-none", ["pin"]), context={}) is None


def test_point_recommending_a_handback_value_is_an_entry_not_a_decision():
    out = _resolve([_point("j-hb", ["surface_to_pm", "leave"],
                           recommendation={"disposition": "surface_to_pm", "rationale": "x"})])
    assert not out.decisions
    (entry,) = out.judgment_entries
    assert [d["value"] for d in entry["dispositions"]] == ["leave"]


def test_no_entry_offers_a_handback_disposition():
    out = _resolve([
        _point("a", ["surface_to_pm", "accept"]),
        _point("b", ["pm_reviews_manually", "leave_for_now", "pin"]),
    ])
    for entry in out.judgment_entries:
        assert not {d["value"] for d in entry["dispositions"]} & HANDBACK_DISPOSITIONS
    assert out.judgment_entries[0]["agent"] == decide.DEFAULT_AGENT


def test_no_entry_offers_a_never_offered_disposition():
    assert "discard" in NEVER_OFFERED_DISPOSITIONS
    out = _resolve([
        _point("a", ["discard", "pin", "surface_to_pm"]),
        _point("b", ["discard", "surface_to_pm"]),
    ])
    (entry,) = out.judgment_entries
    assert entry["id"] == "a" and [d["value"] for d in entry["dispositions"]] == ["pin"]
    (decision,) = out.decisions
    assert (decision.point_id, decision.decision, decision.rule) == ("b", "no-change", "no-live-disposition")


def test_routing_table_picks_the_agent():
    table = decide.RuleTable(point_rules=(), exhausted_rule="no-live-disposition",
                             routing={"j-special": "reviewer:x"})
    out = decide.resolve([_point("j-special-1", ["pin"]), _point("j-other", ["pin"])], [],
                         rules=table, context={})
    assert [e["agent"] for e in out.judgment_entries] == ["reviewer:x", decide.DEFAULT_AGENT]


def test_point_with_only_handback_dispositions_is_no_change():
    out = _resolve([_point("j-hb", ["surface_to_pm", "pm_reviews_manually"], evidence="producer X")])
    (decision,) = out.decisions
    assert (decision.decision, decision.rule) == ("no-change", "no-live-disposition")
    assert "producer X" in decision.evidence
    assert not out.judgment_entries


def test_reportable_gate_nothing_point_is_an_observation():
    out = _resolve([_point("j-note", ["leave"],
                           recommendation={"disposition": "leave", "rationale": "n"}, reportable=True)])
    assert _ids(out.observations) == ["j-note"]
    assert not out.decisions and not out.judgment_entries


def _entry(pid="j-1", dispositions=("pin", "leave"), agent="coordinator:apm"):
    return {"id": pid, "question": "q", "evidence": "e", "agent": agent,
            "dispositions": [{"value": v, "resolves": []} for v in dispositions]}


def test_agent_result_with_undeclared_disposition_is_undecided(tmp_path):
    results = {"groups": {"judgment": [
        {"key": "j-1", "outcome": "ok", "decision": {"disposition": "switch", "rationale": "r"}},
        {"key": "j-2", "outcome": "ok", "decision": {"disposition": "pin", "rationale": "r"}},
        {"key": "j-3", "outcome": "ok", "decision": None},
    ]}}
    entries = [_entry("j-1"), _entry("j-2", agent="coordinator:the Staff Engineer"), _entry("j-3"), _entry("j-4")]
    decisions, undecided = decide.record_agent_decisions(tmp_path, entries, results)
    assert [(d.point_id, d.decided_by, d.decision) for d in decisions] == [
        ("j-2", "agent:coordinator:the Staff Engineer", "pin")]
    assert {u["id"]: u["reason"] for u in undecided} == {
        "j-1": "undeclared-disposition:switch",
        "j-3": "result-carries-no-decision",
        "j-4": "no-agent-result",
    }
    rows = [json.loads(ln) for ln in (tmp_path / DECISIONS_FILE).read_text().splitlines()]
    assert [r["point_id"] for r in rows] == ["j-2"]
    assert "payload" not in rows[0]


def test_agent_result_payload_is_copied_onto_the_decision(tmp_path):
    results = {"groups": {"judgment": [
        {"key": "j-1", "outcome": "ok", "decision": {"disposition": "pin", "rationale": "r"},
         "payload": {"branch": "work/x"}},
        {"key": "j-2", "outcome": "ok", "decision": {"disposition": "pin", "rationale": "r"},
         "payload": "not-a-mapping"},
    ]}}
    decisions, _ = decide.record_agent_decisions(tmp_path, [_entry("j-1"), _entry("j-2")], results)
    assert [d.payload for d in decisions] == [{"branch": "work/x"}, None]
    rows = [json.loads(ln) for ln in (tmp_path / DECISIONS_FILE).read_text().splitlines()]
    assert rows[0]["payload"] == {"branch": "work/x"}


def test_record_appends_one_object_per_line(tmp_path):
    d = Decision("p", "q", "v", "rule", "recommendation", "r", "e")
    decide.record(tmp_path / "run", [d])
    decide.record(tmp_path / "run", [d])
    lines = (tmp_path / "run" / DECISIONS_FILE).read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["point_id"] == "p"


def test_decisions_map_has_the_apply_base_shape():
    d = Decision("p", "q", "pin", "rule", "recommendation", "r", "e")
    assert decide.decisions_map([d]) == {"p": {"disposition": "pin"}}


def test_decide_holds_no_ceremony_name():
    text = DECIDE_SOURCE.read_text(encoding="utf-8")
    assert "workday-start" not in text and "workday_start" not in text

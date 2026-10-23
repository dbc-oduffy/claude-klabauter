"""Pure tests for ops/_anchor_premise.py."""

import hashlib

from coordinator_core.ops._anchor_premise import (
    FIELD,
    VERDICTS,
    anchor_findings,
    parse_anchors,
    unjudged,
)

GOOD = {"id": "AP1", "claim": "data lives in x", "check": {"command": "grep -c a b", "expect": "1"}}


def _h(line):
    return hashlib.sha256(line.rstrip().encode()).hexdigest()


def test_field_name():
    assert FIELD == "anchor_premises"


def test_absent_key():
    anchors, f = parse_anchors({}, None)
    assert anchors == [] and f[0].startswith("anchor-premises-undeclared")


def test_empty_list_is_clean():
    assert parse_anchors({FIELD: []}, None) == ([], [])


def test_not_a_list():
    assert parse_anchors({FIELD: "x"}, None) == ([], ["anchor-premises-malformed"])


def test_well_formed():
    anchors, f = parse_anchors({FIELD: [GOOD]}, None)
    assert f == [] and anchors[0]["id"] == "AP1"


def test_missing_id_and_claim():
    _, f = parse_anchors({FIELD: [{"claim": "c", "check": GOOD["check"]}, {"id": "B", "claim": " "}]}, None)
    assert f[0].startswith("anchor-premise-unchecked: #0") and f[1].startswith("anchor-premise-unchecked: B")


def test_duplicate_id():
    anchors, f = parse_anchors({FIELD: [GOOD, GOOD]}, None)
    assert len(anchors) == 1 and "duplicate id" in f[0]


def test_bad_check_shapes():
    for check in (None, "x", {"command": "", "expect": "1"}, {"command": "ls", "expect": " "}):
        _, f = parse_anchors({FIELD: [{"id": "A", "claim": "c", "check": check}]}, None)
        assert f and f[0].startswith("anchor-premise-unchecked: A")


def test_screen_refusal():
    bad = {"id": "A", "claim": "c", "check": {"command": "python -c 1", "expect": "1"}}
    anchors, f = parse_anchors({FIELD: [bad]}, None)
    assert anchors == [] and f[0].startswith("anchor-premise-unchecked: A") and "allowed form" in f[0]


A = [{"id": "AP1", "claim": "c" * 300}]


def test_no_verdict():
    assert anchor_findings(A, [], [], "p") == ["anchor-premise-no-verdict: AP1"]


def test_passed_row_resolves():
    assert anchor_findings(A, [{"anchor": "AP1", "verdict": "RESOLVES"}], [], "p") == []


def test_carry_row_resolves_and_stale():
    plan = "line one\nline two"
    live = {"anchor": "AP1", "verdict": "RESOLVES", "line_hash": _h("line two")}
    assert anchor_findings(A, [], [live], plan) == []
    stale = {**live, "line_hash": _h("gone")}
    assert anchor_findings(A, [], [stale], plan) == ["anchor-premise-no-verdict: AP1"]
    assert anchor_findings(A, [], [{**live, "line_hash": None}], plan) == ["anchor-premise-no-verdict: AP1"]


def test_passed_wins_over_carry():
    carry = {"anchor": "AP1", "verdict": "CONTRADICTED", "line_hash": _h("p")}
    assert anchor_findings(A, [{"anchor": "AP1", "verdict": "RESOLVES"}], [carry], "p") == []


def test_non_resolves_verdicts():
    for v in ("UNRESOLVED", "CONTRADICTED", "UNCHECKABLE"):
        (f,) = anchor_findings(A, [{"anchor": "AP1", "verdict": v}], [], "p")
        assert f == f"anchor-premise-{v}: AP1 -- {'c' * 160}"


def test_out_of_enum_verdict_is_no_verdict():
    plan = "p"
    for bad in ("RESOLVE", "resolves", 1, None):
        assert anchor_findings(A, [{"anchor": "AP1", "verdict": bad}], [], plan) == [
            "anchor-premise-no-verdict: AP1"
        ]
        carry = {"anchor": "AP1", "verdict": bad, "line_hash": _h("p")}
        assert anchor_findings(A, [], [carry], plan) == ["anchor-premise-no-verdict: AP1"]
    good = {"anchor": "AP1", "verdict": "RESOLVES", "line_hash": _h("p")}
    assert anchor_findings(A, [{"anchor": "AP1", "verdict": "typo"}], [good], plan) == []


def test_row_anchor_id_is_stripped_before_matching():
    assert anchor_findings(A, [{"anchor": " AP1 ", "verdict": "RESOLVES"}], [], "p") == []


def test_mixed_row_set_reports_unknown_and_non_mapping_rows():
    rows = [
        {"anchor": "AP1", "verdict": "RESOLVES"},
        {"anchor": "AP9", "verdict": "RESOLVES"},
        "junk",
    ]
    assert anchor_findings(A, rows, [], "p") == [
        "anchor-premise-unknown: AP9 (no declared anchor has this id)",
        "anchor-premise-malformed-row: rows[2] (not a mapping)",
    ]


def test_verdict_set_matches_readiness_premise_verdicts():
    assert VERDICTS == {"RESOLVES", "UNRESOLVED", "CONTRADICTED", "UNCHECKABLE"}
    assert unjudged("x") == "anchor-premises-unjudged: x"


def test_exact_id_only():
    assert anchor_findings(A, [{"anchor": "AP10", "verdict": "RESOLVES"}], [], "p") == [
        "anchor-premise-unknown: AP10 (no declared anchor has this id)",
        "anchor-premise-no-verdict: AP1",
    ]

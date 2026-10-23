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


def test_citation_naming_anchor_is_its_verdict():
    row = {"citation": "anchor AP1 -- evidence at x.py:3", "verdict": "RESOLVES"}
    assert anchor_findings(A, [row], [], "p") == []
    assert anchor_findings(A, [{**row, "citation": "anchor: AP1"}], [], "p") == []
    (f,) = anchor_findings(A, [{**row, "verdict": "CONTRADICTED"}], [], "p")
    assert f.startswith("anchor-premise-CONTRADICTED: AP1")


def test_citation_naming_undeclared_or_other_text_does_not_resolve():
    row = {"citation": "anchor AP9 here", "verdict": "RESOLVES"}
    assert anchor_findings(A, [row], [], "p") == [
        "anchor-premise-unknown: AP9 (no declared anchor has this id)",
        "anchor-premise-no-verdict: AP1",
    ]
    assert anchor_findings(A, [{"citation": "anchor AP1", "verdict": "bad"}], [], "p") == [
        "anchor-premise-no-verdict: AP1"
    ]


def test_explicit_anchor_key_wins_over_citation():
    row = {"anchor": "AP1", "citation": "anchor AP9", "verdict": "RESOLVES"}
    assert anchor_findings(A, [row], [], "p") == []


def test_verdict_set_matches_readiness_premise_verdicts():
    assert VERDICTS == {"RESOLVES", "UNRESOLVED", "CONTRADICTED", "UNCHECKABLE"}
    assert unjudged("x") == "anchor-premises-unjudged: x"


def test_exact_id_only():
    assert anchor_findings(A, [{"anchor": "AP10", "verdict": "RESOLVES"}], [], "p") == [
        "anchor-premise-unknown: AP10 (no declared anchor has this id)",
        "anchor-premise-no-verdict: AP1",
    ]


def _engine(monkeypatch, state, observed="1", detail=""):
    from coordinator_core.ops import _anchor_check

    calls = []

    def fake(command, expect, repo_root):
        calls.append((command, expect, repo_root))
        return {"state": state, "observed": observed, "detail": detail}

    monkeypatch.setattr(_anchor_check, "evaluate", fake)
    return calls


def test_engine_states_map_to_verdicts(monkeypatch, tmp_path):
    from coordinator_core.ops._anchor_premise import anchor_verdicts

    one = [{**GOOD, "id": "AP1"}]
    _engine(monkeypatch, "MATCH", "1")
    assert anchor_verdicts(one, [], [], "p", tmp_path) == [
        {"id": "AP1", "verdict": "RESOLVES", "source": "engine-check", "observed": "1"}
    ]
    assert anchor_findings(one, [], [], "p", tmp_path) == []
    _engine(monkeypatch, "MISMATCH", "2")
    (v,) = anchor_verdicts(one, [], [], "p", tmp_path)
    assert (v["verdict"], v["source"]) == ("CONTRADICTED", "engine-check")
    (f,) = anchor_findings(one, [], [], "p", tmp_path)
    assert f.startswith("anchor-premise-CONTRADICTED: AP1")


def test_not_evaluable_leaves_none_source_with_suffix(monkeypatch, tmp_path):
    from coordinator_core.ops._anchor_premise import anchor_verdicts

    one = [{**GOOD, "id": "AP1"}]
    _engine(monkeypatch, "NOT_EVALUABLE", "", "pipe refused")
    (v,) = anchor_verdicts(one, [], [], "p", tmp_path)
    assert v["verdict"] is None and v["source"] == "none"
    assert anchor_findings(one, [], [], "p", tmp_path) == [
        "anchor-premise-no-verdict: AP1 (check not evaluable in-process: pipe refused)"
    ]


def test_row_and_carry_outrank_engine_and_engine_needs_repo_root(monkeypatch, tmp_path):
    from coordinator_core.ops._anchor_premise import anchor_verdicts

    one = [{**GOOD, "id": "AP1"}]
    calls = _engine(monkeypatch, "MATCH")
    row = {"anchor": "AP1", "verdict": "UNRESOLVED"}
    (v,) = anchor_verdicts(one, [row], [], "p", tmp_path)
    assert (v["verdict"], v["source"]) == ("UNRESOLVED", "row")
    carry = {"anchor": "AP1", "verdict": "CONTRADICTED", "line_hash": _h("p")}
    (v,) = anchor_verdicts(one, [], [carry], "p", tmp_path)
    assert (v["verdict"], v["source"]) == ("CONTRADICTED", "carry")
    assert calls == []
    (v,) = anchor_verdicts(one, [], [], "p", None)
    assert (v["verdict"], v["source"]) == (None, "none") and calls == []
    assert anchor_findings(one, [], [], "p") == ["anchor-premise-no-verdict: AP1"]


def _anchors(*ids):
    return [{**GOOD, "id": i} for i in ids]


def test_resolves_row_shapes_clear_their_anchor_by_whole_token():
    four = _anchors("AP1", "AP2", "AP3", "AP4")
    rows = [
        {"citation": "AP1 em_analysis additionalProperties string", "verdict": "RESOLVES"},
        {"citation": "AP2 em_context must be a string", "verdict": "RESOLVES"},
        {"citation": "AP3 sizing_intake_contract.with_em_context", "verdict": "RESOLVES"},
        {"citation": "EM_BRIEF_BYTE_CAP", "evidence": "cap holds (AP4).", "verdict": "RESOLVES"},
    ]
    assert anchor_findings(four, rows, [], "p") == []
    for i in range(4):
        rest = rows[:i] + rows[i + 1 :]
        assert anchor_findings(four, rest, [], "p") == [f"anchor-premise-no-verdict: AP{i + 1}"]


def test_token_match_refuses_substring_and_non_resolves():
    one = _anchors("AP1")
    no = ["anchor-premise-no-verdict: AP1"]
    assert anchor_findings(one, [{"citation": "AP10 x", "verdict": "RESOLVES"}], [], "p") == no
    assert anchor_findings(one, [{"evidence": "xAP1", "verdict": "RESOLVES"}], [], "p") == no
    assert anchor_findings(one, [{"citation": "AP1 x", "verdict": "CONTRADICTED"}], [], "p") == no


def _row(**kw):
    return {"verdict": "RESOLVES", **kw}


def test_range_and_list_specs_credit_each_declared_id():
    four = _anchors("P1", "P2", "P3", "P4")
    for row in (
        _row(citation="anchors P1-P4 resolved"),
        _row(citation="anchor: P1..P4"),
        _row(anchor="P1-P4"),
        _row(anchor="P1..P4"),
        _row(anchor="P1, P2, P3, P4"),
        _row(anchor="P1 P2 P3 P4"),
        _row(citation="anchors P1, P2,P3, P4 -- all hold"),
    ):
        assert anchor_findings(four, [row], [], "p") == [], row
    assert anchor_findings(four, [_row(anchor="P1, P2")], [], "p") == [
        "anchor-premise-no-verdict: P3",
        "anchor-premise-no-verdict: P4",
    ]


def test_citation_prose_after_the_ids_is_not_a_list():
    two = _anchors("P1", "P2")
    row = _row(citation="anchor P1 resolves because P2 is fine")
    assert anchor_findings(two, [row], [], "p") == ["anchor-premise-no-verdict: P2"]


def test_declared_dashed_id_matches_exactly_before_any_expansion():
    declared = _anchors("close-leg-forbids-transcript-scans", "P1-P2", "P1", "P2")
    assert anchor_findings(declared[:1], [_row(anchor="close-leg-forbids-transcript-scans")], [], "p") == []
    only_whole = anchor_findings(declared, [_row(anchor="P1-P2")], [], "p")
    assert "anchor-premise-no-verdict: P1-P2" not in only_whole
    assert "anchor-premise-no-verdict: P1" in only_whole


def test_range_past_declared_ids_credits_only_declared_ones():
    four = _anchors("P1", "P2", "P3", "P4")
    assert anchor_findings(four, [_row(anchor="P1-P9")], [], "p") == []


def test_spec_with_no_declared_member_is_unknown():
    four = _anchors("P1", "P2")
    for spec in ("P5-P7", "Q1, Q2", "P9"):
        out = anchor_findings(four, [_row(anchor=spec)], [], "p")
        assert out[0] == f"anchor-premise-unknown: {spec} (no declared anchor has this id)", spec
    assert anchor_findings(four, [_row(anchor="P3-P1")], [], "p")[0].startswith("anchor-premise-unknown")


def test_range_keeps_zero_padding():
    from coordinator_core.ops._anchor_premise import named_ids

    assert named_ids({"anchor": "P01-P03"}, frozenset()) == ["P01", "P02", "P03"]
    assert named_ids({"anchor": "P8-P10"}, frozenset()) == ["P8", "P9", "P10"]


def test_range_over_the_cap_is_not_expanded():
    wide = _anchors("P1", "P60")
    out = anchor_findings(wide, [_row(anchor="P1-P60")], [], "p")
    assert out[0].startswith("anchor-premise-unknown: P1-P60")

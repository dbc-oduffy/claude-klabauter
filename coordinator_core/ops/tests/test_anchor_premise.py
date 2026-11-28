"""Pure tests for ops/_anchor_premise.py."""

import hashlib

import pytest

from coordinator_core.ops._anchor_premise import (
    FIELD,
    VERDICTS,
    anchor_findings,
    parse_anchors,
    unjudged,
)
from pathlib import Path

pytestmark = pytest.mark.complete

GOOD = {"id": "AP1", "claim": "data lives in x", "check": {"command": "grep -c a b", "expect": "1"}}


def _h(line) -> str:
    return hashlib.sha256(line.rstrip().encode()).hexdigest()


def test_field_name() -> None:
    assert FIELD == "anchor_premises"


def test_absent_key() -> None:
    anchors, f = parse_anchors({}, None)
    assert anchors == [] and f[0].startswith("anchor-premises-undeclared")


def test_empty_list_is_clean() -> None:
    assert parse_anchors({FIELD: []}, None) == ([], [])


def test_not_a_list() -> None:
    assert parse_anchors({FIELD: "x"}, None) == ([], ["anchor-premises-malformed"])


def test_well_formed() -> None:
    anchors, f = parse_anchors({FIELD: [GOOD]}, None)
    assert f == [] and anchors[0]["id"] == "AP1"


def test_missing_id_and_claim() -> None:
    _, f = parse_anchors({FIELD: [{"claim": "c", "check": GOOD["check"]}, {"id": "B", "claim": " "}]}, None)
    assert f[0].startswith("anchor-premise-unchecked: #0") and f[1].startswith("anchor-premise-unchecked: B")


def test_duplicate_id() -> None:
    anchors, f = parse_anchors({FIELD: [GOOD, GOOD]}, None)
    assert len(anchors) == 1 and "duplicate id" in f[0]


def test_bad_check_shapes() -> None:
    for check in (None, "x", {"command": "", "expect": "1"}, {"command": "ls", "expect": " "}):
        _, f = parse_anchors({FIELD: [{"id": "A", "claim": "c", "check": check}]}, None)
        assert f and f[0].startswith("anchor-premise-unchecked: A")


def test_screen_refusal() -> None:
    bad = {"id": "A", "claim": "c", "check": {"command": "python -c 1", "expect": "1"}}
    anchors, f = parse_anchors({FIELD: [bad]}, None)
    assert anchors == [] and f[0].startswith("anchor-premise-unchecked: A") and "allowed form" in f[0]


A = [{"id": "AP1", "claim": "c" * 300}]


def test_no_verdict() -> None:
    assert anchor_findings(A, [], [], "p") == ["anchor-premise-no-verdict: AP1"]


def test_passed_row_resolves() -> None:
    assert anchor_findings(A, [{"anchor": "AP1", "verdict": "RESOLVES"}], [], "p") == []


def test_carry_row_resolves_and_stale() -> None:
    plan = "line one\nline two"
    live = {"anchor": "AP1", "verdict": "RESOLVES", "line_hash": _h("line two")}
    assert anchor_findings(A, [], [live], plan) == []
    stale = {**live, "line_hash": _h("gone")}
    assert anchor_findings(A, [], [stale], plan) == ["anchor-premise-no-verdict: AP1"]
    assert anchor_findings(A, [], [{**live, "line_hash": None}], plan) == ["anchor-premise-no-verdict: AP1"]


def test_passed_wins_over_carry() -> None:
    carry = {"anchor": "AP1", "verdict": "CONTRADICTED", "line_hash": _h("p")}
    assert anchor_findings(A, [{"anchor": "AP1", "verdict": "RESOLVES"}], [carry], "p") == []


def test_non_resolves_verdicts() -> None:
    for v in ("UNRESOLVED", "CONTRADICTED", "UNCHECKABLE"):
        (f,) = anchor_findings(A, [{"anchor": "AP1", "verdict": v}], [], "p")
        assert f == f"anchor-premise-{v}: AP1 -- {'c' * 160}"


def test_out_of_enum_verdict_is_no_verdict() -> None:
    plan = "p"
    for bad in ("RESOLVE", "resolves", 1, None):
        assert anchor_findings(A, [{"anchor": "AP1", "verdict": bad}], [], plan) == [
            "anchor-premise-no-verdict: AP1"
        ]
        carry = {"anchor": "AP1", "verdict": bad, "line_hash": _h("p")}
        assert anchor_findings(A, [], [carry], plan) == ["anchor-premise-no-verdict: AP1"]
    good = {"anchor": "AP1", "verdict": "RESOLVES", "line_hash": _h("p")}
    assert anchor_findings(A, [{"anchor": "AP1", "verdict": "typo"}], [good], plan) == []


def test_row_anchor_id_is_stripped_before_matching() -> None:
    assert anchor_findings(A, [{"anchor": " AP1 ", "verdict": "RESOLVES"}], [], "p") == []


def test_mixed_row_set_reports_unknown_and_non_mapping_rows() -> None:
    rows = [
        {"anchor": "AP1", "verdict": "RESOLVES"},
        {"anchor": "AP9", "verdict": "RESOLVES"},
        "junk",
    ]
    assert anchor_findings(A, rows, [], "p") == [
        "anchor-premise-unknown: AP9 (no declared anchor has this id)",
        "anchor-premise-malformed-row: rows[2] (not a mapping)",
    ]


def test_citation_naming_anchor_is_its_verdict() -> None:
    row = {"citation": "anchor AP1 -- evidence at x.py:3", "verdict": "RESOLVES"}
    assert anchor_findings(A, [row], [], "p") == []
    assert anchor_findings(A, [{**row, "citation": "anchor: AP1"}], [], "p") == []
    # The premise checker's class-5 shape (example-game-repo hair-02 readiness params).
    premise_row = {**row, "citation": "anchor premise AP1: contract.py ... clean at HEAD"}
    assert anchor_findings(A, [premise_row], [], "p") == []
    (f,) = anchor_findings(A, [{**row, "verdict": "CONTRADICTED"}], [], "p")
    assert f.startswith("anchor-premise-CONTRADICTED: AP1")


def test_citation_naming_undeclared_or_other_text_does_not_resolve() -> None:
    row = {"citation": "anchor AP9 here", "verdict": "RESOLVES"}
    assert anchor_findings(A, [row], [], "p") == [
        "anchor-premise-unknown: AP9 (no declared anchor has this id)",
        "anchor-premise-no-verdict: AP1",
    ]
    assert anchor_findings(A, [{"citation": "anchor AP1", "verdict": "bad"}], [], "p") == [
        "anchor-premise-no-verdict: AP1"
    ]


def test_explicit_anchor_key_wins_over_citation() -> None:
    row = {"anchor": "AP1", "citation": "anchor AP9", "verdict": "RESOLVES"}
    assert anchor_findings(A, [row], [], "p") == []


def test_verdict_set_matches_readiness_premise_verdicts() -> None:
    assert VERDICTS == {"RESOLVES", "UNRESOLVED", "CONTRADICTED", "UNCHECKABLE"}
    assert unjudged("x") == "anchor-premises-unjudged: x"


def test_exact_id_only() -> None:
    assert anchor_findings(A, [{"anchor": "AP10", "verdict": "RESOLVES"}], [], "p") == [
        "anchor-premise-unknown: AP10 (no declared anchor has this id)",
        "anchor-premise-no-verdict: AP1",
    ]


def _engine(monkeypatch, state, observed: str = "1", detail: str = ""):
    from coordinator_core.ops import _anchor_check

    calls = []

    def fake(command, expect, repo_root):
        calls.append((command, expect, repo_root))
        return {"state": state, "observed": observed, "detail": detail}

    monkeypatch.setattr(_anchor_check, "evaluate", fake)
    return calls


def test_engine_states_map_to_verdicts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from coordinator_core.ops._anchor_premise import anchor_verdicts

    one = [{**GOOD, "id": "AP1"}]
    _engine(monkeypatch, "MATCH", "1")
    assert anchor_verdicts(one, [], [], "p", tmp_path) == [
        {"id": "AP1", "verdict": "RESOLVES", "source": "engine-check", "observed": "1"}
    ]
    assert anchor_findings(one, [], [], "p", tmp_path) == []
    _engine(monkeypatch, "MISMATCH", "0")
    (v,) = anchor_verdicts(one, [], [], "p", tmp_path)
    assert (v["verdict"], v["source"]) == ("CONTRADICTED", "engine-check")
    (f,) = anchor_findings(one, [], [], "p", tmp_path)
    assert f.startswith("anchor-premise-CONTRADICTED: AP1")


_AP1_CLAIM = "fan_launcher.write_launcher requires a fifth positional emitted argument at HEAD"
_LAUNCHER = (
    "def _render(root, children, emitted: Sequence[Mapping[str, Any]]) -> str:\n"
    "def write_launcher(\n"
    "    emitted: Sequence[Mapping[str, Any]],\n"
)


def _ap1(command, claim=_AP1_CLAIM, expect: str = "1"):
    return [{"id": "AP1", "claim": claim, "check": {"command": command, "expect": expect}}]


def test_true_premise_with_a_loose_check_is_unevaluable_not_contradicted(tmp_path: Path) -> None:
    from coordinator_core.ops._anchor_premise import anchor_verdicts, unevaluable_anchors

    (tmp_path / "fan_launcher.py").write_text(_LAUNCHER)
    loose = _ap1("grep -c 'emitted: Sequence' fan_launcher.py")
    (v,) = anchor_verdicts(loose, [], [], "p", tmp_path)
    assert v["verdict"] is None and v["source"] == "none" and "over-matched" in v["observed"]
    assert anchor_findings(loose, [], [], "p", tmp_path) == []
    assert unevaluable_anchors([v])[0][0] == "AP1"
    tight = _ap1("grep -c -F 'emitted: Sequence[Mapping[str, Any]],' fan_launcher.py")
    (v,) = anchor_verdicts(tight, [], [], "p", tmp_path)
    assert (v["verdict"], v["source"]) == ("RESOLVES", "engine-check")


def test_elided_pattern_mismatch_is_a_malformed_check_not_contradicted(tmp_path: Path) -> None:
    from coordinator_core.ops._anchor_premise import anchor_verdicts

    (tmp_path / "fan_launcher.py").write_text(_LAUNCHER)
    elided = _ap1("grep -c 'emitted: Sequence[...]' fan_launcher.py")
    (v,) = anchor_verdicts(elided, [], [], "p", tmp_path)
    assert v["verdict"] is None and v["source"] == "none"
    assert v["observed"].startswith("malformed check: pattern ") and "elides with '...'" in v["observed"]
    assert anchor_findings(elided, [], [], "p", tmp_path) == []
    fixed = _ap1("grep -c 'absent_token' fan_launcher.py")
    (v,) = anchor_verdicts(fixed, [], [], "p", tmp_path)
    assert (v["verdict"], v["source"]) == ("CONTRADICTED", "engine-check")
    (v,) = anchor_verdicts(elided, [{"id": "AP1", "verdict": "CONTRADICTED"}], [], "p", tmp_path)
    assert v["verdict"] is None and v["source"] == "none" and v["observed"].startswith("malformed check")


def test_overmatch_relief_never_covers_a_shortfall_a_count_claim_or_a_bound(tmp_path: Path) -> None:
    from coordinator_core.ops._anchor_premise import anchor_verdicts

    (tmp_path / "fan_launcher.py").write_text(_LAUNCHER)
    cases = [
        _ap1("grep -c 'absent_token' fan_launcher.py"),
        _ap1("grep -c 'emitted: Sequence' fan_launcher.py", claim="write_launcher has one emitted"),
        _ap1("grep -c 'emitted: Sequence' fan_launcher.py", expect="<=1"),
        _ap1("grep -c 'emitted: Sequence' fan_launcher.py", expect="0"),
    ]
    for anchors in cases:
        (v,) = anchor_verdicts(anchors, [], [], "p", tmp_path)
        assert (v["verdict"], v["source"]) == ("CONTRADICTED", "engine-check"), anchors


def test_not_evaluable_is_not_a_blocker_even_when_the_checker_ran(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from coordinator_core.ops._anchor_premise import anchor_verdicts, unevaluable_anchors

    one = [{**GOOD, "id": "AP1"}]
    _engine(monkeypatch, "NOT_EVALUABLE", "", "pipe refused")
    (v,) = anchor_verdicts(one, [], [], "p", tmp_path)
    assert v["verdict"] is None and v["source"] == "none"
    assert anchor_findings(one, [], [], "p", tmp_path) == []
    assert unevaluable_anchors([v]) == [("AP1", "pipe refused")]
    # a checker verdict that blocks still blocks
    rows = [{"anchor": "AP1", "verdict": "CONTRADICTED"}]
    (f,) = anchor_findings(one, rows, [], "p", tmp_path)
    assert f.startswith("anchor-premise-CONTRADICTED: AP1")


def test_without_a_checker_not_evaluable_moves_to_unevaluable_anchors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from coordinator_core.ops._anchor_premise import anchor_verdicts, unevaluable_anchors

    one = [{**GOOD, "id": "AP1"}]
    _engine(monkeypatch, "NOT_EVALUABLE", "", "pipe refused")
    verdicts = anchor_verdicts(one, [], [], "p", tmp_path)
    assert anchor_findings(one, [], [], "p", tmp_path, verdicts) == []
    assert unevaluable_anchors(verdicts) == [("AP1", "pipe refused")]
    # no repo_root: nothing was evaluated, so the no-verdict finding stays in either lane
    bare = anchor_verdicts(one, [], [], "p", None)
    assert anchor_findings(one, [], [], "p", None, bare) == [
        "anchor-premise-no-verdict: AP1"
    ]
    assert unevaluable_anchors(bare) == []


def test_row_and_carry_outrank_engine_and_engine_needs_repo_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from coordinator_core.ops._anchor_premise import anchor_verdicts

    one = [{**GOOD, "id": "AP1"}]
    calls = _engine(monkeypatch, "MATCH")
    row = {"anchor": "AP1", "verdict": "RESOLVES"}
    (v,) = anchor_verdicts(one, [row], [], "p", tmp_path)
    assert (v["verdict"], v["source"]) == ("RESOLVES", "row")
    carry = {"anchor": "AP1", "verdict": "RESOLVES", "line_hash": _h("p")}
    (v,) = anchor_verdicts(one, [], [carry], "p", tmp_path)
    assert (v["verdict"], v["source"]) == ("RESOLVES", "carry")
    assert calls == []
    (v,) = anchor_verdicts(one, [], [], "p", None)
    assert (v["verdict"], v["source"]) == (None, "none") and calls == []
    assert anchor_findings(one, [], [], "p") == ["anchor-premise-no-verdict: AP1"]


def test_blocking_row_yields_to_a_met_check_but_not_to_a_broken_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from coordinator_core.ops._anchor_premise import anchor_verdicts

    one = [{**GOOD, "id": "AP1"}]
    row = {"anchor": "AP1", "verdict": "UNRESOLVED"}
    _engine(monkeypatch, "MATCH", "1")
    (v,) = anchor_verdicts(one, [row], [], "p", tmp_path)
    assert (v["verdict"], v["source"]) == ("RESOLVES", "engine-check")
    assert anchor_findings(one, [row], [], "p", tmp_path) == []
    _engine(monkeypatch, "MISMATCH", "2")
    (v,) = anchor_verdicts(one, [row], [], "p", tmp_path)
    assert (v["verdict"], v["source"]) == ("UNRESOLVED", "row")


BASELINE_ONLY = {"id": "AP1", "claim": "c", "check": {"command": "grep -c refuse mod.py", "expect": "1"}}


def _plan(falsifier_ref=None) -> str:
    falsifier = f"  falsifier:\n    baseline_ref: {falsifier_ref}\n" if falsifier_ref else ""
    return f"---\nsizing_object: sz.yaml\nprime_exit_criterion:\n  statement: s\n{falsifier}---\n# p\n"


def _own_run(monkeypatch, ap, written, baseline) -> None:
    from coordinator_core.ops import _own_run_premise

    monkeypatch.setattr(_own_run_premise, "own_run_paths", lambda root, sizing: written)
    monkeypatch.setattr(ap, "_run_baseline", lambda root, run_id: baseline)


def test_falsifier_baseline_ref_wins_over_the_run_halt_baseline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from coordinator_core.ops import _anchor_premise as ap

    _own_run(monkeypatch, ap, {"mod.py": "ask-1"}, "halt-sha")
    assert ap._own_context(_plan("abc1234"), tmp_path) == ("abc1234", {"mod.py": "ask-1"})
    assert ap._own_context(_plan(), tmp_path) == ("halt-sha", {"mod.py": "ask-1"})


def _anchors(*ids):
    return [{**GOOD, "id": i} for i in ids]


def test_resolves_row_shapes_clear_their_anchor_by_whole_token() -> None:
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


def test_token_match_refuses_substring_and_non_resolves() -> None:
    one = _anchors("AP1")
    no = ["anchor-premise-no-verdict: AP1"]
    assert anchor_findings(one, [{"citation": "AP10 x", "verdict": "RESOLVES"}], [], "p") == no
    assert anchor_findings(one, [{"evidence": "xAP1", "verdict": "RESOLVES"}], [], "p") == no
    assert anchor_findings(one, [{"citation": "AP1 x", "verdict": "CONTRADICTED"}], [], "p") == no


def _row(**kw):
    return {"verdict": "RESOLVES", **kw}


def test_range_and_list_specs_credit_each_declared_id() -> None:
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


def test_citation_prose_after_the_ids_is_not_a_list() -> None:
    two = _anchors("P1", "P2")
    row = _row(citation="anchor P1 resolves because P2 is fine")
    assert anchor_findings(two, [row], [], "p") == ["anchor-premise-no-verdict: P2"]


def test_declared_dashed_id_matches_exactly_before_any_expansion() -> None:
    declared = _anchors("close-leg-forbids-transcript-scans", "P1-P2", "P1", "P2")
    assert anchor_findings(declared[:1], [_row(anchor="close-leg-forbids-transcript-scans")], [], "p") == []
    only_whole = anchor_findings(declared, [_row(anchor="P1-P2")], [], "p")
    assert "anchor-premise-no-verdict: P1-P2" not in only_whole
    assert "anchor-premise-no-verdict: P1" in only_whole


def test_range_past_declared_ids_credits_only_declared_ones() -> None:
    four = _anchors("P1", "P2", "P3", "P4")
    assert anchor_findings(four, [_row(anchor="P1-P9")], [], "p") == []


def test_spec_with_no_declared_member_is_unknown() -> None:
    four = _anchors("P1", "P2")
    for spec in ("P5-P7", "Q1, Q2", "P9"):
        out = anchor_findings(four, [_row(anchor=spec)], [], "p")
        assert out[0] == f"anchor-premise-unknown: {spec} (no declared anchor has this id)", spec
    assert anchor_findings(four, [_row(anchor="P3-P1")], [], "p")[0].startswith("anchor-premise-unknown")


def test_range_keeps_zero_padding() -> None:
    from coordinator_core.ops._anchor_premise import named_ids

    assert named_ids({"anchor": "P01-P03"}, frozenset()) == ["P01", "P02", "P03"]
    assert named_ids({"anchor": "P8-P10"}, frozenset()) == ["P8", "P9", "P10"]


def test_range_over_the_cap_is_not_expanded() -> None:
    wide = _anchors("P1", "P60")
    out = anchor_findings(wide, [_row(anchor="P1-P60")], [], "p")
    assert out[0].startswith("anchor-premise-unknown: P1-P60")


def test_split_screen_refused_separates_unevaluable_from_malformed() -> None:
    from coordinator_core.ops._anchor_premise import split_screen_refused

    findings = [
        "anchor-premise-unchecked: AP1 (pnpm is not allowed; allowed form: X)",
        "anchor-premise-unchecked: AP2 (check must be {command, expect})",
        "anchor-premise-unchecked: AP3 (id and claim must be non-blank strings)",
        "anchor-premise-unchecked: AP4 (duplicate id)",
    ]
    kept, refused = split_screen_refused(findings)
    assert [i for i, _ in refused] == ["AP1", "AP2"]
    assert kept == findings[2:]


def test_self_falsifying_premise_without_a_ref_is_refused_at_authoring() -> None:
    from coordinator_core.ops import _anchor_premise as ap

    entry = {"id": "AP1", "claim": "mod.py has no refuse", "check": {"command": "grep -c refuse mod.py", "expect": "0"}}
    rows = [{"id": "C1", "writes": ["./mod.py"]}]
    anchors, f = parse_anchors({FIELD: [entry]}, None, rows)
    assert anchors == [] and f[0].startswith("anchor-premise-unchecked: AP1") and "self-falsified" in f[0]
    assert ap.split_screen_refused(f)[0] == f
    named = {**entry, "claim": "at baseline_ref abc1234 mod.py has no refuse"}
    anchors, f = parse_anchors({FIELD: [named]}, None, rows)
    assert [a["id"] for a in anchors] == ["AP1"] and f == []
    anchors, f = parse_anchors({FIELD: [entry]}, None, [{"id": "C1", "writes": ["other.py"]}])
    assert [a["id"] for a in anchors] == ["AP1"] and f == []
    anchors, f = parse_anchors({FIELD: [entry]}, None)
    assert [a["id"] for a in anchors] == ["AP1"] and f == []
    anchors, f = parse_anchors({FIELD: [entry]}, None, rows, base_ref="origin/main")
    assert [a["id"] for a in anchors] == ["AP1"] and f == []

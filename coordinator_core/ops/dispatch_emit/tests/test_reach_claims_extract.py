"""AC1: extract_reach_claims accepts only the canonical backticked form."""

from coordinator_core.ops.dispatch_emit.reach_claims import (
    REACH_CLAIM_REFUSED,
    ReachClaim,
    ReachVerdict,
    extract_reach_claims,
    reach_refusal,
    reach_reply,
)


def _pairs(text):
    return [(c.source, c.target) for c in extract_reach_claims(text)]


def test_canonical_claim_extracted_with_dotted_and_file_symbol():
    note = "`a.b.run` reaches `pkg/mod.py:hookimpl` now.\n"
    assert _pairs(note) == [("a.b.run", "pkg/mod.py:hookimpl")]
    assert extract_reach_claims(note)[0].anchor.startswith("`a.b.run` reaches")


def test_negated_and_free_prose_refused():
    assert _pairs("`X` does not reach `Y`.") == []
    assert _pairs("`X` never reaches `Y`.") == []
    assert _pairs("It is not true that `X` reaches `Y`.") == []
    assert _pairs("X reaches Y here.") == []
    assert _pairs("`X` reaches Y here.") == []
    assert _pairs("X reaches `Y` here.") == []


def test_fenced_and_after_verdicts_ignored():
    note = "```\n`X` reaches `Y`\n```\n`A` reaches `B`\n## Verdicts\n`C` reaches `D`\n"
    assert _pairs(note) == [("A", "B")]


def test_dedup_in_note_order_and_anchor_cap():
    note = "`B` reaches `C`\n`A` reaches `B`\n`B` reaches `C`\n" + "`L` reaches `M` " + "x" * 400
    claims = extract_reach_claims(note)
    assert [(c.source, c.target) for c in claims] == [("B", "C"), ("A", "B"), ("L", "M")]
    assert len(claims[2].anchor) == 300


def test_refusal_and_reply():
    ok = ReachVerdict(ReachClaim("A", "B", "a1"), "reachable", "")
    bad = ReachVerdict(ReachClaim("X", "Y", "a2"), "unreachable", "no import path")
    und = ReachVerdict(ReachClaim("P", "Q", "a3"), "undecidable", "")
    assert reach_refusal([ok, und]) is None
    msg = reach_refusal([ok, bad, und])
    assert msg.startswith(REACH_CLAIM_REFUSED) and "`X`" in msg and "no import path" in msg
    assert reach_reply([ok, bad, und]) == {
        "checked": 3,
        "unreachable": ["a2"],
        "undecidable": ["a3"],
    }

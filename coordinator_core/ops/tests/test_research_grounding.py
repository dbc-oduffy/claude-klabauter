"""Tests for the pure grounding-report parser."""

import pytest

from coordinator_core.ops import _research_grounding as g

NAMES = ["digest-a.md", "digest-b.md"]


def _report(*claims: str, ruling: str = "") -> str:
    body = "\n".join(
        f"## {n}\n" + "\n".join(claims) + "\n" for n in NAMES
    )
    if ruling:
        body += f"## Contradictions\n{ruling}\n"
    return body


def test_all_grounded_passes():
    assert g.grounding_problems(_report("- **grounded** x -- src"), NAMES) == []


def test_ungrounded_named_once_per_line():
    text = _report("- **ungrounded** claim one", "- **grounded** ok")
    out = g.grounding_problems(text, NAMES)
    assert len(out) == 2 and all(p.startswith("ungrounded:") for p in out)


def test_contradicted_named_once_per_line():
    out = g.grounding_problems(_report("- **contradicted** c", "- **grounded** ok"), ["digest-a.md"])
    assert [p.startswith("contradicted:") for p in out] == [True, True]


def test_invented_contradiction_ruling_blocks():
    text = _report("- **grounded** ok", ruling="- **invented** made up")
    out = g.grounding_problems(text, NAMES)
    assert len(out) == 1 and out[0].startswith("invented:")


def test_real_contradiction_ruling_passes():
    text = _report("- **grounded** ok", ruling="- **real** sources disagree")
    assert g.grounding_problems(text, NAMES) == []


def test_missing_digest_section_named():
    text = "## digest-a.md\n- **grounded** ok\n"
    assert g.grounding_problems(text, NAMES) == ["no grounding section for digest-b.md"]


def test_no_claim_lines_refused():
    out = g.grounding_problems("## digest-a.md\n## digest-b.md\n", NAMES)
    assert out == ["grounding.md grades no claim"]


def test_long_ungrounded_line_truncated():
    out = g.grounding_problems(_report("- **ungrounded** " + "x" * 500), ["digest-a.md", "digest-b.md"])
    assert all(len(p) <= len("ungrounded: ") + 160 for p in out)


def test_crlf_report_parses_like_lf():
    lf = _report("- **grounded** a", "  - **ungrounded** b", "- **contradicted** c")
    crlf = lf.replace("\n", "\r\n")
    assert g.grounding_problems(crlf, NAMES) == g.grounding_problems(lf, NAMES)
    assert g.grounding_counts(crlf) == g.grounding_counts(lf)
    assert not any("\r" in p for p in g.grounding_problems(crlf, NAMES))


def test_counts_exact():
    text = _report("- **grounded** a", "- **ungrounded** b", ruling="- **real** r\n- **invented** i\n- **real** s")
    assert g.grounding_counts(text) == {
        "grounded": 2, "ungrounded": 2, "contradicted": 0, "real": 2, "invented": 1,
    }


def test_reground_names():
    assert g.REGROUND_OP == "research.reground"
    assert g.stale_report_name(3) == "grounding.stale-3.md"
    assert g.reground_brief_name(2) == "reground-2.md"


def test_names_refuse_generation_below_one():
    for fn in (g.stale_report_name, g.reground_brief_name):
        for n in (0, -1):
            with pytest.raises(ValueError):
                fn(n)


def test_next_stale_index_empty_is_one():
    assert g.next_stale_index([]) == 1


def test_next_stale_index_gapped_takes_max():
    assert g.next_stale_index(["grounding.stale-1.md", "grounding.stale-4.md"]) == 5


def test_next_stale_index_counts_reground_briefs():
    assert g.next_stale_index(["grounding.stale-1.md", "reground-2.md"]) == 3


def test_next_stale_index_ignores_non_matching():
    names = ["grounding.md", "grounding.stale-x.md", "grounding.stale-.md",
             "grounding.stale-2.txt", "digest-a.md", "reground-b.md"]
    assert g.next_stale_index(names) == 1


def test_grounding_predates_digest_strictness():
    assert g.grounding_predates_digest(10.0, [9.0, 10.5]) is True
    assert g.grounding_predates_digest(10.0, [10.0]) is False
    assert g.grounding_predates_digest(10.0, [9.0]) is False
    assert g.grounding_predates_digest(10.0, []) is False


def test_line_hashes_crlf_equals_lf_and_trailing_space():
    assert g.line_hashes("a\r\nb  \r\n") == g.line_hashes("a\nb\n") == g.line_hashes("a\nb")
    assert len(g.line_hashes("a\nb")) == 2


def test_changed_digests_cases():
    basis = {"digest-a.md": g.line_hashes("x\ny\nz"), "digest-b.md": g.line_hashes("p\nq")}
    cur = {"digest-a.md": "x\nY\nz", "digest-b.md": "p\nq", "digest-c.md": "n1\nn2"}
    assert g.changed_digests(basis, cur) == {"digest-a.md": [2], "digest-c.md": [1, 2]}
    assert g.changed_digests(basis, {"digest-b.md": "p\nq"}) == {}


def test_changed_digests_truncation_and_reorder_count_as_changed():
    basis = {"digest-a.md": g.line_hashes("x\ny\nz")}
    assert g.changed_digests(basis, {"digest-a.md": "x\ny"}) == {"digest-a.md": []}
    assert g.changed_digests(basis, {"digest-a.md": "x\nz\ny"}) == {"digest-a.md": [2, 3]}


def _prior() -> str:
    return (
        "summary line\n\n"
        "## digest-a.md\n- **grounded** ok\n- **contradicted** bad claim\n\n"
        "## digest-b.md\n- **grounded** fine\n\n"
        "## Contradictions\n"
        "- **invented** digest-a.md made up\n"
        "- **real** digest-a.md vs digest-b.md differ\n"
        "- **real** no digest named\n"
    )


def test_report_sections_split():
    s = g.report_sections(_prior())
    assert s["summary"] == ["summary line"]
    assert s["digests"]["digest-a.md"] == ["- **grounded** ok", "- **contradicted** bad claim"]
    assert len(s["rulings"]) == 3


def test_carry_document_shape_is_pinned():
    doc = g.carry_forward(_prior(), {"digest-b.md"}, NAMES, generation=2)
    assert doc == {
        "generation": 2,
        "carried": {"digest-a.md": ["- **grounded** ok", "- **contradicted** bad claim"]},
        "rulings": ["- **invented** digest-a.md made up"],
        "flagged": {"digest-a.md": ["- **contradicted** bad claim"]},
    }
    doc = g.carry_forward(_prior(), set(), NAMES)
    assert doc["rulings"] == [
        "- **invented** digest-a.md made up",
        "- **real** digest-a.md vs digest-b.md differ",
    ]
    assert set(doc) == {"generation", "carried", "rulings", "flagged"}


def test_carry_skips_digest_without_prior_section():
    doc = g.carry_forward("## digest-a.md\n- **grounded** x\n", set(), NAMES)
    assert list(doc["carried"]) == ["digest-a.md"]


def test_effective_report_none_carry_is_fresh_text():
    assert g.effective_report("raw", None, {"digest-a.md"}) == "raw"


def test_effective_report_round_trips_every_verdict():
    carry = g.carry_forward(_prior(), set(), NAMES)
    out = g.effective_report("", carry, set())
    assert g.grounding_counts(out) == g.grounding_counts(_prior()) | {"real": 1}
    assert g.grounding_problems(out, NAMES) == [
        "contradicted: - **contradicted** bad claim",
        "invented: - **invented** digest-a.md made up",
    ]


def test_effective_report_empty_rulings_and_summary_parse():
    carry = g.carry_forward("## digest-a.md\n- **grounded** x\n", set(), ["digest-a.md"])
    out = g.effective_report("", carry, set())
    assert out.rstrip().endswith("No contradictions found.")
    assert g.grounding_problems(out, ["digest-a.md"]) == []


def test_effective_report_drops_carried_section_for_changed_digest():
    carry = g.carry_forward(_prior(), set(), NAMES)
    fresh = "## digest-a.md\n- **grounded** stale nit-free\n"
    out = g.effective_report(fresh, carry, {"digest-a.md"})
    assert g.grounding_problems(out, NAMES) == ["no grounding section for digest-a.md"]
    assert not any("ruling" in p for p in g.grounding_problems(out, NAMES))
    assert "digest-a.md made up" not in out


def test_effective_report_discards_fresh_nit_in_carried_digest():
    carry = g.carry_forward("## digest-a.md\n- **grounded** x\n", set(), ["digest-a.md"])
    fresh = (
        "## digest-a.md\n- **contradicted** new nit\n"
        "## Contradictions\n- **invented** digest-a.md nit\n"
    )
    out = g.effective_report(fresh, carry, set())
    assert g.grounding_problems(out, ["digest-a.md"]) == []


def test_effective_report_keeps_fresh_section_and_rulings_for_regraded_digest():
    carry = g.carry_forward(_prior(), {"digest-b.md"}, NAMES)
    fresh = (
        "## digest-b.md\n- **grounded** redo\n"
        "## Contradictions\n- **invented** digest-b.md fake\n- **real** unnamed note\n"
        "- **invented** digest-a.md only carried\n"
    )
    out = g.effective_report(fresh, carry, set())
    probs = g.grounding_problems(out, NAMES)
    assert "invented: - **invented** digest-b.md fake" in probs
    assert not any("only carried" in p for p in probs)
    assert "contradicted: - **contradicted** bad claim" in probs
    assert "- **real** unnamed note" in out


def test_real_ruling_carried_never_blocks():
    prior = "## digest-a.md\n- **grounded** x\n## Contradictions\n- **real** digest-a.md vs digest-b.md\n"
    carry = g.carry_forward(prior, set(), ["digest-a.md"])
    assert g.grounding_problems(g.effective_report("", carry, set()), ["digest-a.md"]) == []


def test_prose_mentioning_verdict_is_not_a_verdict_line():
    assert g.grounding_counts("the claim is **ungrounded** here\n") == dict.fromkeys(
        g.CLAIM_VERDICTS + g.RULINGS, 0
    )

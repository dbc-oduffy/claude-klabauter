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


def test_prose_mentioning_verdict_is_not_a_verdict_line():
    assert g.grounding_counts("the claim is **ungrounded** here\n") == dict.fromkeys(
        g.CLAIM_VERDICTS + g.RULINGS, 0
    )

"""load_em_brief refuses an inline brief, brief_file or em_context block that falls back from the corpus to engine-source reads."""

import pytest

from coordinator_core.ops import corpus_fallback
from coordinator_core.ops.dispatch_emit.ask_brief import EM_BRIEF_BYTE_CAP, EmBriefRefused, load_em_brief
from coordinator_core.ops.sizing_intake_contract import with_em_context

FALLBACK = "if example-retrieval-repo is unreachable, fall back to engine-source reads"
BENIGN = "example-retrieval-repo was ECONNREFUSED; stop and memo example-retrieval-repo-em"


def test_inline_brief_refused_naming_the_rule(tmp_path):
    with pytest.raises(EmBriefRefused, match=corpus_fallback.RULE):
        load_em_brief(tmp_path, brief=f"Do the thing. {FALLBACK}", brief_file=None)


def test_brief_file_refused_naming_the_rule(tmp_path):
    (tmp_path / "brief.md").write_text(f"Do the thing.\n{FALLBACK}\n", encoding="utf-8")
    with pytest.raises(EmBriefRefused, match=corpus_fallback.RULE):
        load_em_brief(tmp_path, brief=None, brief_file="brief.md")


def test_em_context_block_refused(tmp_path):
    brief = with_em_context("Do the thing.", FALLBACK)
    with pytest.raises(EmBriefRefused, match=corpus_fallback.RULE):
        load_em_brief(tmp_path, brief=brief, brief_file=None)


def test_refused_oversize_brief_writes_no_spill_file(tmp_path):
    brief = FALLBACK + "\n" + "x" * (EM_BRIEF_BYTE_CAP + 10)
    with pytest.raises(EmBriefRefused):
        load_em_brief(tmp_path, brief=brief, brief_file=None)
    assert not (tmp_path / "scratch").exists()


def test_benign_outage_brief_is_accepted(tmp_path):
    loaded = load_em_brief(tmp_path, brief=with_em_context(BENIGN, BENIGN), brief_file=None)
    assert loaded is not None and BENIGN in loaded.text

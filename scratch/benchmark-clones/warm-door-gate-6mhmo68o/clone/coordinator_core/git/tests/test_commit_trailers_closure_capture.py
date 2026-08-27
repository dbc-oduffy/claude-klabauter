"""Tests for coordinator_core.git.commit_trailers's closure-fact extractor
(C1, state/dispatch-briefs/2026-08-22-the-commit-closure-pipe-carries-rows/
C1.md, AC1/AC2).

Covers the trailing-region bound this chunk introduces: a `Closes:` line
demoted out of git's own last-paragraph trailer block (successive `-m`
args) must still record, while a `Closes:`-shaped line sitting OUTSIDE the
trailing region (a quoted/embedded prior commit message in the body) must
NOT.
"""

from __future__ import annotations

from coordinator_core.git.commit_trailers import (
    extract_closure_facts,
    extract_closure_facts_from_text,
)


def test_demoted_closes_paragraph_still_records():
    # Built the way `git commit -m subject -m "Closes: RECS-1" -m "Commit-Token: abc"`
    # produces it: three blank-line-separated paragraphs, `Closes:` sitting
    # ABOVE the trailer block git itself would parse -- the exact demotion
    # shape DECISION-2 used to miss.
    text = "Subject line\n\nCloses: RECS-1\n\nCommit-Token: abc123\n"
    closes, reverts_sha = extract_closure_facts_from_text(text)
    assert closes == ["RECS-1"]
    assert reverts_sha is None


def test_multi_closes_lines_all_captured():
    text = "Subject\n\nCloses: RECS-1\nCloses: RECS-2\n\nCommit-Token: abc123\n"
    closes, _ = extract_closure_facts_from_text(text)
    assert closes == ["RECS-1", "RECS-2"]


def test_properly_blocked_trailer_records():
    # A single, correctly-placed trailing trailer block -- git's own parser
    # would already see this; must keep working under the new extractor.
    text = "Subject\n\nBody paragraph text.\n\nCloses: RECS-3\nCommit-Token: abc123\n"
    closes, _ = extract_closure_facts_from_text(text)
    assert closes == ["RECS-3"]


def test_non_line_anchored_closes_does_not_record():
    # Prose containing "closes" mid-sentence, and a line where the token
    # isn't anchored at column 0, must never match.
    text = "Subject\n\nThis change closes the loop on the earlier bug.\n"
    closes, _ = extract_closure_facts_from_text(text)
    assert closes == []


def test_revert_line_captured():
    text = 'Revert "Subject line"\n\nThis reverts commit deadbeefdeadbeefdeadbeefdeadbeefdeadbeef.\n'
    closes, reverts_sha = extract_closure_facts_from_text(text)
    assert closes == []
    assert reverts_sha == "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def test_quoted_embedded_closes_outside_trailing_region_not_recorded():
    # A quoted prior commit message sitting in the body, separated from the
    # trailing region by an ordinary (non-trailer-shaped) paragraph -- the
    # DECISION-2 supersession's named hazard this bound must still close.
    text = (
        "Subject\n\n"
        "Quoting the original commit for context:\n\n"
        "    Original subject\n\n"
        "    Closes: RECS-99\n\n"
        "This is unrelated explanatory body text.\n\n"
        "Commit-Token: abc123\n"
    )
    closes, _ = extract_closure_facts_from_text(text)
    assert closes == []


def test_unindented_quoted_closes_outside_trailing_region_not_recorded():
    """The sibling above indents its quoted ``Closes:`` by four spaces, so
    ``_CLOSES_LINE_RE`` (anchored ``^Closes:``, no leading-whitespace
    allowance) rejects it on the line pattern alone and the trailing-region
    walk is never reached -- it passes without exercising the bound it names.

    This one quotes the line UNINDENTED, so the line pattern matches and the
    paragraph walk is the only thing standing between it and a false close
    row. That walk is the whole of DECISION-2's replacement guarantee, so it
    needs a test that fails when it regresses. Found by review of C1.
    """
    text = (
        "Subject\n\n"
        "Quoting the original commit for context:\n\n"
        "Original subject\n\n"
        "Closes: RECS-99\n\n"
        "This is unrelated explanatory body text.\n\n"
        "Commit-Token: abc123\n"
    )
    closes, _ = extract_closure_facts_from_text(text)
    assert closes == [], (
        "an unindented Closes: quoted in the body, separated from the trailing "
        "region by a prose paragraph, must not record -- the paragraph walk is "
        "what closes the hazard DECISION-2's structural guarantee used to close"
    )


def test_empty_text_returns_empty():
    assert extract_closure_facts_from_text("") == ([], None)
    assert extract_closure_facts_from_text("   \n\n  \n") == ([], None)


def test_extract_closure_facts_reads_file(tmp_path):
    msg_file = tmp_path / "COMMIT_EDITMSG"
    msg_file.write_text("Subject\n\nCloses: RECS-7\n\nCommit-Token: abc123\n", encoding="utf-8")
    closes, reverts_sha = extract_closure_facts(msg_file)
    assert closes == ["RECS-7"]
    assert reverts_sha is None


def test_extract_closure_facts_missing_file_degrades():
    closes, reverts_sha = extract_closure_facts("/does/not/exist/COMMIT_EDITMSG")
    assert closes == []
    assert reverts_sha is None

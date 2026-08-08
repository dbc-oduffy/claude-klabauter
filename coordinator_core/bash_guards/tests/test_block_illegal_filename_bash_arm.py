"""Regression coverage for `block_illegal_filename.py`'s Bash-arm quote-aware
redirect-target extraction (C3,
`docs/plans/2026-08-07-deny-legs-reachable-and-quoted-redirects-visible.md`).

Before C3, `check()` built `cmd_for_scan` by stripping double- then
single-quoted spans and scanning THAT for `>`/`>>` targets, which erased any
quoted redirect target wholesale -- `echo x > "bad?name.txt"` was silent
while `echo x > bad?name.txt` fired. C3 replaces `_extract_redir_candidates`
with a quote-AWARE scan over the quote-INTACT text: a `>` only counts as an
operator at quote-depth 0 with an allowed preceding character, and the
captured target may itself be a quoted span (including embedded spaces).

This module had no dedicated test file before C3 (dispatched brief item).

Negative-spec:
  - Does NOT cover the RELIABLE arm (`coordinator_core.write_guards.block_illegal_filename`)
    -- that is a separate module file, out of this chunk's scope.
  - Does NOT exercise the PowerShell dialect SILENT gate or the `mv`/`--out`
    candidate sources beyond what already existed -- this file targets the
    C3 redirect-extraction regression specifically (plan AC6/AC7/AC9).

Spec backlink: docs/plans/2026-08-07-deny-legs-reachable-and-quoted-redirects-visible.md, C3
"""
from __future__ import annotations

import itertools

import pytest

from coordinator_core.bash_guards import block_illegal_filename as m


def _payload(command: str) -> dict:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": "/repo",  # abs-path-ok: fixture-only synthetic cwd, not a real path
    }


def _fires(command: str) -> bool:
    out = m.check(_payload(command))
    return out is not None


def test_unquoted_illegal_redirect_target_still_fires():
    assert _fires('echo x > bad?name.txt')


def test_double_quoted_illegal_redirect_target_no_space_fires():
    assert _fires('echo x > "bad?name.txt"')


def test_double_quoted_illegal_redirect_target_with_space_fires():
    assert _fires('echo x > "X:/repo/bx17 bad:name?.txt"')


def test_single_quoted_illegal_redirect_target_fires():
    assert _fires("echo x > 'bad?name.txt'")


def test_double_quoted_legal_redirect_target_stays_silent():
    assert not _fires('echo x > "fine_name.txt"')


def test_single_quoted_legal_redirect_target_stays_silent():
    assert not _fires("echo x > 'fine_name.txt'")


def test_arrow_thin_exclusion_still_applies():
    assert not _fires('echo "a -> b?"')
    assert not _fires('some_dsl_call foo->bar?')
    assert not _fires('some_dsl_call foo=>bar?')


def test_git_commit_quoted_gt_in_message_stays_silent_ac9():
    """AC9 -- the regression the rejected quote-blind-scan-over-cmd approach
    reintroduces: a '>' that is merely quoted ARGUMENT content (inside the
    `-m` message string) must never be treated as a redirect operator."""
    assert not _fires('git commit -m "see foo > bar?"')


def test_git_commit_unquoted_example_stays_silent():
    assert not _fires('git commit -m "fixes a > b"')


def test_advisory_never_denies_when_it_fires():
    out = m.check(_payload('echo x > "bad?name.txt"'))
    assert out is not None
    hso = out.get("hookSpecificOutput", {})
    assert hso.get("permissionDecision") != "deny"


def test_extract_redir_candidates_captures_spaced_quoted_target_whole():
    candidates = m._extract_redir_candidates('echo x > "a b?.txt"')
    assert candidates == ['"a b?.txt"']


def test_extract_redir_candidates_skips_quoted_gt_as_argument():
    candidates = m._extract_redir_candidates('git commit -m "see foo > bar?"')
    assert candidates == []


# ---------------------------------------------------------------------------
# Backslash-escaped-quote regression (dispatched follow-up to C3): a `\"`
# inside a double-quoted span (or `\'` inside a single-quoted span) must NOT
# toggle quote depth. Before this fix the scanner desynced on the escaped
# quote, believed itself at depth 0, and captured a runaway candidate
# spanning to end-of-input.
# ---------------------------------------------------------------------------


def test_escaped_quote_in_commit_message_stays_silent_ab_matches_head():
    """The exact repro command from the dispatch brief -- SILENT at HEAD,
    must remain SILENT after the fix (A/B parity)."""
    assert not _fires('git commit -m "see foo > bar?"')


def test_mv_dest_still_caught_alongside_escaped_quote_fix():
    assert _fires('mv a.txt b?.txt')


def test_escaped_double_quote_inside_message_does_not_desync():
    assert not _fires('echo "he said \\"foo > bar?\\" loudly"')


def test_escaped_single_quote_inside_message_does_not_desync():
    """Retargeted (review: MAJOR-3) -- the original probe,
    ``echo 'it\\'s > fine'``, is malformed bash (the trailing quote after
    ``fine`` is never closed, a real syntax error) so it exercised nothing
    about escaping. Bash never honours backslash escapes inside ``'...'``:
    in ``'a\\'``, the span closes at the quote immediately after the
    backslash, not after skipping it as an escape. This probe is valid bash
    (the closed span is ``a\\``, then a genuine ``>`` operator follows) and
    must still fire on the real target."""
    assert _fires("echo 'a\\' > bad?.txt")


def test_redirect_target_after_escaped_quote_still_extracts_cleanly():
    assert _fires('echo hi > "out?.txt"')
    assert not _fires('echo hi > "out.txt"')


def test_extract_redir_candidates_honours_escaped_double_quote():
    candidates = m._extract_redir_candidates(
        'echo "he said \\"foo > bar?\\" loudly"'
    )
    assert candidates == []


def test_extract_redir_candidates_honours_escaped_single_quote():
    candidates = m._extract_redir_candidates("echo 'it\\'s > fine'")
    assert candidates == []


def test_unterminated_quote_yields_no_candidate_deliberately():
    """Requirement 3 -- an unparseable/unterminated quote must be silent,
    never a guess, and never a runaway candidate."""
    candidates = m._extract_redir_candidates('echo "unterminated > x')
    assert candidates == []
    assert not _fires('echo "unterminated > x')


# ---------------------------------------------------------------------------
# Escaped-backslash regression (dispatched follow-up to the escaped-quote
# fix above): a bare `\\` (escaped backslash) inside a quoted span must
# consume as a literal PAIR, leaving the quote that follows it a REAL
# delimiter -- not mistakenly treated as an escaped quote. The "only skip if
# next char is a quote" rule got this backwards: it read the `"` after `\\`
# as escaped, never closed the span, and swallowed the genuine redirect that
# followed -- a false negative vs. HEAD. Repro commands from the dispatch
# brief; the control proves the isolation (still fires, unaffected).
# ---------------------------------------------------------------------------


def test_escaped_backslash_before_close_quote_still_fires():
    assert _fires('echo "\\\\" > tricky?.txt')


def test_escaped_backslash_after_text_before_close_quote_still_fires():
    assert _fires('echo "a\\\\" > bad?.txt')


def test_escaped_backslash_in_single_quoted_span_still_fires():
    assert _fires("echo 'x\\\\' > worse?.txt")


def test_plain_control_command_still_fires():
    """Control, proving the isolation of the three repro commands above --
    an escaped-backslash-free command must still fire unaffected."""
    assert _fires('echo "plain" > alsobad?.txt')


def test_extract_redir_candidates_survives_escaped_backslash_before_close_quote():
    candidates = m._extract_redir_candidates('echo "\\\\" > tricky?.txt')
    assert candidates == ["tricky?.txt"]


def test_extract_redir_candidates_survives_escaped_backslash_in_single_quotes():
    candidates = m._extract_redir_candidates("echo 'x\\\\' > worse?.txt")
    assert candidates == ["worse?.txt"]


def test_no_candidate_ever_spans_a_newline_or_is_unreasonably_long():
    """Guards against a future quote-depth desync silently emitting a
    multi-line/huge blob instead of failing loudly (empty candidate list).

    Includes multi-line probes (review: MAJOR-1) -- the prior probe list was
    entirely single-line, so ``"\\n" not in candidate`` was vacuously true
    (a candidate cannot contain a newline if the input never does). These
    two fail against pre-fix HEAD (BLOCKER-1: an unquoted target ran past
    its own line and captured the next command) and pass after the
    ``_REDIR_TARGET_STOP`` fix."""
    probes = [
        'git commit -m "see foo > bar?"',
        'echo "he said \\"foo > bar?\\" loudly"',
        "echo 'it\\'s > fine'",
        'echo "unterminated > x',
        'echo x > "a b?.txt"',
        'echo hi > "out?.txt"',
        "echo hi > out.txt\nls",
        "a > x.txt\nb > y.txt\nc",
    ]
    for cmd in probes:
        for candidate in m._extract_redir_candidates(cmd):
            assert "\n" not in candidate
            assert len(candidate) < 200


# ---------------------------------------------------------------------------
# Operator-form coverage (review: MINOR-2) -- '&>' and '>|' were silently
# unrecognised (missing from _REDIR_PRECEDING_OK / target-capture handling
# respectively); '2>&1' and 'echo err >&2' must stay silent throughout.
# ---------------------------------------------------------------------------


def test_combined_redirect_operator_fires():
    assert _fires('cmd &> bad?.txt')


def test_noclobber_override_operator_fires():
    assert _fires('echo x >| bad?.txt')


def test_fd_duplication_2_greater_and_1_stays_silent():
    assert not _fires('2>&1')


def test_fd_duplication_greater_and_2_stays_silent():
    assert not _fires('echo err >&2')


# Review: coordinator:code-reviewer (49482a06) P3 -- the two tests above only
# assert indirectly via `_fires()`, which is silent regardless of whether
# `_extract_redir_candidates` returns `[]` or some benign-but-nonempty
# candidate -- neither `2>&1` nor `>&2` contains a character `_check_candidate`
# treats as illegal, so `_fires()` can't distinguish "extracted nothing" from
# "extracted something harmless". Assert against the extractor directly: `&`
# is a member of `_REDIR_TARGET_STOP`, so target capture must break
# immediately and emit no candidate at all.
def test_extract_redir_candidates_fd_duplication_2_greater_1_yields_no_candidate():
    assert m._extract_redir_candidates('2>&1') == []


def test_extract_redir_candidates_fd_duplication_greater_2_yields_no_candidate():
    assert m._extract_redir_candidates('echo err >&2') == []


# Review: coordinator:code-reviewer (49482a06) P3 -- process-substitution
# bodies (`(`/`)`) were added to `_REDIR_TARGET_STOP` so a construct like
# `tee >(grep foo) < in` cannot have its `>(...)` treated as a redirect
# target; this had zero regression coverage.
def test_extract_redir_candidates_process_substitution_yields_no_candidate():
    cmd = "tee >(grep foo" + _QM + ") < in"
    assert m._extract_redir_candidates(cmd) == []


# ---------------------------------------------------------------------------
# Escape/quote/target cross-product matrix (review: MAJOR-2) -- the commit
# message that introduced this file claimed a "60-case escape/quote/target
# matrix built with chr()"; no such matrix was in the tree (27 tests, no
# chr()). This lands one, using chr() so the harness's own literal-escaping
# introduces no artefacts of its own, covering the classes the review
# findings name: multi-line targets, single- vs double-quoted spans, escaped
# backslash before a closing quote, escaped quote, unterminated quote, and a
# '>' inside a quoted argument that is not a redirect.
# ---------------------------------------------------------------------------

_DQ = chr(34)  # "
_SQ = chr(39)  # '
_BS = chr(92)  # backslash
_NL = chr(10)  # newline
_GT = chr(62)  # >
_SP = chr(32)  # space
_QM = chr(63)  # ?


def _quoted(quote_char, body):
    if quote_char is None:
        return body
    return quote_char + body + quote_char


def _quote_target_matrix():
    cases = []
    for quote_char, (body, illegal) in itertools.product(
        (None, _SQ, _DQ),
        (("fine.txt", False), ("bad" + _QM + ".txt", True)),
    ):
        target = _quoted(quote_char, body)
        cases.append(("echo x" + _SP + _GT + _SP + target, illegal))
    return cases


_MATRIX_CASES = _quote_target_matrix() + [
    # multi-line unquoted target must terminate at the newline (BLOCKER-1).
    ("echo hi" + _SP + _GT + _SP + "out.txt" + _NL + "ls", False),
    (
        "a" + _SP + _GT + _SP + "x.txt" + _NL
        + "b" + _SP + _GT + _SP + "y" + _QM + ".txt" + _NL + "c",
        True,
    ),
    # escaped backslash immediately before a closing double-quote must not
    # desync -- the pair consumes as a literal '\\', leaving the quote a
    # real delimiter, and the genuine redirect after it still fires.
    (
        "echo" + _SP + _DQ + _BS + _BS + _DQ + _SP + _GT + _SP
        + "tricky" + _QM + ".txt",
        True,
    ),
    # escaped quote inside a double-quoted argument: the '>' inside stays
    # argument content, not an operator -- must stay silent.
    (
        "git commit -m" + _SP + _DQ + "see foo" + _SP + _GT + _SP + "bar"
        + _QM + _DQ,
        False,
    ),
    # unterminated quote -- deliberate silence, never a guess.
    ("echo" + _SP + _DQ + "unterminated" + _SP + _GT + _SP + "x", False),
    # '>' inside a quoted argument that is not a redirect at all.
    ("echo" + _SP + _DQ + "a" + _SP + _GT + _SP + "b" + _DQ, False),
]


@pytest.mark.parametrize("cmd,expected_fires", _MATRIX_CASES)
def test_redir_escape_quote_target_matrix(cmd, expected_fires):
    assert _fires(cmd) is expected_fires

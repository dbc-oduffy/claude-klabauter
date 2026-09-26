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

Spec backlink: pln-the-platform-conditioned-deny-9c8e07, C3
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
        "cwd": "/repo",
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


def test_escaped_quote_in_commit_message_stays_silent_ab_matches_head():
    assert not _fires('git commit -m "see foo > bar?"')


def test_mv_dest_still_caught_alongside_escaped_quote_fix():
    assert _fires('mv a.txt b?.txt')


def test_mv_dest_double_quoted_no_space_fires():
    """C3 follow-up (2026-08-07 bug-backlog record ccced871da89): the dest
    leg was left reading the quote-STRIPPED `cmd_for_scan`, which erased a
    quoted mv destination before tokenization ever saw it -- silent false
    negative on HEAD prior to this fix."""
    assert _fires('mv a.txt "b?.txt"')


def test_mv_dest_quoted_source_with_space_still_fires():
    assert _fires('mv "a a.txt" "b?.txt"')


def test_escaped_double_quote_inside_message_does_not_desync():
    assert not _fires('echo "he said \\"foo > bar?\\" loudly"')


def test_escaped_single_quote_inside_message_does_not_desync():
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
    candidates = m._extract_redir_candidates('echo "unterminated > x')
    assert candidates == []
    assert not _fires('echo "unterminated > x')


def test_escaped_backslash_before_close_quote_still_fires():
    assert _fires('echo "\\\\" > tricky?.txt')


def test_escaped_backslash_after_text_before_close_quote_still_fires():
    assert _fires('echo "a\\\\" > bad?.txt')


def test_escaped_backslash_in_single_quoted_span_still_fires():
    assert _fires("echo 'x\\\\' > worse?.txt")


def test_plain_control_command_still_fires():
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


# unrecognised (missing from _REDIR_PRECEDING_OK / target-capture handling


def test_combined_redirect_operator_fires():
    assert _fires('cmd &> bad?.txt')


def test_noclobber_override_operator_fires():
    assert _fires('echo x >| bad?.txt')


def test_fd_duplication_2_greater_and_1_stays_silent():
    assert not _fires('2>&1')


def test_fd_duplication_greater_and_2_stays_silent():
    assert not _fires('echo err >&2')


# is a member of `_REDIR_TARGET_STOP`, so target capture must break
def test_extract_redir_candidates_fd_duplication_2_greater_1_yields_no_candidate():
    assert m._extract_redir_candidates('2>&1') == []


def test_extract_redir_candidates_fd_duplication_greater_2_yields_no_candidate():
    assert m._extract_redir_candidates('echo err >&2') == []


# bodies (`(`/`)`) were added to `_REDIR_TARGET_STOP` so a construct like
def test_extract_redir_candidates_process_substitution_yields_no_candidate():
    cmd = "tee >(grep foo" + _QM + ") < in"
    assert m._extract_redir_candidates(cmd) == []


_DQ = chr(34)
_SQ = chr(39)
_BS = chr(92)
_NL = chr(10)
_GT = chr(62)
_SP = chr(32)
_QM = chr(63)


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
    (
        "echo" + _SP + _DQ + _BS + _BS + _DQ + _SP + _GT + _SP
        + "tricky" + _QM + ".txt",
        True,
    ),
    (
        "git commit -m" + _SP + _DQ + "see foo" + _SP + _GT + _SP + "bar"
        + _QM + _DQ,
        False,
    ),
    ("echo" + _SP + _DQ + "unterminated" + _SP + _GT + _SP + "x", False),
    ("echo" + _SP + _DQ + "a" + _SP + _GT + _SP + "b" + _DQ, False),
]


@pytest.mark.parametrize("cmd,expected_fires", _MATRIX_CASES)
def test_redir_escape_quote_target_matrix(cmd, expected_fires):
    assert _fires(cmd) is expected_fires


def test_illegal_redirect_target_returns_updated_input_with_sanitized_name():
    out = m.check(_payload('echo x > bad?name.txt'))
    assert out is not None
    hso = out.get("hookSpecificOutput", {})
    assert "permissionDecision" not in hso
    updated = hso.get("updatedInput")
    assert updated is not None
    assert updated["command"] == 'echo x > bad-name.txt'


def test_updated_input_preserves_other_tool_input_keys():
    payload = _payload('echo x > bad?name.txt')
    payload["tool_input"]["description"] = "keep me"
    out = m.check(payload)
    updated = out["hookSpecificOutput"]["updatedInput"]
    assert updated["description"] == "keep me"


def test_updated_input_still_carries_advisory_context():
    # Merge note: this asserted the literal "ADVISORY". The register on
    out = m.check(_payload('echo x > bad?name.txt'))
    hso = out["hookSpecificOutput"]
    assert "additionalContext" in hso
    ctx = hso["additionalContext"]
    assert "bad?name.txt" in ctx, "the context must name what was wrong"
    assert "bad-name.txt" in ctx, "the context must name what it was changed to"


def test_updated_input_still_never_denies():
    out = m.check(_payload('echo x > bad?name.txt'))
    hso = out.get("hookSpecificOutput", {})
    assert hso.get("permissionDecision") != "deny"


def test_mv_dest_illegal_name_returns_updated_input():
    out = m.check(_payload('mv a.txt b?.txt'))
    hso = out["hookSpecificOutput"]
    assert hso["updatedInput"]["command"] == 'mv a.txt b-.txt'


def test_quoted_redirect_target_updated_input_preserves_quoting():
    out = m.check(_payload('echo x > "bad?name.txt"'))
    hso = out["hookSpecificOutput"]
    assert hso["updatedInput"]["command"] == 'echo x > "bad-name.txt"'


def test_rewrite_context_does_not_ask_agent_to_act():
    out = m.check(_payload('echo x > "bad?name.txt"'))
    ctx = out["hookSpecificOutput"].get("additionalContext", "")
    assert "Use instead" not in ctx
    assert "Auto-corrected" in ctx


@pytest.mark.parametrize(
    "command",
    [
        "grep -o 'a.*b' file.txt > out.txt",
        'grep -rn -o "x*y" src | sort > hits.txt',
        "cd /tmp && rg -o '[a-z]+?' . > found.log",
        "git grep -o 'foo*' > matches.txt",
    ],
)
def test_grep_family_dash_o_pattern_is_never_a_candidate_or_rewritten(command):
    assert m._extract_out_candidates(command) == []
    assert not _fires(command)


def test_sort_dash_o_output_path_still_fires():
    assert m._extract_out_candidates("sort -o 'bad?name.txt' in.txt > log.txt") == ["'bad?name.txt'"]
    assert _fires("sort -o 'bad?name.txt' in.txt > log.txt")


def test_dash_o_inside_a_word_is_not_a_flag():
    assert m._extract_out_candidates("echo foo-o bad?x > ok.txt") == []

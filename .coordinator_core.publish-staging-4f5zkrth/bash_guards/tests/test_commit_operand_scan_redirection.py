"""Regression test for the 2026-08-05 project-opticon incident: a
surviving shell-redirection token was being read by
`_bt_commit_operand_scan` as a `git commit` pathspec OPERAND, so an
UNSCOPED `git commit` carrying any redirection (`git commit -q -F -
<<'MSG' ... MSG`, the real incident shape) was misclassified as
"already explicitly scoped" and the sweep-commit advisory/deny never
fired.

Spec backlink: dispatch brief "Break-class fix, PM-authorized... a
shipped safety guard goes SILENT on the exact command shape it exists
to catch" (2026-08-05).

Negative-spec: this module does NOT introduce a second redirection
matcher. `_bt_commit_operand_scan` (and therefore both
`_bt_commit_has_explicit_pathspec` and `_bt_commit_own_pathspec`, which
share that one walk) now filters operands through the SAME
`_bt_is_redirection_token` predicate `_bt_has_redirection` already used
pre-fix for the unrelated grep/ls/find BX-16 rewrite declines -- there
remains exactly one definition of "is a redirection token" in
`dispatch_checks.py`.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import dispatch_checks


#: Every redirection spelling named in the dispatch brief's required
#: matrix, plus the verbatim opticon shape as its own named case so a
#: future reader cannot delete it as an arbitrary string (brief
#: requirement). Each entry is `(label, redirection_token)`, already
#: shlex-tokenized the same way `dispatch_checks`'s shared tokenizer
#: would split it (verified live via `shlex.split`, POSIX mode, for
#: every row below).
REDIRECTION_TOKEN_MATRIX = [
    # `<<'W'` and `<<W` are the identical post-shlex token -- quotes are
    # stripped by shlex, so a single "heredoc" row covers both spellings
    # (2026-08-05 follow-up review, Finding 3).
    ("heredoc", "<<W"),
    ("herestring", "<<<w"),
    ("stdout-truncate", ">f"),
    ("stdout-append", ">>f"),
    ("stderr-to-stdout", "2>&1"),
    ("stderr-to-file", "2>f"),
    ("stdin-from-file", "<f"),
    ("stdout-stderr-combined", "&>f"),
    ("fd-dup-explicit-source", "1>&2"),
    ("heredoc-strip-tabs", "<<-W"),
]


#: Whitespace-separated redirections (2026-08-05 follow-up review,
#: Finding 1): `_bt_tokenize_full_command` doesn't include `>`/`<` in its
#: `punctuation_chars`, so a spaced redirection tokenizes as TWO tokens --
#: the bare operator, then its target as a separate plain token. Each
#: entry is `(label, operator_token, target_token)`.
SPACED_REDIRECTION_MATRIX = [
    ("stdout-truncate-spaced", ">", "/dev/null"),
    ("stderr-to-file-spaced", "2>", "log"),
    ("stdin-from-file-spaced", "<", "input"),
]


@pytest.mark.parametrize("label,redir_tok", REDIRECTION_TOKEN_MATRIX)
def test_unscoped_commit_with_redirection_is_not_treated_as_scoped(
    label: str, redir_tok: str
) -> None:
    """The bug: an unscoped `git commit` whose ONLY trailing token is a
    surviving redirection operator must NOT read as explicitly scoped --
    pre-fix, `_bt_commit_operand_scan` counted `redir_tok` itself as a
    pathspec operand and `_bt_commit_has_explicit_pathspec` returned
    `True` (wrongly suppressing the advisory/deny)."""
    tokens = ["git", "commit", "-m", "subj", redir_tok]
    assert dispatch_checks._bt_commit_has_explicit_pathspec(tokens) is False, (
        f"{label}: unscoped commit + {redir_tok!r} must NOT read as scoped"
    )
    # `_bt_commit_own_pathspec` shares the same walk (`_bt_commit_operand_
    # scan`) and inherited the identical misread -- must return `None`
    # (nothing to narrow Check 5's staged-set intersection by), never the
    # redirection token as a "pathspec".
    assert dispatch_checks._bt_commit_own_pathspec(tokens) is None, (
        f"{label}: unscoped commit + {redir_tok!r} must yield no own-pathspec"
    )


@pytest.mark.parametrize("label,redir_tok", REDIRECTION_TOKEN_MATRIX)
def test_genuinely_scoped_commit_with_redirection_still_reads_as_scoped(
    label: str, redir_tok: str
) -> None:
    """The other polarity: a commit that DOES carry an explicit `--
    <paths>` scope must keep reading as scoped even when a redirection
    token trails it -- the redirection token must be filtered OUT of the
    operand list, not merely tolerated by accident, so `['a.txt']` (not
    `['a.txt', redir_tok]`) is what `_bt_commit_own_pathspec` returns."""
    tokens = ["git", "commit", "-m", "subj", "--", "a.txt", redir_tok]
    assert dispatch_checks._bt_commit_has_explicit_pathspec(tokens) is True, (
        f"{label}: scoped commit + {redir_tok!r} must still read as scoped"
    )
    assert dispatch_checks._bt_commit_own_pathspec(tokens) == ["a.txt"], (
        f"{label}: scoped commit + {redir_tok!r} must narrow to the real "
        "pathspec only, not the redirection token"
    )


@pytest.mark.parametrize(
    "label,op_tok,target_tok", SPACED_REDIRECTION_MATRIX
)
def test_unscoped_commit_with_spaced_redirection_is_not_treated_as_scoped(
    label: str, op_tok: str, target_tok: str
) -> None:
    """The spaced-redirection bug (Finding 1): an unscoped `git commit`
    trailed by a BARE operator token and a separate target token must NOT
    read as scoped -- pre-fix, the operator was filtered but the target
    token survived into `operands`, making `_bt_commit_has_explicit_
    pathspec` wrongly return `True`."""
    tokens = ["git", "commit", "-m", "subj", op_tok, target_tok]
    assert dispatch_checks._bt_commit_has_explicit_pathspec(tokens) is False, (
        f"{label}: unscoped commit + {op_tok!r} {target_tok!r} must NOT "
        "read as scoped"
    )
    assert dispatch_checks._bt_commit_own_pathspec(tokens) is None, (
        f"{label}: unscoped commit + {op_tok!r} {target_tok!r} must yield "
        "no own-pathspec"
    )


@pytest.mark.parametrize(
    "label,op_tok,target_tok", SPACED_REDIRECTION_MATRIX
)
def test_genuinely_scoped_commit_with_spaced_redirection_still_reads_as_scoped(
    label: str, op_tok: str, target_tok: str
) -> None:
    """The other polarity for the spaced form: a commit with an explicit
    `-- <paths>` scope must keep reading as scoped, and the redirection's
    OWN target token must not pollute the pathspec -- `['a.txt']`, not
    `['a.txt', target_tok]`."""
    tokens = ["git", "commit", "-m", "subj", "--", "a.txt", op_tok, target_tok]
    assert dispatch_checks._bt_commit_has_explicit_pathspec(tokens) is True, (
        f"{label}: scoped commit + {op_tok!r} {target_tok!r} must still "
        "read as scoped"
    )
    assert dispatch_checks._bt_commit_own_pathspec(tokens) == ["a.txt"], (
        f"{label}: scoped commit + {op_tok!r} {target_tok!r} must narrow "
        "to the real pathspec only, not the redirection's operator or "
        "target"
    )


def test_no_redirection_baseline_unscoped_stays_unscoped() -> None:
    """Control row: no redirection token at all -- must behave exactly as
    before this fix (bare `-m` commit is unscoped, no operands)."""
    tokens = ["git", "commit", "-m", "subj"]
    assert dispatch_checks._bt_commit_has_explicit_pathspec(tokens) is False
    assert dispatch_checks._bt_commit_own_pathspec(tokens) is None


def test_no_redirection_baseline_scoped_stays_scoped() -> None:
    """Control row: no redirection token at all, explicit pathspec --
    must behave exactly as before this fix."""
    tokens = ["git", "commit", "-m", "subj", "--", "a.txt"]
    assert dispatch_checks._bt_commit_has_explicit_pathspec(tokens) is True
    assert dispatch_checks._bt_commit_own_pathspec(tokens) == ["a.txt"]


def test_opticon_incident_shape_verbatim() -> None:
    """The exact real-world shape from the 2026-08-05 project-opticon
    incident: `git commit -q -F - <<'MSG' ... MSG`, tokenized (shlex
    strips the heredoc delimiter's quoting to `<<MSG`, one token,
    verified live). Pre-fix this read as explicitly scoped
    (`_bt_commit_operand_scan` counted `-` and `<<MSG` as pathspec
    operands) and silenced the advisory that should have caught the
    bystander session's commit absorbing a peer's staged rename. Do NOT
    delete this case as an arbitrary string -- it is the incident."""
    tokens = ["git", "commit", "-q", "-F", "-", "<<MSG"]
    assert dispatch_checks._bt_commit_has_explicit_pathspec(tokens) is False
    assert dispatch_checks._bt_commit_own_pathspec(tokens) is None


#: End-to-end confirmation through the full public advisory entrypoint,
#: for the two shapes the dispatch brief called out explicitly by name.
INTEGRATION_TABLE = [
    (
        "git commit -q -F - <<'MSG'",
        True,
        "the real incident command: unscoped + a surviving heredoc marker "
        "must still fire the advisory/deny, not read as scoped",
    ),
    (
        'git commit -m subj >/dev/null',
        True,
        "unscoped + stdout redirection must still fire",
    ),
    (
        'git commit -m subj 2>&1',
        True,
        "unscoped + stderr-to-stdout redirection must still fire",
    ),
    (
        'git commit -m subj <file',
        True,
        "unscoped + stdin redirection must still fire",
    ),
    (
        'git commit -m subj',
        True,
        "unscoped, no redirection at all -- baseline, still fires",
    ),
    (
        'git commit -m subj -- a.txt',
        False,
        "explicit pathspec, no redirection -- baseline, stays silent",
    ),
    (
        'git commit -m subj -- a.txt >/dev/null',
        False,
        "explicit pathspec WITH redirection must stay silent -- the "
        "other required polarity",
    ),
    (
        'git commit -m subj > /dev/null',
        True,
        "unscoped + spaced stdout redirection must still fire "
        "(2026-08-05 follow-up review, Finding 1)",
    ),
    (
        'git commit -m subj 2> log',
        True,
        "unscoped + spaced stderr redirection must still fire",
    ),
    (
        'git commit -m subj < input',
        True,
        "unscoped + spaced stdin redirection must still fire",
    ),
    (
        'git commit -m subj -- a.txt > /dev/null',
        False,
        "explicit pathspec WITH spaced redirection must stay silent",
    ),
]


@pytest.mark.parametrize("cmd,should_fire,note", INTEGRATION_TABLE)
def test_full_advisory_entrypoint_end_to_end(
    cmd: str, should_fire: bool, note: str
) -> None:
    fires = (
        dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-redir")
        is not None
    )
    assert fires is should_fire, f"{cmd!r}: {note}"

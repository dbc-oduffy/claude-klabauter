"""AC2's firing-shape oracle for `check_git_commit_safe_commit_advise`
(`dispatch_checks.py`).

Spec backlink: pln-advisory-firing-shape-predicat-802b35 § C1a, AC1, AC2, AC10.

`test_deny_message_accuracy.py`'s `TestGitCommitSafeCommitAdviseMessageAccuracy`
already covers MESSAGE accuracy (does the emitted text describe what tripped
it, does the suggestion carry a real scope) -- see that file's own module
docstring: "for every deny/advisory path in scope, EXECUTE it and assert the
emitted message...". This module's remit is narrower and different in kind:
a per-row FIRING-SHAPE table, one expected verdict per command shape, with no
claim about message text at all. Placed as a new sibling module rather than
folded into that class because the two properties (verdict vs. message) drift
independently -- a shape can flip its verdict while the message stays
accurate, and vice versa.

Existing coverage inventory (do NOT re-author, per C1a's body): three of the
rows below already exist verbatim or near-verbatim in that file's
`SCOPED_FORMS`/`UNSCOPED_FORMS` (the `git -C` scoped form, the `&&`-joined
both-halves-scoped form, `-am` as an unscoped-only bundled form) -- annotated
per-row below via the ``existing_coverage`` field rather than skipped, since
AC2 asks for a complete table, not a diff against the other file.

Negative-spec: this module does NOT assert on advisory/deny TEXT (that is
`test_deny_message_accuracy.py`'s job) and does NOT itself probe the git
index -- `_fires` only asks whether the check returned non-`None`, so it is
insensitive to C7's own index probe (bare-commit-half escalation) or its
2026-08-15 solo-bare-commit sibling (both live in `test_git_commit_safe_
commit_deny_escalation.py`, C7's own module). A row's ``fires`` bit is the
ONLY thing under test; a row lacking `-C <repo>` runs the ambient probe
against whatever cwd this test process happens to have, which can only
flip a `None` firing-shape row's verdict between advisory and deny, never
between firing and silent.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import dispatch_checks


def _fires(cmd: str) -> bool:
    return dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-c1a") is not None


#: (command, fires, existing_coverage, note, fix_group)
#: ``existing_coverage`` names the near-variant already pinned in
#: `test_deny_message_accuracy.py`'s SCOPED_FORMS/UNSCOPED_FORMS, or ``None``
#: for a shape genuinely new to this chunk. ``fix_group`` tags which fix (or
#: pre-existing baseline shape) the row belongs to, so
#: `test_table_is_bidirectional_where_applicable` can verify pairing
#: PER GROUP rather than just "some row somewhere fires."
FIRING_SHAPE_TABLE = [
    # --- Unconditional baseline (964fad0a, unchanged by this chunk) ---
    (
        'git commit -m "x" -- one/file.md',
        False,
        "SCOPED_FORMS[0] (near-variant)",
        "explicit trailing pathspec is the ratified scoped form; stays silent",
        "baseline",
    ),
    (
        'git add -- one/file.md && git commit -m "x"',
        True,
        None,
        "bare commit half after a scoped add -- PM Ruling 1: NOT doctrinally "
        "correct, stays firing, no index-probe suppression exists anywhere",
        "baseline",
    ),
    # --- git -C / bundled short-flag / segment-join shapes (AC2 explicit) ---
    (
        'git -C /tmp/repo commit -m "x" -- one/file.md',
        False,
        "SCOPED_FORMS[3]",
        "git -C with a scoped commit half stays silent",
        "dash-C-bundled-segment-join",
    ),
    (
        "git -C /tmp/repo commit -m 'x'",
        True,
        None,
        "git -C with NO scope still fires -- -C alone is not scope",
        "dash-C-bundled-segment-join",
    ),
    (
        "git commit -sm 'x' -- one/file.md",
        False,
        None,
        "bundled short-flag (-sm) WITH a scoped tail stays silent -- the "
        "existing UNSCOPED_FORMS only pins -am as unscoped, never a scoped "
        "bundled-short-flag variant",
        "dash-C-bundled-segment-join",
    ),
    (
        "git commit -sm 'x'",
        True,
        "UNSCOPED_FORMS[1] (near-variant, -am not -sm)",
        "bundled short-flag with no scope fires",
        "dash-C-bundled-segment-join",
    ),
    (
        'true && git commit -m "x" -- one/file.md',
        False,
        "SCOPED_FORMS[2] (near-variant: that row scopes BOTH halves; this "
        "row's leading segment carries no pathspec at all)",
        "&&-joined segment where only the commit half is scoped stays silent",
        "dash-C-bundled-segment-join",
    ),
    (
        'true; git commit -m "x" -- one/file.md',
        False,
        None,
        ";-joined segment with a scoped commit half stays silent",
        "dash-C-bundled-segment-join",
    ),
    (
        'true || git commit -m "x" -- one/file.md',
        False,
        None,
        "||-joined segment with a scoped commit half stays silent",
        "dash-C-bundled-segment-join",
    ),
    # --- Fix 1: --pathspec-from-file / --pathspec-file-nul (real over-fire) ---
    (
        "git commit -m 'x' --pathspec-from-file list.txt",
        False,
        None,
        "space-separated --pathspec-from-file is git's documented equivalent "
        "of naming paths on the command line (man git-commit(1)) -- explicit "
        "scope, must not fire post-fix",
        "fix1-pathspec-from-file",
    ),
    (
        "git commit -m 'x' --pathspec-from-file=list.txt",
        False,
        None,
        "the =-joined form is a SINGLE token that never matched "
        "_GIT_COMMIT_OPT_WITH_ARG pre-fix -- explicit scope, must not fire",
        "fix1-pathspec-from-file",
    ),
    (
        "git commit -m 'x' --pathspec-from-file=list.txt --pathspec-file-nul",
        False,
        None,
        "--pathspec-file-nul is a genuine addition (appeared nowhere in the "
        "option set pre-fix) -- must not fire",
        "fix1-pathspec-from-file",
    ),
    (
        "git commit -m 'x'",
        True,
        "UNSCOPED_FORMS[0]",
        "control row: no pathspec flag at all still fires",
        "fix1-pathspec-from-file",
    ),
    # --- -o/--only: same index-bypassing self-scoped mode (real over-fire) ---
    (
        "git commit -o one/file.md -m 'x'",
        False,
        None,
        "-o selects git's identical index-bypassing self-scoped mode as a "
        "trailing -- <paths> (verified live: peer's staged path stays "
        "staged) -- explicit scope, must not fire",
        "only-flag-scope",
    ),
    (
        "git commit --only -m 'x' one/file.md",
        False,
        None,
        "the long form is the same mode -- must not fire",
        "only-flag-scope",
    ),
    (
        "git commit -om 'x' one/file.md",
        False,
        None,
        "-o bundles into a short-flag cluster like any other short flag "
        "(verified live) -- must not fire",
        "only-flag-scope",
    ),
    (
        "git commit -i -m 'x' one/file.md",
        True,
        None,
        "NEGATIVE SPEC: --include MERGES the named paths into the staged "
        "index and commits the union (verified live: a peer's staged path "
        "landed in HEAD) -- exactly the sweep this advisory catches",
        "only-flag-scope",
    ),
    (
        "git commit -m -o",
        True,
        None,
        "-o as the -m VALUE is not scope -- the option-value pair-skip runs "
        "before the only-flag check",
        "only-flag-scope",
    ),
    # --- SC-DR-020: the separator disambiguates, it does not scope ---
    # Every row above that establishes scope does it with `-- <paths>`, which
    # is exactly why the mirror-image gap in _bt_git_add_own_pathspec went
    # uncovered until a live sweep found it. These rows pin the UNSEPARATED
    # spelling so the same blind spot cannot re-form in this predicate.
    (
        "git commit one/file.md -m 'x'",
        False,
        None,
        "SC-DR-020: a bare positional pathspec is the same operation as "
        "-- <paths> (verified live on git 2.54.0: with a peer's b.txt also "
        "staged, one/file.md lands alone and b.txt stays staged) -- explicit "
        "scope, must not fire",
        "sc-dr-020-positional-pathspec",
    ),
    (
        "git commit -m 'x' one/file.md",
        False,
        None,
        "operand AFTER the message operand is still an operand -- the "
        "option-value pair-skip must not swallow it",
        "sc-dr-020-positional-pathspec",
    ),
    (
        "git commit -i one/file.md -m 'x'",
        True,
        None,
        "BOUND: --include is untouched by SC-DR-020 -- it merges into the "
        "index and commits the union, so it must keep firing even though it "
        "now carries a positional operand the scan can see",
        "sc-dr-020-positional-pathspec",
    ),
    (
        "git commit -S one/file.md -m 'x'",
        True,
        None,
        "BOUND 4 (fail open): a STANDALONE -S consumes nothing (it is an "
        "OPTIONAL-keyid flag; a separate token is never its value), so the "
        "parser cannot statically resolve it and marks the parse ambiguous "
        "via the short-cluster path -- ambiguity yields advisory, never "
        "suppression. (Passed for the WRONG reason pre-fix: -S used to sit "
        "in _GIT_COMMIT_OPT_WITH_ARG and unconditionally swallow "
        "one/file.md as its phantom value, leaving zero operands -- this "
        "row's verdict was accidental, not derived from ambiguity.)",
        "sc-dr-020-positional-pathspec",
    ),
    (
        "git commit --frobnicate one/file.md -m 'x'",
        True,
        None,
        "BOUND 4 (fail open): an unrecognized option may consume the next "
        "token -- must not suppress on a parse it cannot prove",
        "sc-dr-020-positional-pathspec",
    ),
    (
        "git commit -a one/file.md -m 'x'",
        True,
        None,
        "BOUND: -a stages from the WORKTREE -- a positional operand does not "
        "make a sweep-all commit scoped",
        "sc-dr-020-positional-pathspec",
    ),
    # --- Fix 2: piped git commit segment (real under-fire) ---
    (
        'echo y | git commit -m "x"',
        True,
        None,
        "a git commit reached through a pipe is inspected like any other "
        "segment post-fix -- pre-fix this was silent (pipe_before skip)",
        "fix2-piped-segment",
    ),
    (
        'echo y | git commit -m "x" -- one/file.md',
        False,
        None,
        "piped AND scoped stays silent -- Fix 2 only removes the pipe skip, "
        "it does not change the scope predicate itself",
        "fix2-piped-segment",
    ),
    # --- Fix 3: env-var / wrapper-prefixed git commit (real under-fire) ---
    (
        'GIT_INDEX_FILE=/tmp/i git commit -m "x"',
        True,
        None,
        "env-var-assignment-prefixed bare commit fires post-fix -- this is "
        "the exact shape the advisory's own message names (GIT_INDEX_FILE) "
        "as needing scoped-git-commit, and it silently bypassed pre-fix",
        "fix3-env-wrapper",
    ),
    (
        'GIT_INDEX_FILE=/tmp/i git commit -m "x" -- one/file.md',
        False,
        None,
        "env-var-prefixed AND scoped stays silent",
        "fix3-env-wrapper",
    ),
    (
        'nice git commit -m "x"',
        True,
        None,
        "wrapper-word-prefixed (nice) bare commit fires post-fix",
        "fix3-env-wrapper",
    ),
    (
        'nice git commit -m "x" -- one/file.md',
        False,
        None,
        "wrapper-word-prefixed AND scoped stays silent",
        "fix3-env-wrapper",
    ),
    # --- Review finding 1 (P0): -S/--gpg-sign standalone must not
    # unconditionally consume the next token (they are OPTIONAL-argument
    # flags whose value must be ATTACHED) -- a standalone occurrence must
    # render the parse ambiguous and fire, never fabricate a pathspec out
    # of the next unrelated token and suppress. ---
    (
        'git commit -S -m "x"',
        True,
        None,
        "standalone -S must not swallow -m's VALUE as its own phantom "
        "value and fake a pathspec out of it -- the exact false-suppression "
        "the P0 finding named; must fire",
        "review-finding1-gpg-sign",
    ),
    (
        'git commit --gpg-sign -m "x"',
        True,
        None,
        "long form of the same standalone-optional-arg bug -- must fire",
        "review-finding1-gpg-sign",
    ),
    (
        'git commit -Skeyid -m "x" foo.py',
        False,
        None,
        "the ATTACHED form (-Skeyid) is a single, non-ambiguous token "
        "consuming nothing further -- foo.py resolves cleanly as scope, "
        "stays silent",
        "review-finding1-gpg-sign",
    ),
    (
        'git commit --gpg-sign=keyid -m "x" foo.py',
        False,
        None,
        "the =-joined long attached form resolves cleanly the same way -- "
        "stays silent",
        "review-finding1-gpg-sign",
    ),
    # --- Review finding 2 (P2): the operand-scan anchor must be a
    # global-option-aware positional walk, not a literal `.index("commit")`
    # search that a `-C`-style global option's VALUE can collide with. ---
    (
        "git -C commit commit -m x",
        True,
        None,
        "the LITERAL string 'commit' appearing as -C's value must not "
        "mis-anchor the scan onto the wrong token -- the real commit "
        "subcommand has no scope, so this fires",
        "review-finding2-dash-C-value-collision",
    ),
    (
        "git -C commit commit -m 'x' -- one/file.md",
        False,
        None,
        "same -C-value-collision shape but with a genuinely scoped commit "
        "half -- must resolve the real subcommand and stay silent",
        "review-finding2-dash-C-value-collision",
    ),
    # --- Review finding 3 (nit): -t/--template is a real mandatory-arg
    # option; must resolve cleanly rather than fall to spurious ambiguity. ---
    (
        "git commit -t tmpl.txt -m 'x' foo.py",
        False,
        None,
        "-t/--template takes a separate-token value -- must resolve "
        "cleanly (not ambiguous) so foo.py is correctly read as scope",
        "review-finding3-template",
    ),
    (
        "git commit -t tmpl.txt -m 'x'",
        True,
        None,
        "control row: -t consumes its value but leaves no pathspec -- "
        "still fires (unscoped)",
        "review-finding3-template",
    ),
]


@pytest.mark.parametrize(
    "cmd, fires, existing_coverage, note, fix_group",
    FIRING_SHAPE_TABLE,
    ids=[row[0] for row in FIRING_SHAPE_TABLE],
)
def test_firing_shape_row(cmd, fires, existing_coverage, note, fix_group):
    assert _fires(cmd) is fires, "%s -- %s" % (cmd, note)


def test_table_is_bidirectional_where_applicable():
    """AC2: each fixed shape is asserted in BOTH directions (scoped stays
    silent, unscoped fires) -- verified PER fix_group, not merely "some row
    somewhere fires and some row somewhere stays silent" (a table that lost
    every silent companion row for one fix, while keeping them for the
    others, would still pass the weaker check)."""
    groups: dict[str, set[bool]] = {}
    for _cmd, fires, _existing_coverage, _note, fix_group in FIRING_SHAPE_TABLE:
        groups.setdefault(fix_group, set()).add(fires)
    assert groups, "table must not be empty"
    for fix_group, verdicts in groups.items():
        assert verdicts == {True, False}, (
            "fix_group %r only carries verdict(s) %r -- expected both a "
            "fires and a silent row" % (fix_group, verdicts)
        )

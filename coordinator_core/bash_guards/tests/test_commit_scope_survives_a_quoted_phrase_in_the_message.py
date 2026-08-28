"""The commit-scope guard must not lose a correct `-- <path>` because the
COMMIT MESSAGE quotes a phrase.

Bug row: state/bug-backlog/2026-08-27-commit-scope-guard-denies-a-correct-
pathspec-when-the-message-quotes-a-phrase-8f31c0a4e7b2.yaml

The shape: `-m "$(cat <<'MSG' ... MSG\n)"`. A `"` inside the heredoc BODY closed
the outer double quote that opened at `-m "$(`, so the rest of the body lexed
unquoted and a `;` in the message subject was read as a real segment separator.
The command split in two, the `git commit` head landed in one segment and the
`-- <path>` in the other, and `_bt_commit_has_explicit_pathspec` correctly
reported "no pathspec" about a segment that genuinely had none. The detector was
never wrong; it was being handed a mis-cut command.

Fixed by stripping heredoc BODIES before segmenting, at this one check's seam --
the per-path precedent `resolve_command_positions` and `block_illegal_filename`
already set. `tokenize_full_command` itself stays `$(...)`-unaware for its ~33
other consumers, deliberately: a guard that must see commands inside a body
(`bash <<'EOF'`) would go blind if the strip were global. That wider boundary is
still open and is named in `_shell_c_unwrap_payloads`' own comment.

WHY BOTH DIRECTIONS ARE PINNED IN ONE MODULE, and why they must stay together.
The tempting fix was to relax `_bt_commit_has_explicit_pathspec` until the false
positive stopped. That would reintroduce the bare-commit hole
`coordinator_core/tests/test_no_pathspec_less_commit.py` (88832e9d4) closed, and
quietly, since the false positive would indeed be gone. So the negative case sits
in the same file as the positive one: any future fix has to satisfy both reads at
once, and a reviewer sees both by opening one module.

Asserted through the CHECK, not only through the tokenizer. The tokenizer-level
assertions below are kept as the diagnostic — they say WHERE a regression is —
but the contract this module defends is the guard's verdict, and a fix that
restored correct segmentation while breaking the verdict would otherwise pass.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards._command_tokenizer import (
    segments_from_tokens_simple,
    tokenize_full_command,
)
from coordinator_core.bash_guards.dispatch_checks import (
    _bt_commit_has_explicit_pathspec,
    _bt_strip_heredocs,
    check_git_commit_safe_commit_advise,
)

#: The message body carries BOTH ingredients the bug needs, and it needs both:
#: a `;` (a segment separator once quoting is lost) and a double-quoted phrase
#: (what loses the quoting). Either alone tokenizes fine, which is why the bug
#: went unnoticed until a commit message happened to contain the pair.
_MESSAGE_WITH_QUOTED_PHRASE = (
    "the attribution data exists; the access path does not\n"
    "\n"
    'Index shape is not: keyed by transcript file, so "did\n'
    'session S write path P" is a scan, not a lookup.\n'
)

_MESSAGE_PLAIN = (
    "the attribution data exists; the access path does not\n"
    "\n"
    "Index shape is not: keyed by transcript file, so asking\n"
    "whether session S wrote path P is a scan, not a lookup.\n"
)


def _commit_command(message: str, *, pathspec: str | None) -> str:
    """A `git commit` whose `-m` operand is a heredoc inside a command
    substitution -- the shape every long-form commit message in this repo
    uses, because `-F` has no pathspec escape."""
    tail = f" -- {pathspec}" if pathspec else ""
    return f"git commit -q -m \"$(cat <<'MSG'\n{message}MSG\n)\"{tail}"


def _fires(cmd: str) -> bool:
    """True iff the scope check returns a verdict (deny or advisory) rather
    than allowing the command through."""
    return check_git_commit_safe_commit_advise(cmd) is not None


def _scope_verdicts(cmd: str) -> list[bool]:
    """Per-segment answers to 'does this segment carry an explicit pathspec',
    over the same heredoc-stripped text the check now segments.

    Returned as a LIST rather than an any()/all() so a failure message shows
    the segmentation itself -- `[False, False]` is the original bug's
    signature (the command was cut in two), and is a different defect from a
    single `[False]` (one segment, genuinely unscoped).
    """
    tokens = tokenize_full_command(_bt_strip_heredocs(cmd))
    assert tokens is not None, "command failed to tokenize at all"
    return [
        _bt_commit_has_explicit_pathspec(seg)
        for seg in segments_from_tokens_simple(tokens)
    ]


def test_plain_message_with_pathspec_does_not_fire():
    """Control. The shape that always got through, establishing that the
    quoted phrase is the only variable in the test below."""
    cmd = _commit_command(_MESSAGE_PLAIN, pathspec="state/x.yaml")
    assert not _fires(cmd)
    assert _scope_verdicts(cmd) == [True]


def test_quoted_phrase_in_the_message_does_not_destroy_the_pathspec():
    """The bug. Identical command but for a quoted phrase in the message body;
    the pathspec is byte-identical and correct.

    Before the fix this DENIED, and its remediation named the very form the
    author had used -- which is worse than a nag: it teaches that the scoped
    form does not work, at a moment the fleet is being asked to trust it more.
    """
    cmd = _commit_command(_MESSAGE_WITH_QUOTED_PHRASE, pathspec="state/x.yaml")
    assert not _fires(cmd), (
        "the ratified both-halves scoped form was refused because the MESSAGE "
        "quoted a phrase -- see this module's docstring; the detector is not "
        "the thing to change"
    )
    assert _scope_verdicts(cmd) == [True]


def test_a_bare_commit_still_fires_however_its_message_is_quoted():
    """The half that must never regress. A fix that suppressed the false
    positive by loosening the detector would let THIS through, which is the
    bare-commit hole 88832e9d4 closed. No pathspec anywhere, in either message
    shape."""
    for message in (_MESSAGE_PLAIN, _MESSAGE_WITH_QUOTED_PHRASE):
        cmd = _commit_command(message, pathspec=None)
        assert _fires(cmd), (
            "a commit with no pathspec at all was allowed -- a fix for the "
            "false positive has weakened the detector instead of fixing "
            "segmentation"
        )
        assert not any(_scope_verdicts(cmd))


def test_heredoc_body_is_not_mistaken_for_a_pathspec():
    """Negative-spec on the fix itself. Stripping bodies must not let text
    INSIDE a heredoc supply the scope: a message that merely mentions a path,
    on a commit carrying no real pathspec, must still fire."""
    message = "subject; here\n\nthis body names state/x.yaml -- state/y.yaml\n"
    assert _fires(_commit_command(message, pathspec=None))


# The three shapes below are built rather than written inline: each carries a
# single quote, a double quote and a newline at once, and inline escaping of
# all three is how the fixture stops being readable. _Q is "'".
_Q = chr(39)

_MENTIONS_HEREDOC_AND_IS_SCOPED = (
    "git commit -m \"discussing the <<" + _Q + "EOF" + _Q + " pattern\n"
    "EOF\n"
    "rest of the message\" -- state/x.yaml"
)

_PATHSPEC_PHRASE_IN_A_PLAIN_MESSAGE = (
    "git commit -m \"we should use -- state/x.yaml here\""
)

_PATHSPEC_PHRASE_IN_A_HEREDOC_BODY = (
    "git commit -m \"$(cat <<" + _Q + "MSG" + _Q + "\n"
    "use -- state/x.yaml here\nMSG\n)\""
)

_PATHSPEC_PHRASE_AFTER_A_MENTIONED_TERMINATOR = (
    "git commit -m \"the <<" + _Q + "EOF" + _Q + " form\n"
    "EOF\nand -- state/x.yaml\""
)


class TestTheStripIsQuoteUnawareAndThatIsSafeHere:
    """The heredoc strip feeding this seam does NOT track quote state -- it is a
    textual scan, unlike every other walk in `_command_tokenizer` -- so a commit
    MESSAGE that merely MENTIONS a heredoc opener and later carries a bare
    terminator line has its body mangled on the way to the scope question.
    Raised as a P1 by review of `d33296c70..d62bbad65`, and the mechanism is
    real: verified in process, `_strip_heredocs` deletes the mentioned
    terminator line and injects a `;` in its place.

    THE CONSEQUENCE THE REPORT FEARED DOES NOT FOLLOW, and this class is where
    that is written down instead of re-derived by the next reader. The stripped
    text is a COPY used to answer ONE question -- does this command line carry a
    pathspec -- and is never the text that runs. Mangling the message inside
    that copy cannot change the answer, because the answer does not depend on
    the message.

    That is the entire safety argument for applying the strip here, so it is
    pinned in BOTH directions rather than left as prose: a message that talks
    about heredocs must not lose a real pathspec (the false-DENY direction,
    which is the original bug returning through its own fix), and a message
    that talks about pathspecs must not manufacture one (the false-ALLOW
    direction -- the dangerous half, and the one nothing else would announce).

    Neither case is exotic in this repo: the bug rows and commit messages
    describing this very fix are full of literal heredoc syntax.
    """

    def test_a_message_that_mentions_heredoc_syntax_keeps_its_pathspec(self):
        assert not _fires(_MENTIONS_HEREDOC_AND_IS_SCOPED), (
            "the strip mangles this message, but the trailing pathspec is "
            "untouched and the commit is scoped"
        )

    @pytest.mark.parametrize(
        "cmd",
        [
            _PATHSPEC_PHRASE_IN_A_PLAIN_MESSAGE,
            _PATHSPEC_PHRASE_IN_A_HEREDOC_BODY,
            _PATHSPEC_PHRASE_AFTER_A_MENTIONED_TERMINATOR,
        ],
    )
    def test_a_pathspec_shaped_phrase_never_supplies_scope(self, cmd):
        """The false-ALLOW direction. A path mentioned in prose is not a
        pathspec, however the strip rearranges the text around it.
        """
        assert _fires(cmd), (
            "a bare commit must still fire -- message text is never scope"
        )

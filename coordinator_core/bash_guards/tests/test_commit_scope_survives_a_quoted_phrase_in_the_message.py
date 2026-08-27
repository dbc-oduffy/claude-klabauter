"""The commit-scope guard must not lose a correct `-- <path>` because the
COMMIT MESSAGE quotes a phrase.

Bug row: state/bug-backlog/2026-08-27-commit-scope-guard-denies-a-correct-
pathspec-when-the-message-quotes-a-phrase-8f31c0a4e7b2.yaml

The shape: `-m "$(cat <<'MSG' ... MSG\n)"`. A `"` inside the heredoc BODY closes
the outer double quote that opened at `-m "$(`, so the rest of the body lexes
unquoted and a `;` in the message subject is read as a real segment separator.
The command splits in two, the `git commit` head lands in one segment and the
`-- <path>` in the other, and the scope check correctly reports "no pathspec"
about a segment that genuinely has none. The detector is right; the input is
mis-cut. `dispatch_checks.py` already names the underlying cause -- its own
comment reads "`tokenize_full_command` has no `$(...)`-aware grouping (a
shared-tokenizer scope boundary, not fixed here)".

WHY BOTH DIRECTIONS ARE PINNED IN ONE MODULE, and why they must stay together.
The tempting fix is to relax `_bt_commit_has_explicit_pathspec` until the false
positive stops. That would reintroduce the bare-commit hole
`coordinator_core/tests/test_no_pathspec_less_commit.py` (88832e9d4) just closed,
and it would do so quietly, since the false positive would indeed be gone. So the
negative case sits in the same file as the positive one: whatever fix lands has to
satisfy both reads at once, and a reviewer sees both by opening one module.

`designed_red`: the ALLOW half fails until the fix lands, and its failure output
IS the worklist. It is deliberately not `pending_fix` -- nothing here is awaiting
a mechanical retouch; the disposition (narrow-scope strip vs making the shared
tokenizer substitution-aware) is an open engineering call recorded on the row.
The DENY half passes today and must never start failing.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards._command_tokenizer import (
    segments_from_tokens_simple,
    tokenize_full_command,
)
from coordinator_core.bash_guards.dispatch_checks import (
    _bt_commit_has_explicit_pathspec,
)

#: The message body carries BOTH ingredients the bug needs, and it needs both:
#: a `;` (a segment separator once quoting is lost) and a double-quoted phrase
#: (what loses the quoting). Either alone tokenizes fine.
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


def _scope_verdicts(cmd: str) -> list[bool]:
    """Per-segment answers to 'does this segment carry an explicit pathspec'.

    Returned as a LIST rather than an any()/all() so a failure message shows
    the segmentation itself -- `[False, False]` is the bug's signature (the
    command was cut in two), and is a different defect from a single
    `[False]` (one segment, genuinely unscoped).
    """
    tokens = tokenize_full_command(cmd)
    assert tokens is not None, "command failed to tokenize at all"
    return [
        _bt_commit_has_explicit_pathspec(seg)
        for seg in segments_from_tokens_simple(tokens)
    ]


def test_plain_message_with_pathspec_is_scoped():
    """Control. Same command, same pathspec, no quoted phrase in the body --
    this is the shape that gets through today, and its passing is what
    establishes that the quoted phrase is the only variable below."""
    cmd = _commit_command(_MESSAGE_PLAIN, pathspec="state/x.yaml")
    assert _scope_verdicts(cmd) == [True]


@pytest.mark.designed_red
def test_quoted_phrase_in_the_message_does_not_destroy_the_pathspec():
    """The bug. Identical command but for a quoted phrase in the message body;
    the pathspec is byte-identical and correct.

    Expected once fixed: exactly one segment, carrying the pathspec.
    Observed today: two segments, neither carrying it -- so the guard denies a
    correctly scoped commit and its remediation names the very form the author
    used."""
    cmd = _commit_command(_MESSAGE_WITH_QUOTED_PHRASE, pathspec="state/x.yaml")
    assert _scope_verdicts(cmd) == [True]


def test_a_bare_commit_is_still_unscoped_however_its_message_is_quoted():
    """The half that must never regress. A fix that suppresses the false
    positive by loosening the detector would make THIS read as scoped, which
    is the bare-commit hole 88832e9d4 closed. No pathspec anywhere in the
    command, in either message shape."""
    for message in (_MESSAGE_PLAIN, _MESSAGE_WITH_QUOTED_PHRASE):
        verdicts = _scope_verdicts(_commit_command(message, pathspec=None))
        assert not any(verdicts), (
            "a commit with no pathspec at all read as scoped -- a fix for the "
            "false positive has weakened the detector instead of fixing "
            "segmentation"
        )

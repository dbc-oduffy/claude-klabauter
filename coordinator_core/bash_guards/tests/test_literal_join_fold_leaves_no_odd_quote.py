"""A surviving odd quote separates a governed name as well as two did.

Purpose: a LIVE, ALLOWED Bash call on 2026-08-31 created a file named for a
governed doctrine surface. Not a predicate probe -- a real tool call through
the armed hook, and the file was on disk afterwards:

    echo probe > <Q>$S/CLAUDE<Q><Q><Q>.md      ->  allowed, wrote the governed name

(`<Q>` stands for a double-quote character throughout this docstring: the
literal bytes cannot be written here, because the shape under test is three
adjacent double quotes and this is a triple-quoted string.)

`_fold_literal_joins` exists precisely to stop this: it collapses zero-width
literal joins so a name split across a concatenation reads as contiguous
text, and it already handled `'CLAU''DE.md'`. It folded PAIRS of quotes. The
command above carries THREE adjacent quotes; the pair rule consumed two and
left `claude".md`, which matches no governed identifier, while real bash
concatenated the lot.

This was one of three prefilter-evasion shapes reported on 2026-08-29 that no
shard had independently verified. Measured through the live hook, this
session: the quote-split pair is already CLOSED (blocked), this one
REPRODUCED, and variable expansion (`N=CLAUDE; ... > "$S/$N.md"`) also
reproduced and is NOT addressed here -- resolving it needs stem-level binding
and concatenation tracking, which is a design question, not a parser gap.

The fix is shell semantics rather than a heuristic: inside ONE word, quotes
are pure delimiters and every one of them is dropped, so `a"b"c` is the
single word `abc`. The whitespace guard that keeps `'a' 'b'` two arguments is
untouched and pinned below, because widening this fold in a way that invents
governed mentions in ordinary commands would be the worse trade.

Negative-spec: this file asserts on the PREFILTER (`_mentions_governed_
identifier`) and on the redirect-target read, never on a deny envelope. The
prefilter is the leg that failed -- the command never reached a sink leg at
all -- and the sink legs have their own corpora
(`test_guard_doctrine_surface_point3_*`, `test_guard_doctrine_surface_point4_
by_sink.py`). Folding is applied IN ADDITION to the raw text, so a wider fold
can only admit MORE commands to those legs, never fewer.

Governed names are assembled from fragments here rather than spelled: this
guard reads its own test file's literals when a session edits it through
Bash, and it is the guard's job to refuse that.
"""

from __future__ import annotations

from typing import Optional

import pytest

from coordinator_core.bash_guards.guard_doctrine_surface_bash_write import (
    _mentions_governed_identifier,
    _redirect_target_token,
)

_STEM = "CLAUDE"
_GOVERNED_MD = _STEM + ".md"
_IDENTIFIERS = (_GOVERNED_MD.lower(), "em-operating-" + "doctrine.md")


#: `(id, command, target)` -- every shape real bash resolves to a write of
#: the governed surface. `target` is what the redirect read must recover.
_JOINED_SHAPES = [
    pytest.param('echo p > "S/%s"' % _GOVERNED_MD, id="plain"),
    pytest.param('echo p > "S/CLAU""DE.md"', id="quote-split-pair-already-closed"),
    pytest.param('echo p > "S/%s"""".md"' % _STEM, id="empty-quote-splice-the-live-bypass"),
    pytest.param('echo p > "S/%s"""' % _STEM + ".md", id="odd-quote-then-unquoted-tail"),
    pytest.param('echo p > "S/%s"' % _STEM + '".md"', id="single-word-internal-quote"),
    pytest.param("echo p > 'S/CLAU' + 'DE.md'", id="explicit-plus-join"),
]


@pytest.mark.parametrize("command", _JOINED_SHAPES)
def test_a_joined_governed_name_reaches_the_prefilter(command: str) -> None:
    """The prefilter is what failed. A command real bash resolves to a
    governed write must be SEEN, whatever a sink leg then decides."""
    assert _mentions_governed_identifier(command, _IDENTIFIERS) is True


@pytest.mark.parametrize("command", _JOINED_SHAPES)
def test_the_redirect_target_recovers_the_whole_governed_name(command: str) -> None:
    """Seeing the mention is not enough -- the target read must recover the
    name CONTIGUOUSLY, or the sink legs get a truncated token that matches
    no identifier. A prior defect did exactly that (see
    `_redirect_target_token`'s own docstring: an unfolded read stopped at the
    first closing quote and a split name reached a real redirect target)."""
    target: Optional[str] = _redirect_target_token(command)
    assert target is not None
    assert _GOVERNED_MD.lower() in target.lower()


#: The guard the widened fold must not cost. Whitespace-separated words are
#: two arguments in shell; folding across them would invent governed
#: mentions in ordinary commands, and an invented mention on the hot path is
#: paid by every Bash call in the fleet.
_MUST_NOT_BE_INVENTED = [
    pytest.param("grep 'claude' 'md' docs/", id="two-quoted-args-with-space"),
    pytest.param("git status --short", id="no-quotes-at-all"),
    pytest.param("echo 'claude' && echo 'md'", id="separate-segments"),
]


@pytest.mark.parametrize("command", _MUST_NOT_BE_INVENTED)
def test_whitespace_separated_words_are_still_not_joined(command: str) -> None:
    assert _mentions_governed_identifier(command, _IDENTIFIERS) is False


def test_a_quoted_target_containing_spaces_still_parses() -> None:
    """A quote with whitespace beside it is not word-internal, so the
    widened rule leaves it alone and a spaced target still reads whole."""
    assert _redirect_target_token('echo p > "my notes.md"') == "my notes.md"

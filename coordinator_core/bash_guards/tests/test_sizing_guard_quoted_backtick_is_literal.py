"""A backtick or `$(` the shell treats as literal text is not command substitution.

Single quotes and a backslash-escaped backtick in double quotes are literal; an
unescaped backtick or `$(` that executes still denies a command naming a sizing.

Negative-spec: does not assert deny text.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as guard

IDS = guard._SIZING_RECORD_IDENTIFIERS
BASE = "sizing-assemble --tshirt M --write content-root:" + IDS[0] + "a.yaml"


@pytest.mark.parametrize(
    "arg",
    [
        '--intent "a \\`/compact\\` b"',
        "--intent 'a `/compact` b'",
        "--intent 'a $(date) b'",
        "--intent \"it's `x`\"".replace("`x`", "\\`x\\`"),
    ],
    ids=["escaped-in-double", "single-quoted", "single-quoted-dollar-paren", "apostrophe-in-double"],
)
def test_literal_backtick_or_dollar_paren_passes(arg):
    assert not guard.is_denied_bash_write(f"{BASE} {arg}", IDS)


@pytest.mark.parametrize(
    "arg",
    [
        '--intent "a `date` b"',
        "--intent a`date`b",
        '--intent "a $(date) b"',
        "--intent 'x' \"`date`\"",
    ],
    ids=["double-quoted-backtick", "bare-backtick", "double-quoted-dollar-paren", "after-single-quote"],
)
def test_live_command_substitution_still_denies(arg):
    assert guard.is_denied_bash_write(f"{BASE} {arg}", IDS)

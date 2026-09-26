"""A governed name split across an assignment boundary is still that name.

Purpose: `_mentions_governed_identifier` is a substring test, so it needs the
governed name to appear CONTIGUOUSLY. A command can split it across an
assignment and never satisfy that:

    N=<stem>; echo probe > "$SOME_DIR/$N.md"

The stem sits in an assignment and the suffix is a separate literal, so the
governed name appears nowhere, `is_denied_bash_write`'s opening gate returns
False, no leg of the guard is reached, and the write lands. Measured as a
LIVE bypass through the armed hook on 2026-08-31 -- allowed, with the
governed-named file on disk afterwards. One of three prefilter-evasion shapes
from `2026-08-29-doe-claude-em-prefilter-evasion-is-not-only-a-python-
concat.md`; the other two are closed at `f6222de006` and were already closed.

WHY THIS IS NOT A TABLE OF SHAPES. The backlog row that filed this
(`2026-08-31-variable-expansion-still-evades-the-doct-5764bcce7a61.yaml`)
was right that no enumeration closes the class, because a variable can be
assembled from arbitrary fragments. The fix does not enumerate: it resolves
the command to what it will actually RUN and lets the existing legs judge
that. Every fragment that decides the sink has to be present for the SHELL
to run it too, so following the assignments follows the class.

Negative-spec -- this file must never contain a governed identifier
contiguously in its own source. The guard reads the text of commands, and a
test file that spells one out cannot itself be written or committed through
the ordinary route. Every governed name below is assembled from fragments at
runtime, which is also why `_STEM`/`_SUFFIX` exist rather than a literal.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards.guard_doctrine_surface_bash_write import (
    _expand_local_assignments,
    _local_assignment_values,
    is_denied_bash_write,
)

_STEM = "CLAUDE"
_SUFFIX = ".md"
_GOVERNED = _STEM + _SUFFIX
_IDS = (_GOVERNED.lower(),)


def _denied(cmd: str) -> bool:
    return is_denied_bash_write(cmd, _IDS)


def test_the_live_bypass_is_closed() -> None:
    cmd = 'N=%s; echo probe > "$S/$N%s"' % (_STEM, _SUFFIX)
    assert _denied(cmd) is True


def test_the_braced_deref_form_is_closed() -> None:
    cmd = 'N=%s; echo probe > "$S/${N}%s"' % (_STEM, _SUFFIX)
    assert _denied(cmd) is True


def test_an_alias_chain_is_followed() -> None:
    cmd = 'A=%s; B=$A; echo x > "$B%s"' % (_STEM, _SUFFIX)
    assert _denied(cmd) is True


def test_the_suffix_may_come_from_a_variable_too() -> None:
    cmd = 'E=%s; echo x > "%s$E"' % (_SUFFIX, _STEM)
    assert _denied(cmd) is True


def test_a_contiguous_governed_write_still_denies() -> None:
    assert _denied("echo x > %s" % (_GOVERNED,)) is True


def test_an_unrelated_variable_write_is_untouched() -> None:
    cmd = 'N=notes; echo x > "$N%s"' % (_SUFFIX,)
    assert _denied(cmd) is False


def test_a_read_of_a_governed_name_via_a_variable_is_not_a_write() -> None:
    cmd = "N=%s; cat \"$N%s\"" % (_STEM, _SUFFIX)
    assert _denied(cmd) is False


@pytest.mark.parametrize(
    "value",
    ["$(echo x)", "`echo x`", "*", "gl?b"],
    ids=["cmd-subst", "backtick", "glob-star", "glob-question"],
)
def test_an_unknowable_value_is_not_guessed_at(value: str) -> None:
    cmd = 'N=%s; echo x > "$N%s"' % (value, _SUFFIX)
    assert _denied(cmd) is False


def test_an_environment_variable_this_command_did_not_assign_is_left_alone() -> None:
    """`$SOME_DIR` is not resolvable from the text and must stay literal.

    Correct, and not a gap: the directory does not decide governance -- the
    basename does. This asserts the expansion leaves it in place rather than
    dropping it, since dropping it would silently change the target."""
    expanded = _expand_local_assignments('N=%s; echo x > "$UNSET_DIR/$N%s"' % (_STEM, _SUFFIX))
    assert "$UNSET_DIR" in expanded
    assert _STEM + _SUFFIX in expanded


def test_expansion_is_a_no_op_without_assignments() -> None:
    cmd = "echo hello > out.txt"
    assert _expand_local_assignments(cmd) == cmd


def test_only_the_first_token_of_an_assignment_is_the_value() -> None:
    assert _local_assignment_values(["N=foo bar"]) == {"N": "foo"}


def test_quoted_assignment_values_are_unwrapped() -> None:
    values = _local_assignment_values(['N="%s"' % (_STEM,), "M='%s'" % (_STEM,)])
    assert values == {"N": _STEM, "M": _STEM}


def test_expansion_terminates_on_a_self_referential_assignment() -> None:
    assert _expand_local_assignments("A=$B; B=$A; echo x") is not None

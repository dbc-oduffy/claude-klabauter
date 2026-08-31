"""C5: the for-loop wrapper around `find` is the same spawn storm.

Purpose: `for f in $(find . -name "*.txt"); do rm "$f"; done` forks one
process per match exactly as `find ... -exec rm {} \\;` does. Until C5 the
guard saw the shape (`_BT_Shape.FOR_LOOP` is admitted by
`check_find_exec_rewrite`) and then dropped it, because
`_bt_parse_find_exec_segment` requires a literal `-exec` token a
for-loop-wrapped find never carries.

Negative-spec -- this is NOT a general loop-body parser and these tests pin
that. `_bt_parse_for_loop_find` recognises ONE canonical shape and returns
None for everything else; general command-substitution rewriting belongs to
`guard_grep_via_bash._substitutable_rewrite` and is not duplicated here.
The refusal rows below are the load-bearing half of this file: a parser that
guessed at a richer body would emit a `find` command that does something
other than what the operator wrote.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards.dispatch_checks import (
    _bt_for_loop_find_batch_rewrite,
    _bt_parse_for_loop_find,
    check_find_exec_rewrite,
)


def _tok(cmd: str):
    from coordinator_core.bash_guards.dispatch_checks import _bt_classify_command

    return _bt_classify_command(cmd).tokens


# --------------------------------------------------------------------------
# The canonical shape parses.
# --------------------------------------------------------------------------


def test_the_canonical_shape_parses() -> None:
    parsed = _bt_parse_for_loop_find(_tok('for f in $(find . -name "*.txt"); do rm "$f"; done'))
    assert parsed is not None
    assert parsed["path"] == "."
    assert parsed["name_pattern"] == "*.txt"
    assert parsed["verb_argv"] == ["rm"]


def test_a_find_with_no_name_predicate_parses() -> None:
    parsed = _bt_parse_for_loop_find(_tok("for f in $(find src); do rm $f; done"))
    assert parsed is not None
    assert parsed["path"] == "src"
    assert parsed["name_pattern"] is None


def test_a_multi_token_verb_parses() -> None:
    parsed = _bt_parse_for_loop_find(_tok('for f in $(find . -name "*.py"); do git add "$f"; done'))
    assert parsed is not None
    assert parsed["verb_argv"] == ["git", "add"]


def test_the_braced_deref_parses() -> None:
    parsed = _bt_parse_for_loop_find(_tok('for f in $(find . -name "*.txt"); do rm "${f}"; done'))
    assert parsed is not None


# --------------------------------------------------------------------------
# The batched offer.
# --------------------------------------------------------------------------


def test_the_batched_form_is_offered_for_a_measured_verb() -> None:
    parsed = _bt_parse_for_loop_find(_tok('for f in $(find . -name "*.txt"); do rm "$f"; done'))
    assert parsed is not None
    assert _bt_for_loop_find_batch_rewrite(parsed) == "find . -name '*.txt' -exec rm {} +"


def test_no_name_predicate_yields_no_name_predicate() -> None:
    parsed = _bt_parse_for_loop_find(_tok("for f in $(find src); do rm $f; done"))
    assert parsed is not None
    assert _bt_for_loop_find_batch_rewrite(parsed) == "find src -exec rm {} +"


def test_a_verb_off_the_measured_list_gets_no_batch_form() -> None:
    """The allowlist is C2's MEASURED set. An unmeasured verb gets silence,
    never a guessed `+` -- batching changes invocation grouping, and for a
    verb nobody measured that can change output."""
    parsed = _bt_parse_for_loop_find(_tok('for f in $(find . -name "*.txt"); do frobnicate "$f"; done'))
    assert parsed is not None
    assert _bt_for_loop_find_batch_rewrite(parsed) is None


def test_git_without_add_gets_no_batch_form() -> None:
    parsed = _bt_parse_for_loop_find(_tok('for f in $(find . -name "*.txt"); do git rm "$f"; done'))
    assert parsed is not None
    assert _bt_for_loop_find_batch_rewrite(parsed) is None


# --------------------------------------------------------------------------
# Refusals -- the negative spec.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        'for f in $(ls); do rm "$f"; done',
        'for f in a b c; do rm "$f"; done',
        'for f in $(find . -name "*.txt"); do rm "$f" && echo done; done',
        'for f in $(find . -name "*.txt"); do rm "$f" | tee log; done',
        'for f in $(find . -name "*.txt"); do echo "$f" > out; done',
        'for f in $(find . -name "*.txt"); do rm "$f"; echo x; done',
        'for f in $(find . -name "*.txt"); do rm "$f" -rf; done',
        'for f in $(find . -newer ref); do rm "$f"; done',
        'for f in $(find . -name "*.txt"); do rm "$g"; done',
        'for f in $(find $(pwd) -name "*.txt"); do rm "$f"; done',
        "while read f; do rm $f; done",
    ],
    ids=[
        "not-find",
        "literal-list",
        "chained-body",
        "piped-body",
        "redirecting-body",
        "two-command-body",
        "operand-not-final",
        "unmodelled-find-option",
        "wrong-variable",
        "nested-substitution",
        "not-a-for-loop",
    ],
)
def test_a_shape_this_parser_does_not_model_is_refused(cmd: str) -> None:
    assert _bt_parse_for_loop_find(_tok(cmd)) is None


# --------------------------------------------------------------------------
# End to end through the guard.
# --------------------------------------------------------------------------


def test_the_guard_now_rewrites_the_loop() -> None:
    result = check_find_exec_rewrite('for f in $(find . -name "*.txt"); do rm "$f"; done')
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
    updated = result["hookSpecificOutput"]["updatedInput"]["command"]
    assert updated == "find . -name '*.txt' -exec rm {} +"


def test_an_unmeasured_verb_gets_an_advisory_not_a_rewrite() -> None:
    result = check_find_exec_rewrite('for f in $(find . -name "*.txt"); do frobnicate "$f"; done')
    assert result is not None
    assert "updatedInput" not in result["hookSpecificOutput"]
    assert "frobnicate" in result["hookSpecificOutput"]["additionalContext"]


def test_a_chained_loop_is_advised_never_wholesale_replaced() -> None:
    """The BX-12 lesson: a rewrite replaces the WHOLE command, so a loop
    that is not the whole command may only be advised about."""
    cmd = 'for f in $(find . -name "*.txt"); do rm "$f"; done; echo finished'
    result = check_find_exec_rewrite(cmd)
    assert result is not None
    assert "updatedInput" not in result["hookSpecificOutput"]


def test_an_ordinary_find_exec_is_unaffected() -> None:
    """The counterpart: C5 must not have disturbed the `-exec` path."""
    result = check_find_exec_rewrite("find . -name '*.txt' -exec rm {} \\;")
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_an_unrelated_for_loop_is_silent() -> None:
    assert check_find_exec_rewrite("for i in 1 2 3; do echo $i; done") is None

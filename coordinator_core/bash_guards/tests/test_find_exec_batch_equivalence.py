"""The `+`-terminated `-exec` shape is not the `\\;` shape it was told it was.

Purpose: `_bt_parse_find_exec_segment` computed `plus_idx` only to bound
`exec_argv`, and returned a dict carrying no terminator. Every downstream
branch in `check_find_exec_rewrite` therefore told `-exec CMD {} +` the same
"forks one process PER MATCH -- the founding-incident 879-process shape on
Windows" story it tells `-exec CMD {} \\;`, byte-identically.

Measured 2026-08-31 (GNU findutils 4.11.0), 3 matches: `\\;` produced 3
invocations, `+` produced 1. The claim is false of the `+` shape, and the
guard was offering to fix a problem that shape does not have.

This chunk adds `terminator: "semi" | "plus"` to the parsed dict and makes
`check_find_exec_rewrite` a SILENT allow for the `+` shape -- no advisory,
no rewrite, no message at all. An already-batched command is not this
guard's business.

Negative-spec: this file does not shell out to a real `find` (matching
`test_find_exec_rewrite_output_equivalence.py`'s own precedent) and does not
assert anything about the `\\;` shape's rewrite CONTENT -- that is the other
file's job. It asserts two things only: the parser records which terminator
it saw, and the guard says nothing at all for the `+` form while still
speaking for the `\\;` form.
"""

from __future__ import annotations

import re
import shlex

from coordinator_core.bash_guards.dispatch_checks import (
    _FIND_EXEC_BATCH_EQUIVALENT_VERBS,
    _FIND_EXEC_TRANSLATABLE_VERBS,
    _bt_find_exec_batch_rewrite,
    _bt_parse_find_exec_segment,
    check_find_exec_rewrite,
)


def test_parser_records_plus_terminator() -> None:
    tokens = shlex.split("find . -name '*.txt' -exec rm {} +")
    parsed = _bt_parse_find_exec_segment(tokens)
    assert parsed is not None
    assert parsed["terminator"] == "plus"


def test_parser_records_semi_terminator() -> None:
    tokens = shlex.split("find . -name '*.txt' -exec rm {} \\;")
    parsed = _bt_parse_find_exec_segment(tokens)
    assert parsed is not None
    assert parsed["terminator"] == "semi"


def test_already_batched_plus_form_gets_no_advisory_or_rewrite() -> None:
    """The falsifier: an already-batched command is silently allowed."""
    result = check_find_exec_rewrite("find . -name '*.txt' -exec rm {} +")
    assert result is None


def test_unbatched_semi_form_still_fires() -> None:
    """The counterpart -- the guard's voice for the per-match shape is
    unchanged by this fix; only the `+` shape went silent."""
    result = check_find_exec_rewrite("find . -name '*.txt' -exec rm {} \\;")
    assert result is not None


def test_plus_form_message_never_claims_per_match_forking() -> None:
    """Even if some future change re-adds a message for the `+` shape, it
    must never carry the per-match-fork claim this chunk falsified -- belt
    and suspenders alongside the silent-allow assertion above."""
    result = check_find_exec_rewrite("find . -name '*.txt' -exec rm {} +")
    if result is not None:
        message = str(result.get("reason", "")) + str(result.get("message", ""))
        assert "PER MATCH" not in message


# ---------------------------------------------------------------------------
# C2: the unknown-verb advisory branch offers `+` as an actually-runnable
# batched alternative for verbs measured (2026-08-31, GNU findutils 4.11.0)
# to be batch-equivalent -- `_FIND_EXEC_BATCH_EQUIVALENT_VERBS`, gated on
# `{}` being the FINAL token of the exec'd argv. Verbs already handled by
# `_FIND_EXEC_TRANSLATABLE_VERBS` (rm/cat/wc) are shadowed by that earlier
# branch inside `check_find_exec_rewrite` -- this chunk does not reorder or
# touch that priority, so the `+` offering is only reachable end-to-end
# through the guard for a verb NOT on the translatable table. The unit
# helper `_bt_find_exec_batch_rewrite` is exercised directly for rm/cat to
# pin the underlying batch-equivalence table independent of that shadowing.
#
# Negative-spec: no shell-out to a real `find` (same precedent as the rest
# of this file); non-batchable verbs and non-final `{}` are pinned to
# produce NO suggestion and NO rewrite, not merely "a different one".
# ---------------------------------------------------------------------------


def test_translatable_verbs_pinned_unchanged() -> None:
    """This chunk MUST NOT touch `_FIND_EXEC_TRANSLATABLE_VERBS` -- a
    different table answering a different question (see module comment
    above `_bt_find_exec_batch_rewrite` in dispatch_checks.py)."""
    assert _FIND_EXEC_TRANSLATABLE_VERBS == frozenset({"rm", "cat", "wc"})


def test_batch_equivalent_verbs_seeded_from_measured_table() -> None:
    assert _FIND_EXEC_BATCH_EQUIVALENT_VERBS == frozenset(
        {"rm", "cat", "chmod", "chown", "touch", "git"}
    )


def test_chmod_gets_plus_form_offered_end_to_end() -> None:
    """chmod is not on the translation table, so the unknown-verb branch is
    reached and must offer the POSIX `+` form as an actually-runnable
    alternative -- an `updatedInput`, not prose describing a shape."""
    result = check_find_exec_rewrite("find . -name '*.txt' -exec chmod 644 {} \\;")
    assert result is not None
    updated = result.get("updatedInput") or result.get("hookSpecificOutput", {}).get(
        "updatedInput"
    )
    command = _extract_command(result)
    assert command is not None
    assert "+" in command
    assert "\\;" not in command


def test_chown_and_touch_get_plus_form_offered() -> None:
    for verb_cmd in ("chown user {}", "touch {}"):
        result = check_find_exec_rewrite("find . -exec %s \\;" % verb_cmd)
        assert result is not None
        command = _extract_command(result)
        assert command is not None
        assert command.rstrip().endswith("+")


def test_git_add_gets_plus_form_offered() -> None:
    result = check_find_exec_rewrite("find . -name '*.py' -exec git add {} \\;")
    assert result is not None
    command = _extract_command(result)
    assert command is not None
    assert command.rstrip().endswith("+")


def test_git_rm_not_on_allowlist_no_plus_offered() -> None:
    """Only `git add` was measured -- any other git subcommand must get the
    unchanged prose advisory, no suggestion, no rewrite."""
    result = check_find_exec_rewrite("find . -exec git rm {} \\;")
    assert result is not None
    command = _extract_command(result)
    assert command is None


def test_head_tail_wc_grep_not_batch_equivalent_no_plus_offered() -> None:
    """Measured non-batchable: head/tail add banners, wc appends a total
    line, grep prefixes hits with the path -- none may be offered `+`."""
    for cmd in (
        "find . -exec head {} \\;",
        "find . -exec tail {} \\;",
        "find . -exec wc {} \\;",
        "find . -exec grep foo {} \\;",
    ):
        result = check_find_exec_rewrite(cmd)
        assert result is not None
        command = _extract_command(result)
        assert command is None


def test_non_final_placeholder_no_plus_offered() -> None:
    """`{}` must be the FINAL token of the exec'd argv for `+` to be a valid
    batch of the `;` form -- a mid-argv placeholder is a different shape and
    gets no suggestion."""
    tokens = shlex.split("find . -exec chmod {} 644 \\;")
    parsed = _bt_parse_find_exec_segment(tokens)
    assert parsed is not None
    assert _bt_find_exec_batch_rewrite(tokens, parsed) is None


def test_unit_helper_batch_rewrite_for_translatable_verbs() -> None:
    """rm/cat are shadowed end-to-end by the translation branch, but the
    underlying batch-equivalence table still covers them -- pinned directly
    against the unit helper."""
    for verb in ("rm", "cat"):
        tokens = shlex.split("find . -exec %s {} \\;" % verb)
        parsed = _bt_parse_find_exec_segment(tokens)
        assert parsed is not None
        rewritten = _bt_find_exec_batch_rewrite(tokens, parsed)
        assert rewritten is not None
        assert rewritten.rstrip().endswith("+")
        assert verb in rewritten


def _extract_command(result: dict) -> "str | None":
    """Pull the rewritten/suggested command out of either an
    `_allow_rewrite` (`updatedInput.command`) or `_advisory` (embedded in
    the message text) result shape -- returns `None` if neither carries a
    `+`-terminated find command."""
    hook_output = result.get("hookSpecificOutput") or {}
    updated = hook_output.get("updatedInput")
    if isinstance(updated, dict) and updated.get("command"):
        return str(updated["command"])
    message = str(hook_output.get("additionalContext", ""))
    # The advisory embeds the batched form parenthesised, and the command
    # itself contains single quotes (`-exec rm '{}' +`), so splitting the
    # message on `'` shreds it. Match the parenthesised span instead.
    parenthesised = re.search(r"\((find\b[^()]*\+)\)", message)
    if parenthesised:
        return parenthesised.group(1)
    for token in message.split("'"):
        if token.strip().startswith("find") and token.rstrip().endswith("+"):
            return token
    return None


# ---------------------------------------------------------------------------
# CHAINED segments -- promoted from the plan's falsifier, shapes E and G.
#
# Every case above is a LONE find command. That is why the chained-translatable
# gap survived C2: the batch offer was wired into the unknown-verb branch, the
# chained-translatable branch returns before reaching it, and nothing in this
# file exercised a chained shape at all. The falsifier caught it because it
# tests the PROPERTY across a shape matrix rather than the chunk. These two
# cases are that half of the matrix, promoted so a unit run catches a
# regression without re-running the falsifier.
#
# The discriminator both cases turn on: substituting the whole command is
# unsafe when a find segment is chained with other work (it would drop that
# work), but the `+` form is a SEGMENT-local edit and stays safe. So a chained
# shape gets no `updatedInput` and MUST still name the batched form in prose.
# ---------------------------------------------------------------------------


def test_chained_translatable_names_the_plus_form_in_prose() -> None:
    """Falsifier shape E. `rm` is translatable AND batch-equivalent; chained,
    the python rewrite cannot be offered but the `+` form still can.

    Before the fix this branch answered a stated problem with a description
    of a solution -- "a python3 -c os.walk(...) loop does the same
    enumeration" -- which is not a command anyone can run."""
    result = check_find_exec_rewrite("echo hi; find . -name '*.txt' -exec rm {} \\;")
    assert result is not None
    hook_output = result["hookSpecificOutput"]
    assert hook_output.get("updatedInput") is None, (
        "a chained command must NOT be substituted wholesale -- that drops the "
        "other work in the command"
    )
    command = _extract_command(result)
    assert command is not None, "no runnable batched form named for a chained translatable verb"
    assert command.rstrip().endswith("+")
    assert "rm" in command
    message = str(hook_output.get("additionalContext", ""))
    assert "os.walk" not in message, (
        "the os.walk prose is the unrunnable answer this case exists to "
        "replace; if it is back, the batch branch stopped being reached"
    )


def test_chained_untranslatable_names_the_plus_form_in_prose() -> None:
    """Falsifier shape G -- the twin that was already correct. Pinned so the
    two chained branches cannot drift apart again: E regressed precisely by
    being the one branch that did not do what G does."""
    result = check_find_exec_rewrite("echo hi; find . -name '*.txt' -exec chmod 644 {} \\;")
    assert result is not None
    assert result["hookSpecificOutput"].get("updatedInput") is None
    command = _extract_command(result)
    assert command is not None
    assert command.rstrip().endswith("+")
    assert "chmod" in command


def test_chained_plus_form_stays_silent_like_its_lone_twin() -> None:
    """Falsifier shape F. Chaining must not resurrect an advisory on a shape
    that is already batched -- the silent-allow decision is about the
    terminator, not about whether the segment stands alone."""
    assert check_find_exec_rewrite("echo hi; find . -name '*.txt' -exec rm {} +") is None

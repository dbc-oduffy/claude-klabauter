
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
    result = check_find_exec_rewrite("find . -name '*.txt' -exec rm {} +")
    assert result is None


def test_unbatched_semi_form_still_fires() -> None:
    result = check_find_exec_rewrite("find . -name '*.txt' -exec rm {} \\;")
    assert result is not None


def test_plus_form_message_never_claims_per_match_forking() -> None:
    result = check_find_exec_rewrite("find . -name '*.txt' -exec rm {} +")
    if result is not None:
        message = str(result.get("reason", "")) + str(result.get("message", ""))
        assert "PER MATCH" not in message


# to be batch-equivalent -- `_FIND_EXEC_BATCH_EQUIVALENT_VERBS`, gated on
# `_FIND_EXEC_TRANSLATABLE_VERBS` (rm/cat/wc) are shadowed by that earlier


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
    result = check_find_exec_rewrite("find . -exec git rm {} \\;")
    assert result is not None
    command = _extract_command(result)
    assert command is None


def test_head_tail_wc_grep_not_batch_equivalent_no_plus_offered() -> None:
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
    tokens = shlex.split("find . -exec chmod {} 644 \\;")
    parsed = _bt_parse_find_exec_segment(tokens)
    assert parsed is not None
    assert _bt_find_exec_batch_rewrite(tokens, parsed) is None


def test_unit_helper_batch_rewrite_for_translatable_verbs() -> None:
    for verb in ("rm", "cat"):
        tokens = shlex.split("find . -exec %s {} \\;" % verb)
        parsed = _bt_parse_find_exec_segment(tokens)
        assert parsed is not None
        rewritten = _bt_find_exec_batch_rewrite(tokens, parsed)
        assert rewritten is not None
        assert rewritten.rstrip().endswith("+")
        assert verb in rewritten


def _extract_command(result: dict) -> "str | None":
    hook_output = result.get("hookSpecificOutput") or {}
    updated = hook_output.get("updatedInput")
    if isinstance(updated, dict) and updated.get("command"):
        return str(updated["command"])
    message = str(hook_output.get("additionalContext", ""))
    parenthesised = re.search(r"\((find\b[^()]*\+)\)", message)
    if parenthesised:
        return parenthesised.group(1)
    for token in message.split("'"):
        if token.strip().startswith("find") and token.rstrip().endswith("+"):
            return token
    return None


# tests the PROPERTY across a shape matrix rather than the chunk. These two
# work), but the `+` form is a SEGMENT-local edit and stays safe. So a chained


def test_chained_translatable_names_the_plus_form_in_prose() -> None:
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
    result = check_find_exec_rewrite("echo hi; find . -name '*.txt' -exec chmod 644 {} \\;")
    assert result is not None
    assert result["hookSpecificOutput"].get("updatedInput") is None
    command = _extract_command(result)
    assert command is not None
    assert command.rstrip().endswith("+")
    assert "chmod" in command


def test_chained_plus_form_stays_silent_like_its_lone_twin() -> None:
    assert check_find_exec_rewrite("echo hi; find . -name '*.txt' -exec rm {} +") is None

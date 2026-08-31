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

import shlex

from coordinator_core.bash_guards.dispatch_checks import (
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

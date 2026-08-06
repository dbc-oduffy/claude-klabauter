"""Engine-level test pinning the 119-vs-120 priority interaction that
``nudge_prose_queue_creation``'s own module docstring names as a real hazard:
a brand-new ``improvement-queue.md`` carrying BOTH a dated pipe row AND a
``justification:`` line satisfies this guard's creation gate (119) AND
``nudge_improvement_queue_write``'s escape hatch (120). 119 must win, or the
escape line's own message would shadow the creation guard's more
consequential offer -- the advisory phase shows at most one message.

Both guards flipped ``CLASS`` to "advisory" in the 2026-08-06 guard-class
census (DR-277; ``nudge_prose_queue_creation`` at C9, this test's sibling
``nudge_improvement_queue_write`` at C10); PRIORITY 119/120 carried across
the phase boundary unchanged (``docs/wiki/write-guard-priority-bands.md``),
so the ordering claim below still holds, just via ``additionalContext``
rather than ``permissionDecision: deny``.

Drives both guards through the real engine
(``coordinator_core.write_guards.engine.evaluate``) rather than unit-testing
either guard in isolation, per the sibling bash-guard commit's
``TestReachableThroughTheDispatchChain`` model.

Spec: example-doctrine-repo docs/decisions/DR-115-queue-shape-is-a-scope-collision-not-a-staleness.md
Spec: docs/decisions/DR-277-guards-are-advisory-by-default-two-named.md
(Review: code-reviewer -- Finding 4, 2026-07-31.)
"""

from __future__ import annotations

from coordinator_core.write_guards import engine


def test_new_improvement_queue_with_justification_creation_guard_wins(tmp_path):
    target = tmp_path / "state" / "improvement-queue.md"
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(target),
            "content": (
                "- 2026-07-31 | new entry | details\n"
                "justification: genuinely cross-cutting, fix needs separate plan\n"
            ),
        },
        "cwd": str(tmp_path),
    }

    result = engine.evaluate(payload)

    assert result is not None
    hso = result["hookSpecificOutput"]
    assert "permissionDecision" not in hso
    # Confirm it's the creation guard (119) that fired, not the escape-hatch
    # guard (120) somehow producing its own advisory for an unrelated reason.
    assert "coordinator-queue-append" in hso["additionalContext"]

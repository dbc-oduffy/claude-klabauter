"""The commit-phase brief tells the committer to confirm an unaccounted path
is actually inside its pathspec before halting on it.

Negative spec: the check is that the confirmation clause exists and precedes
the STOP instruction in the pathspec-reconciliation step, not a prose-quality
lint on the surrounding sentence.
"""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.emit import _PROVENANCE_HEADING


def test_stop_step_confirms_the_path_is_inside_the_pathspec_first():
    stop_index = _PROVENANCE_HEADING.find("STOP")
    confirm_index = _PROVENANCE_HEADING.find("confirm it's inside your")
    assert stop_index != -1, "the STOP instruction is missing"
    assert confirm_index != -1, (
        "the brief no longer tells the committer to confirm the path is "
        "inside its pathspec before halting on it"
    )
    assert confirm_index > stop_index, (
        "the pathspec-confirmation clause must follow the STOP it qualifies"
    )

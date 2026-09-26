
from __future__ import annotations

from coordinator_core.ops.dispatch_emit.emit import _PROVENANCE_HEADING


def test_the_brief_says_a_foreign_dirty_path_is_not_a_refusal():
    lowered = _PROVENANCE_HEADING.lower()
    assert "working tree too" in lowered, (
        "the brief no longer carries the shared-working-tree clause; without it "
        "a commit agent refuses over a peer run's dirty paths and both runs halt"
    )
    assert "belongs to a peer" in lowered
    assert "never commit it, refuse over it, or name it" in lowered


def test_the_brief_tells_the_agent_to_scope_its_own_checks():
    assert "scope checks to your own pathspec" in _PROVENANCE_HEADING.lower(), (
        "an unscoped `git status --porcelain` is how the agent sees peer work "
        "in the first place"
    )

"""The commit brief must tell its agent that a foreign dirty path is not its business.

Concurrent emitted runs share one working tree. A commit agent that reads
`git status --porcelain` with no pathspec sees every peer run's in-flight
edits, and the stranded-work rule — which is about paths THIS wave's own
reports name — reads, to an agent that did not notice the distinction, as
grounds to refuse. Measured 2026-09-19 on four concurrent runs in one repo:
three separate commit phases refused over paths belonging to a different
emitted run, and every one of those runs halted with nothing delivered. Two
runs refusing over each other's paths deliver nothing at all.

Negative spec: this does not pin wording. It pins that the brief says the
scoping rule at all, in a form an agent reading only this paragraph can act
on — a peer's path is not committed, not refused over, not reported.
"""

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

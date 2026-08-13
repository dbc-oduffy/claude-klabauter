"""
coordinator_core.session.tests.test_guard_name_uniqueness — asserts the
three guard registries (bash, write, op-level) share one flat,
unnamespaced key space with no collision between them.

The three registries use three different naming conventions — bash guards
are kebab-case (`GuardEntry.name`, read via
`bash_guards/roster.py::guard_roster()`, payload-free and closure-safe:
it never calls a guard), write guards are snake_case module basenames
(`_Guard.name`, read via `write_guards/engine.py::discover_guard_names()`),
and the op-level guard is the hand-written constant
`scoped_git_commit.py::_CLAIM_CONFLICT_GUARD_NAME`. Nothing upstream
enforces that these three namespaces stay disjoint, and the grant route
this test gates (`docs/plans/2026-08-13-em-exercisable-in-band-grant-
route.md`, C1's `_GRANTABLE_GUARDS`) keys a sentinel on name alone: if a
bash guard and a write guard ever shared a name, minting a sentinel for
one would clear both.

Spec backlink: docs/plans/2026-08-13-em-exercisable-in-band-grant-route.md, chunk C0.
"""

from __future__ import annotations

from collections import Counter

from coordinator_core.bash_guards.roster import guard_roster
from coordinator_core.write_guards.engine import discover_guard_names

try:
    from coordinator_core.ops.ceremony.scoped_git_commit import (
        _CLAIM_CONFLICT_GUARD_NAME,
    )
except ImportError:
    # The op-level registry is a registry of exactly one hand-written
    # constant, and that guard is being deleted outright by
    # `docs/plans/2026-08-13-claim-release-deadlock-and-the-doctrine-that-
    # rejects-it.md` (PM-authorized): a path-touch claim is a swimlane
    # courtesy, so the whole hard-deny goes. Tolerated in both directions on
    # purpose — the invariant this file exists to pin is that the guard-name
    # key space is flat and collision-free, which holds over however many
    # registries currently populate it. Binding the test to the presence of
    # one constant would make it fail on a deletion it has no opinion about.
    _CLAIM_CONFLICT_GUARD_NAME = None


def test_no_guard_name_collides_across_the_three_registries() -> None:
    bash_names = [entry.id for entry in guard_roster()]
    # `import_failed` names are folded in, not discarded: a guard whose module
    # fails to import still occupies its name in the flat key space, and
    # dropping it here would let exactly that guard collide unnoticed.
    write_names, import_failed = discover_guard_names()
    op_names = [_CLAIM_CONFLICT_GUARD_NAME] if _CLAIM_CONFLICT_GUARD_NAME else []

    all_names = bash_names + write_names + list(import_failed) + op_names
    counts = Counter(all_names)
    dupes = sorted(name for name, n in counts.items() if n > 1)

    assert not dupes, (
        f"Guard name(s) shared across registries: {dupes}. The grant key "
        "space is deliberately flat and unnamespaced (route 4's sentinel "
        "is hand-typed by a human operator), so a name shared between "
        "registries lets one sentinel clear guards in two of them. Fix: "
        "rename the newer of the two guards to a name unique across "
        "bash_guards, write_guards, and the op-level constants."
    )

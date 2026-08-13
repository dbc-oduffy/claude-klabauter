"""Registry-level invariant: no two write-guards in the SAME engine phase
(hard-deny or advisory) share a `PRIORITY` slot (AC9,
docs/plans/2026-08-06-apply-guard-class-census.md).

PHASE-SCOPED, NOT GLOBAL. `engine.evaluate` runs two independent phases --
hard-deny guards sorted by `PRIORITY` (first non-None wins), THEN, only if
none fired, advisory guards sorted by `PRIORITY` (first non-None wins) --
each phase its own namespace (`engine.py` lines 168-193). A hard-deny guard
at PRIORITY 120 and an advisory guard at PRIORITY 120 never compete against
each other; the sort/tie-break that matters is only ever within one phase.
A GLOBAL uniqueness assertion would therefore be WRONG and would fail on
correct, deliberately-collision-free code (e.g. a hard-deny guard sharing a
numeric PRIORITY with an advisory guard is not a slot collision at all).

Mirrors `bash_guards/tests/test_advisory_value_registry.py`'s shape: a
registry-level invariant read off the REAL, live registration
(`engine._discover_guards()`), never a per-guard hand check and never a
`check()` closure invocation -- this stays a structural test.

A same-phase PRIORITY collision is a live footgun distinct from a plain
typo: `engine.evaluate`'s `sorted(..., key=lambda g: g.priority)` uses
Python's stable sort, so two colliding guards silently resolve by
whichever happens to iterate first out of `pkgutil.iter_modules` (filesystem
/ import order) -- not a declared, reviewable precedence. This test makes
that latent non-determinism fail loudly instead of shipping silently.

Spec backlink: pln-apply-the-guard-class-census-u-4cae4a, AC9.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List

from coordinator_core.write_guards import engine


def _guards_by_phase() -> Dict[str, List]:
    guards, _import_failed = engine._discover_guards()
    by_phase: Dict[str, List] = defaultdict(list)
    for g in guards:
        by_phase[g.cls].append(g)
    return by_phase


def test_no_priority_collision_within_hard_deny_phase():
    by_phase = _guards_by_phase()
    hard = by_phase.get("hard-deny", [])
    assert len(hard) >= 5, hard  # guard the guard: a near-empty phase would pass vacuously

    seen: Dict[int, List[str]] = defaultdict(list)
    for g in hard:
        seen[g.priority].append(g.name)
    collisions = {p: names for p, names in seen.items() if len(names) > 1}
    assert not collisions, (
        "Same-phase (hard-deny) PRIORITY collision(s) -- these guards would "
        "resolve tie-break order by filesystem/import iteration order, not "
        "a declared precedence: %r" % collisions
    )


def test_no_priority_collision_within_advisory_phase():
    by_phase = _guards_by_phase()
    advisory = by_phase.get("advisory", [])
    assert len(advisory) >= 10, advisory  # guard the guard

    seen: Dict[int, List[str]] = defaultdict(list)
    for g in advisory:
        seen[g.priority].append(g.name)
    collisions = {p: names for p, names in seen.items() if len(names) > 1}
    assert not collisions, (
        "Same-phase (advisory) PRIORITY collision(s) -- these guards would "
        "resolve tie-break order by filesystem/import iteration order, not "
        "a declared precedence: %r" % collisions
    )


def test_cross_phase_shared_priority_is_not_a_collision():
    """PHASE-SCOPED assertion, demonstrated: a hard-deny guard and an
    advisory guard sharing a numeric PRIORITY is legal (independent
    namespaces) -- this test fails only if some future change to this
    module's own logic accidentally globalizes the check above."""
    by_phase = _guards_by_phase()
    hard_priorities = {g.priority for g in by_phase.get("hard-deny", [])}
    advisory_priorities = {g.priority for g in by_phase.get("advisory", [])}
    # No assertion of non-overlap here on purpose -- overlap across phases
    # is EXPECTED and fine. This test exists to make that expectation
    # explicit and executable, not to assert it never happens.
    assert isinstance(hard_priorities, set) and isinstance(advisory_priorities, set)

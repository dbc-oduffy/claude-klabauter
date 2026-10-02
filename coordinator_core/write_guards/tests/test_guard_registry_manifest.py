"""Manifest test — closes the `_discover_guards()` silent-import-failure hole.

Half 1 (CI-visible) of the two-part discharge in
docs/plans/2026-07-29-hook-fan-in-write-path.md chunk C12. Before this fix, a
guard module that raised on import was simply absent from the discovered set
— no operator signal, fail-open for that one guard, every other guard kept
working. This test asserts the discovered guard-name set equals a pinned
manifest, so a syntax error or broken sibling import in ANY guard module
fails a test instead of silently degrading a PM-approval boundary to absent.

What this does NOT cover: an environment-dependent import failure (an
unresolvable claude-klabauter root, a missing third-party dependency, a Windows-only
import quirk present on one operator's machine but not in CI). CI runs on one
environment and cannot see a failure that only reproduces on another. That
gap is why `engine.discover_guard_names()` also exists as a runtime-visible
signal (half 2) — see `coordinator/hooks/scripts/preuse-write-dispatch.py`
(coordinator-content-repo repo), which emits a stderr line naming any guard that failed to
import at actual hook-invocation time, on whatever machine that turns out to
be.

Maintenance note: adding, removing, or renaming a guard module requires
naming it in `test_guard_classification.py`'s HARD_DENY_NAMES or
ADVISORY_NAMES in the same commit — that manual step IS the test's job; it
converts a should-have-been-noticed change into one that must be noticed.
"""

from __future__ import annotations

from coordinator_core.write_guards.engine import discover_guard_names
from coordinator_core.write_guards.tests.test_guard_classification import (
    ADVISORY_NAMES,
    HARD_DENY_NAMES,
)

# The pinned manifest is the classification lists: one hand-maintained
# enumeration, so a new guard is named once, with its class.
_EXPECTED_GUARD_NAMES = frozenset(HARD_DENY_NAMES) | frozenset(ADVISORY_NAMES)


def test_discovered_guard_set_matches_manifest() -> None:
    loaded, import_failed = discover_guard_names()
    assert import_failed == [], (
        f"guard module(s) failed to import and were silently omitted: {import_failed}"
    )
    assert set(loaded) == _EXPECTED_GUARD_NAMES, (
        "discovered guard set diverged from the pinned manifest — "
        f"missing={_EXPECTED_GUARD_NAMES - set(loaded)}, "
        f"unexpected={set(loaded) - _EXPECTED_GUARD_NAMES}"
    )


def test_no_guard_names_collide_with_intentionally_skipped_modules() -> None:
    loaded, _import_failed = discover_guard_names()
    assert "engine" not in loaded
    assert "__main__" not in loaded
    assert not any(name.startswith("_") for name in loaded)

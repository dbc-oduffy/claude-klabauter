"""
coordinator_core.ops.tests.test_handoff_ship_archive_stamp_commit_locus

C4 of docs/plans/2026-08-13-archive-family-coverage-restoration.md.

DISPOSITION: all 3 of the culled file's `def test_` functions are DROPPED-
AS-OBSOLETE, not ported. Reason, discovered while attempting the port
against HEAD 2026-09-19 (AC7b) — this module's entire subject is
unreachable, on a DIFFERENT axis than the mechanism drift documented in this
file's sibling `test_archive_and_commit_index_resync_residue.py`:

`handoff.ship_and_archive`'s Step 3 (the archival call this module's every
test needed to reach) now returns, unconditionally, before attempting any
move:

    "handoff.ship_and_archive is inoperative: its archive leg
    (ops/fleet/archive_shipped_handoffs) was deleted by the C1b subsumption
    without migrating this caller. The successor drops the live-claim-gate
    opt-out this op depends on, so the migration is a safety decision, not
    a repoint. Ship and archive as two steps until it is settled."

(coordinator_core/ops/handoff_ship_archive.py, `_handler`, `_archive_shipped_
act is None` branch.) There is no reachable "stamp lands in the archival
commit" outcome to observe — the op fails loudly at invocation, before Step
3's `archive_and_commit` call is ever made. Attempting a real-git repro (the
governed pattern this row's body calls for) confirmed this empirically: a
full end-to-end `handoff.ship_and_archive` call against a freshly seeded
real repo returns `exit_code: 1`, `archived: False`, with exactly the
message above, not the archival outcome this module's tests were written to
inspect.

**This is not this chunk's gap to close.** `coordinator_core/ops/tests/
test_handoff_ship_archive.py` (present on disk, out-of-band restoration,
C-RECONCILE's own subject — see `state/audits/2026-09-10-archive-family-
restored-out-of-band-reconciliation.md`) already pins this EXACT loud-
failure contract as its one test, `test_ship_and_archive_now_fails_loud_
instead_of_reaching_the_removed_archive_leg` (or equivalently named per
that file's own docstring), and explicitly names this module's own
predecessor coverage as what it replaces. Re-pinning the same contract here
would duplicate that file's assertion, not add coverage — AC8's disposition
for these 3 tests is therefore "superseded by `test_handoff_ship_archive.
py`'s existing loud-failure pin", not "dropped with no replacement".

No production code is touched by this disposition (Anti-scope) — the
inoperative state is read, not fixed; fixing it (re-wiring `handoff.ship_
and_archive`'s Step 3 onto `fleet.archive_completed_handoffs`) is the "not a
repoint" migration decision the op's own error message defers, and is out
of this coverage-only plan's scope.

Spec backlinks:
  - Docstring under test (now stale on this point, see the op's own current
    docstring instead): coordinator_core/ops/handoff_ship_archive.py
  - Superseding coverage: coordinator_core/ops/tests/test_handoff_ship_archive.py
  - This chunk: docs/plans/2026-08-13-archive-family-coverage-restoration.md, C4

This module intentionally carries zero `def test_` functions — the one test
that could be written here (re-pinning the inoperative-Step-3 contract) is
already written, verbatim in effect, in `test_handoff_ship_archive.py`.
"""

from __future__ import annotations

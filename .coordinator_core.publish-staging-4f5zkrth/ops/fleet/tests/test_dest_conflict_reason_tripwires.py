"""
coordinator_core.ops.fleet.tests.test_dest_conflict_reason_tripwires

Cheap, git-free tripwires guarding AC7 of docs/plans/2026-08-13-fleet-archive-
dest-collision-vs-idempotent-replay.md: `_REASON_DEST_CONFLICT` (the wedge
skip reason) must never collapse back onto the AC12-pinned "already-archived"
string (the benign source-gone idempotent-replay reason). Conflating the two
is exactly how a stuck record reported as converged (see the plan's Problem
section and archive/specs/2026-07/2026-07-04-pcore-11-fleet-invoke-ops.md
§ AC12).

Mirrors bin/tests/test_sweep_actioned_memos_blocked.py:53
(test_dest_conflict_reason_is_not_already_archived), extended to every
consuming import path so a future refactor cannot quietly re-collapse the two
reasons in one family while leaving the others intact: the shared definition
in _common.py, the re-export from archive_actioned_memos.py (the original
owner, pinned by the bin/ test above), and the three families lifted onto the
shared predicate by this plan's C1-C3 (archive_handoffs, archive_shipped_handoffs,
prune_bugs).

Negative-spec: pure imports and string comparison only — no git, no
filesystem, no subprocess. This module must stay cheap; the wedge-shape
detector (test_archive_dest_conflict_wedge_detector.py) and the reversal-else
real-git test carry the behavioural coverage.
"""

from __future__ import annotations

import inspect

from coordinator_core.ops.fleet import _common
from coordinator_core.ops.fleet import archive_actioned_memos
from coordinator_core.ops.fleet import archive_handoffs
from coordinator_core.ops.fleet import archive_shipped_handoffs
from coordinator_core.ops.fleet import archive_sizings
from coordinator_core.ops.fleet import prune_bugs

_ALREADY_ARCHIVED = "already-archived"

# Every import path a consumer actually uses, per the plan's C1 note that
# archive_actioned_memos.py re-exports the constant because
# bin/tests/test_sweep_actioned_memos_blocked.py imports it from there, not
# from _common.py.
#
# archive_sizings added by docs/plans/2026-08-13-terminal-sizings-boot-sweep-
# family.md (AC7) — the terminal-sizings family also consumes the shared
# _common._is_identical_duplicate / _REASON_DEST_CONFLICT predicate (AC4)
# and must never let its dest-collision reason collapse onto the AC12-pinned
# "already-archived" source-gone string either.
_IMPORT_SITES = {
    "_common": _common._REASON_DEST_CONFLICT,
    "archive_actioned_memos (re-export)": archive_actioned_memos._REASON_DEST_CONFLICT,
    "archive_handoffs": archive_handoffs._REASON_DEST_CONFLICT,
    "archive_shipped_handoffs": archive_shipped_handoffs._REASON_DEST_CONFLICT,
    "prune_bugs": prune_bugs._REASON_DEST_CONFLICT,
    "archive_sizings": archive_sizings._REASON_DEST_CONFLICT,
}


def test_dest_conflict_reason_differs_from_already_archived_everywhere() -> None:
    """No import path may see `_REASON_DEST_CONFLICT` equal the AC12-pinned reason."""
    for site, value in _IMPORT_SITES.items():
        assert value != _ALREADY_ARCHIVED, (
            f"{site} exposes _REASON_DEST_CONFLICT == {_ALREADY_ARCHIVED!r} — "
            "the wedge and the benign idempotent-replay reasons have collapsed"
        )


def test_dest_conflict_reason_is_one_shared_value() -> None:
    """All five import sites resolve to the SAME constant, not five copies that
    happen to agree today — the whole point of lifting it into _common.py."""
    values = set(_IMPORT_SITES.values())
    assert values == {"archive-dest-conflict"}, (
        f"import sites disagree on the shared constant's value: {_IMPORT_SITES!r}"
    )


def test_already_archived_source_gone_reason_still_present_in_each_family() -> None:
    """AC3: the source-gone case in each of the three lifted families must still
    reach the AC12-pinned "already-archived" string — this plan narrows what
    reaches it, it does not remove it. Reads each module's own source (not a
    behavioural exercise) so this stays git-free and filesystem-free."""
    for module in (archive_handoffs, archive_shipped_handoffs, prune_bugs, archive_sizings):
        source = inspect.getsource(module)
        assert '"already-archived"' in source, (
            f"{module.__name__} no longer contains the AC12-pinned "
            '"already-archived" literal — the source-gone case must not be removed'
        )

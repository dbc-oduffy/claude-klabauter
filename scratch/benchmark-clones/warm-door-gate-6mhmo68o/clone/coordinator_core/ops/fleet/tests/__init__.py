"""Test package for coordinator_core.ops.fleet — fleet.* MUTATING archival ops.

Co-located tests per ops/emit/tests/ precedent.  Tests are grouped by handler:
    test_archive_plans.py    — fleet.archive_completed_plans (C6)
    test_archive_handoffs.py — fleet.archive_completed_handoffs (C7)
    test_prune_bugs.py       — fleet.prune_closed_bugs (C8)
    test_fleet_common.py     — _common.py unit tests (C8)

Shared fixture: conftest.py (fleet_repo — tmp git init -b main repo with
standard artifact trees, seedable helpers, git-state assertion helpers).

Import guard (lesson: state/lessons/2026-07-04-universal-registry-completeness-tests-ov.yaml):
Each test file that asserts on registry completeness or _OP_KEY_SCOPE coverage MUST
import coordinator_core.ops.fleet (or the specific handler module) before the
assertion so that @register_op side-effects have fired.  Without the import,
completeness tests pass vacuously over an empty registry.
"""

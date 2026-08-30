"""Import-surface pin for C1 (docs/plans/2026-08-29-the-push-subsystem-leaves-
and-then-the-pipeline-can-go.md): the push-with-retry subsystem now lives at
`coordinator_core.ops.ceremony.push`, not `commit_pipeline.py`, and every
push-only importer resolves it from there.

IMPORT-ONLY move, no behaviour change -- this test pins the SURFACE (the
symbol lives at its new home), not push semantics, which the pre-existing
push tests already cover.

`commit_pipeline.py` itself was RETIRED by `12b6a009aa` (2026-08-29), so the
re-export half of this pin went with it: a module that does not exist
re-exports nothing -- see `test_commit_entry_import_cost.py` for the full
incident this retirement surfaced.
"""

from __future__ import annotations

import importlib

PUSH_ONLY_IMPORTERS = [
    "coordinator_core.ops.push_outstanding",
    "coordinator_core.ops.ceremony.post_commit_tail",
    "coordinator_core.ops.ceremony.consumed_handoff_stamp",
    "coordinator_core.execute_plan_assemble.close_out_and_stamp",
    "coordinator_core.workstream_complete.directives_commit_tail",
    "coordinator_core.ops.session.safe_commit_offer",
]


def test_push_subsystem_symbols_live_at_their_new_home():
    push_mod = importlib.import_module("coordinator_core.ops.ceremony.push")
    for name in (
        "push_with_retry",
        "derive_push_status",
        "derive_pushed_tristate",
        "resolve_post_push_sha",
        "PushOutcome",
        "PUSH_MODE_SYNC",
        "PUSH_MODE_DEFERRED",
        "PUSH_MODE_NONE",
        "PUSH_MODE_NEVER",
        "PUSH_STATUS_PUSHED",
        "PUSH_STATUS_FAILED",
        "PUSH_STATUS_DECLINED",
        "PUSH_STATUS_NO_REMOTE",
        "PUSH_STATUS_NOT_ATTEMPTED",
        "PUSH_STATUS_UNCONFIRMED",
        "PUSH_STATUS_CADENCE_PENDING",
        "CEREMONY_PUSH_BUDGET_SECS",
        "PUSH_RETRY_BUDGET_SECS",
        "_drain_pending_push_after_sync",
    ):
        assert hasattr(push_mod, name), f"push.py is missing {name!r}"


def test_seven_push_only_importers_still_resolve_after_the_move():
    """Every importer named in C1's `writes` list still imports cleanly.

    Not a behaviour test -- an import-time smoke test that a caller's own
    `from ... import (...)` line resolves, which is exactly what a stale
    `commit_pipeline` re-export would silently defeat.
    """
    for module_name in PUSH_ONLY_IMPORTERS:
        importlib.import_module(module_name)

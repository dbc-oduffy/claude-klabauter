"""Fast-tier companion to `test_process_global_characterization.py`.

Purpose: C30 of `docs/plans/2026-08-20-a-refusal-cannot-exit-zero.md` (W21) --
the cadence-only module guards all nine process-global sites the 2026-08-15
warm-engine plan's C-chunks catalogued, but `pytestmark = pytest.mark.cadence`
on that module means a regression in any of the nine is NOT caught per-commit.

This module re-collects the SAME nine test functions (imported by reference,
not copied) under a module that carries no `cadence` marker, so they run on
the fast tier too. Pytest marker application is a collection-time property of
the module a test is COLLECTED from, not where its body is defined --
re-binding the function objects here is sufficient to make them fast-tier
tests without duplicating a single assertion or drifting out of sync with the
slow module's HISTORY/FIXED-BEHAVIOUR docstrings, which stay authoritative on
the original module.

Each site's own docstring already documents its actual wall-clock shape: the
`threading`-driven sites (3, 5, 6, 7) synchronize via `threading.Event`s with
a 5s timeout CEILING, not a 5s sleep -- in the passing case every one resolves
in well under a second, which is why duplicating them onto the fast tier is
safe under the machine load norm rather than reintroducing cadence-only
weight through the back door.

Spec backlink: docs/plans/2026-08-20-a-refusal-cannot-exit-zero.md § C30 (W21)
"""

from __future__ import annotations

from coordinator_core.warm.tests import test_process_global_characterization as _slow

# Site 1
test_blanket_disarm_cache_does_not_fail_open_past_expiry = (
    _slow.test_blanket_disarm_cache_does_not_fail_open_past_expiry
)

# Site 2
test_load_deliverable_ledger_serves_each_roots_own_ledger = (
    _slow.test_load_deliverable_ledger_serves_each_roots_own_ledger
)
test_ledger_artifact_readable_serves_each_roots_own_verdict = (
    _slow.test_ledger_artifact_readable_serves_each_roots_own_verdict
)
test_ledger_validated_flag_still_validates_a_second_roots_ledger = (
    _slow.test_ledger_validated_flag_still_validates_a_second_roots_ledger
)

# Site 3
test_session_identity_does_not_cross_contaminate_under_interleave = (
    _slow.test_session_identity_does_not_cross_contaminate_under_interleave
)
test_mirror_session_env_for_subprocess_scoped_to_one_call = (
    _slow.test_mirror_session_env_for_subprocess_scoped_to_one_call
)

# Site 4
test_registry_snapshot_cache_serves_within_ttl_not_for_process_lifetime = (
    _slow.test_registry_snapshot_cache_serves_within_ttl_not_for_process_lifetime
)
test_registry_snapshot_cache_re_fetches_after_ttl_expiry = (
    _slow.test_registry_snapshot_cache_re_fetches_after_ttl_expiry
)

# Site 5
test_reentrancy_guard_isolates_unrelated_concurrent_dispatches = (
    _slow.test_reentrancy_guard_isolates_unrelated_concurrent_dispatches
)
test_reentrancy_guard_still_detects_genuine_same_context_nesting = (
    _slow.test_reentrancy_guard_still_detects_genuine_same_context_nesting
)

# Site 6
test_git_probe_deadline_not_shared_across_interleaved_dispatches = (
    _slow.test_git_probe_deadline_not_shared_across_interleaved_dispatches
)

# Site 7
test_capture_session_does_not_cross_contaminate_under_interleave = (
    _slow.test_capture_session_does_not_cross_contaminate_under_interleave
)

# Site 8
test_file_lock_eviction_is_held_aware = _slow.test_file_lock_eviction_is_held_aware

# Site 9
test_engine_root_gate_memo_keys_per_interleaved_session_root = (
    _slow.test_engine_root_gate_memo_keys_per_interleaved_session_root
)

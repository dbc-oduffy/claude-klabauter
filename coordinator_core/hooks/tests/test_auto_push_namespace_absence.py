"""
coordinator_core.hooks.tests.test_auto_push_namespace_absence -- mechanical
oracle for C8's gravestone list (docs/plans/2026-08-30-who-pushes-and-when.md).

C8's dispatch brief inverted the default from "survive unless named for
deletion" to "delete unless a named live caller is cited" (Kira Finding "C8
has no forcing function"): a zero-diff C8 must FAIL this test. Rather than
reading the module's source to confirm a deletion happened -- which a
sufficiently confident reviewer could get wrong the same way a human can --
this asserts on the module's own runtime namespace (`dir(auto_push)`), the
same grep-the-namespace primitive `2026-08-30-who-pushes-and-when.falsifier.py`
leg3 (`leg3_cadence`) already uses to distinguish "named right" from
"behaves right": here the criterion is "named at all".

Negative-spec: this file does NOT re-verify that the SURVIVING symbols
(`log_failure`, `log_race_resolved`, `log_dead_ref_failure`,
`_read_pending_record`, `_pending_record_path`, `_record_is_stale`,
`_maybe_publish_cockpit_contract`) still exist -- that is
`coordinator_core/warm/push_cadence.py`'s own import surface and
`test_auto_push.py`'s retargeted coverage, and duplicating it here would
let this file go stale in the direction that hides a real regression (a
survivor accidentally deleted) behind a passing absence-only assertion.
(`run_push_with_retry`, `drain_pending_push`, and `_write_pending_record`
were themselves gravestoned the same day by C2, a separate commit from
this file's own `_GRAVESTONED_NAMES` list below, which tracks C8's
distinct `_hold_window`/detached-respawn cascade.)

Spec backlink: state/dispatch-briefs/2026-08-30-who-pushes-and-when/C8.md
Module under test: coordinator_core/hooks/auto_push.py
"""

from __future__ import annotations

from coordinator_core.hooks import auto_push

#: The full C8 gravestone list -- every name here must be ABSENT from
#: `dir(auto_push)`. Sourced from the brief's two reachability traces:
#: the `_hold_window` retraction-window cascade (itself, plus the helpers
#: whose sole caller it was) and the per-commit detached-respawn cascade
#: (`_detach_and_run`/`spawn_detached_push`, plus their sole-caller
#: helpers). `_resolve_python_exe` is deliberately NOT in this list -- it
#: survives via `_invoke_cockpit_publish` (see that function's docstring).
_GRAVESTONED_NAMES = (
    # `_hold_window` cascade.
    "_hold_window",
    "_branch_diverged_no_spawn",
    "_shared_branch_live_count",
    "_peer_commit_within_window",
    "_read_ref_sha_no_spawn",
    "_remote_is_ancestor_no_spawn",
    # per-commit detached-respawn cascade.
    "_detach_and_run",
    "spawn_detached_push",
    "_claude_klabauter_package_root",
    "_respawn_env",
    "_open_respawn_stderr_log",
    "_windows_detached_flags",
    "_disown_stdio",
)


def test_gravestoned_names_absent_from_auto_push_namespace():
    present = sorted(name for name in _GRAVESTONED_NAMES if hasattr(auto_push, name))
    assert not present, (
        f"C8 gravestone list still present in coordinator_core.hooks.auto_push: "
        f"{present} -- a zero-diff C8 must fail this test "
        "(docs/plans/2026-08-30-who-pushes-and-when.md)"
    )


def test_resolve_python_exe_survives_as_a_named_exception():
    """`_resolve_python_exe` is the one name shared with the respawn cascade
    that is NOT gravestoned -- it survives via `_invoke_cockpit_publish`.
    Pinned separately so a future over-broad deletion sweep across the
    respawn helpers is caught here rather than silently taking this one
    down with it.
    """
    assert hasattr(auto_push, "_resolve_python_exe")

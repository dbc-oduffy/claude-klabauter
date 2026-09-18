"""Coverage for `coordinator_core.warm.http_hook_forwarder` -- the fixed-port front door
`type: "http"` hook registrations dial, forwarding to the engine's own ephemeral warm listener.

This module arrived from DoE-claude's `coordinator/hooks/http_hook_forwarder.py`
(docs/plans/2026-09-18-doe-holds-no-scripts.md, row W4-C2): a long-lived fixed-port relay is
engine infrastructure, so it now lives in the engine's own supervised process set. The move
required one substantive change beyond the path, and this file's first section pins it: per that
plan's § Path resolution, the module's DoE-side `_engine_root` sibling-checkout lookup --
resolve-once, cached, invalidated on a debounced ladder, and the exact mechanism behind the
2026-09-01 incident where a resident forwarder answered "no live engine backend reachable" for
~38 minutes against a healthy listener because its cached root had gone stale -- is DELETED, not
ported. `Path(__file__).resolve().parents[2]` **is** the engine root now (the module's own
tree, matching `coordinator_core.ops.invoke_from_argv._ENGINE_ROOT`'s own computation), and a
root that cannot resolve wrong needs no invalidation path.

NOT A FULL PORT of DoE's twelve `test_http_hook_forwarder_*.py` files (2861 lines) -- this
footprint carries one test file. What is ported here is the behaviour that is either
location-independent pure logic (`_apply_registration_op`, the ladder response bodies) or the
one thing this move actually changed (engine-root resolution); DialCounter's zero-discipline is
exercised directly against its public API rather than over a live socket, which the DoE suite's
`test_http_hook_forwarder_dial_counter.py` does and this file does not attempt to re-derive.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.invoke_from_argv import _ENGINE_ROOT as _EXPECTED_ENGINE_ROOT
from coordinator_core.warm import http_hook_forwarder as forwarder
from coordinator_core.warm.engine_root import is_engine_root


# ---------------------------------------------------------------------------
# Engine-root resolution -- the one behaviour the move actually changed.
# ---------------------------------------------------------------------------


def test_engine_root_matches_the_module_tree_computation():
    """`_resolved_engine_root` must agree with the engine's own `_ENGINE_ROOT` constant -- the
    same `Path(__file__).resolve().parents[2]` computation, from a different file two levels
    under the repo root. A divergence here means the two disagree about where the engine lives."""
    assert forwarder._resolved_engine_root() == str(_EXPECTED_ENGINE_ROOT)
    assert forwarder._ENGINE_ROOT == _EXPECTED_ENGINE_ROOT


def test_engine_root_is_never_none():
    """Unlike the retired registry-resolved root, this one cannot fail to resolve -- there is no
    registry read that can come back empty. Every `if not engine_root:` guard downstream of
    `_resolved_engine_root()` is therefore dead on this checkout, by construction, not by luck."""
    for _ in range(5):
        assert forwarder._resolved_engine_root()


def test_engine_root_is_a_well_formed_predicate_call():
    """`_resolve_backend`'s `is_engine_root` gate is called against the fixed root, never against
    a value this module invented -- confirm the call is well-formed (a `bool`, never a raise) for
    the exact path `_resolved_engine_root` hands it. Whether THIS checkout happens to carry a
    stamp is a fact about the checkout (per W4-C1's own spike verdict, an unpublished dev
    checkout does not), not something this move changes or this test should assume either way."""
    result = is_engine_root(Path(forwarder._resolved_engine_root()))
    assert isinstance(result, bool)


def test_engine_root_snapshot_shape_is_stable():
    """Readers of the dial file's `engine` key (schema 5) still get every field the DoE-side
    snapshot promised, even though `resolution_class`/`provenance` are now constants rather than
    values read off a registry ladder."""
    snap = forwarder.engine_root_snapshot()
    assert snap["engine_root"] == str(_EXPECTED_ENGINE_ROOT)
    assert snap["resolution_class"] == "module-tree"
    assert snap["provenance"] == "Path(__file__).resolve().parents[2]"
    assert isinstance(snap["resolved_at"], str) and snap["resolved_at"]
    # discovery_path is memoised lazily by _note_discovery_path; absent until a request notes it.


def test_the_retired_registry_ladder_is_gone():
    """Negative spec: a successor must not reintroduce the cache-with-no-exit this move deleted.
    Pinning by absence rather than by behaviour, since there is no behaviour left to observe."""
    for retired_name in (
        "_ensure_engine_on_sys_path",
        "_ensure_engine_on_sys_path_locked",
        "invalidate_engine_root",
        "_engine_root_cache",
        "_resolve_engine",
    ):
        assert not hasattr(forwarder, retired_name), retired_name


# ---------------------------------------------------------------------------
# _apply_registration_op -- pure, location-independent; ported from DoE's
# test_http_hook_forwarder_registration_op.py.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "incoming, expected",
    [
        ("/hook/hooks.agent_postuse_dispatch", "/hook/hooks.agent_postuse_dispatch"),
        ("/hook/hooks.context_pressure_precompact", "/hook/hooks.context_pressure_precompact"),
        ("/hook/hooks.agent_postuse_dispatch?x=1", "/hook/hooks.agent_postuse_dispatch"),
        ("/hook/hooks.agent_postuse_dispatch#frag", "/hook/hooks.agent_postuse_dispatch"),
    ],
)
def test_a_registration_op_reaches_the_backend_path(incoming, expected):
    assert forwarder._apply_registration_op("/hook", incoming) == expected


@pytest.mark.parametrize(
    "incoming",
    ["/hook", "/hook/", "/hook/notanop", "", None],
)
def test_anything_without_an_op_leaves_the_discovery_path_untouched(incoming):
    """NEGATIVE SPEC. A bare or unrecognized path must change NOTHING -- the forwarder must never
    synthesize an op segment the caller did not send."""
    assert forwarder._apply_registration_op("/hook", incoming) == "/hook"


def test_the_discovery_records_own_hook_path_is_respected_not_hardcoded():
    assert (
        forwarder._apply_registration_op("/custom", "/hook/hooks.agent_postuse_dispatch")
        == "/custom/hooks.agent_postuse_dispatch"
    )
    assert forwarder._apply_registration_op("/custom/", "/hook/hooks.x") == "/custom/hooks.x"


def test_self_path_is_actually_read_by_the_handler():
    """Regression pin for the root cause DoE measured 2026-08-27: the module must reference
    `self.path` at all, or every mapping test above passes while the wiring that feeds them is
    gone."""
    source = Path(forwarder.__file__).read_text(encoding="utf-8")
    assert "_apply_registration_op(hook_path, self.path)" in source


# ---------------------------------------------------------------------------
# Ladder response bodies -- DR-402's shapes.
# ---------------------------------------------------------------------------


def test_deny_body_shape():
    payload = json.loads(forwarder._deny_body("PreToolUse"))
    out = payload["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == forwarder.DENY_REASON


def test_proceed_body_never_carries_an_allow_decision():
    """Rung 3 states a reason and authors NO permission decision -- an explicit allow
    auto-approves on PreToolUse, which is strictly more permissive than the guard being absent."""
    payload = json.loads(forwarder._proceed_body("PreToolUse", "some cause"))
    out = payload["hookSpecificOutput"]
    assert "permissionDecision" not in out
    assert out["hookEventName"] == "PreToolUse"
    assert "some cause" in out["additionalContext"]


# ---------------------------------------------------------------------------
# DialCounter -- zero-discipline and counting, via the public API directly
# (no live socket; DoE's own socket-based suite is not re-derived here).
# ---------------------------------------------------------------------------


def test_a_bound_counter_reads_zero_not_absent(tmp_path):
    """The file exists with counts already at zero the moment a counter is constructed -- what
    makes zero mean 'bound and undialled' rather than 'never bound'."""
    counter = forwarder.DialCounter(path=tmp_path / "dial-count.json")
    snap = counter.snapshot()
    assert snap["received_total"] == 0
    assert snap["received_by_event"] == {}
    assert snap["bound_at"] is not None
    assert snap["boot_id"]


def test_a_real_arrival_moves_the_count_from_zero_to_one(tmp_path):
    counter = forwarder.DialCounter(path=tmp_path / "dial-count.json")
    at = counter.record_arrival()
    counter.record_event("PreToolUse", at=at)
    counter.record_forwarded("PreToolUse")
    snap = counter.snapshot()
    assert snap["received_total"] == 1
    assert snap["received_by_event"] == {"PreToolUse": 1}
    assert snap["forwarded_by_event"] == {"PreToolUse": 1}


def test_denied_arm_and_cause_are_both_recorded_together(tmp_path):
    """`denied_by_cause` must never undercount `denied_by_arm` -- every `record_denied` needs an
    accompanying `record_cause`, or a reader cannot attribute the denial to a return site."""
    counter = forwarder.DialCounter(path=tmp_path / "dial-count.json")
    counter.record_denied(forwarder.DENY_ARM_NO_BACKEND)
    counter.record_cause(forwarder.CAUSE_NO_ROUTING_KEY)
    snap = counter.snapshot()
    assert snap["denied_by_arm"] == {forwarder.DENY_ARM_NO_BACKEND: 1}
    assert snap["denied_by_cause"] == {forwarder.CAUSE_NO_ROUTING_KEY: 1}


def test_persist_writes_a_readable_file(tmp_path):
    path = tmp_path / "dial-count.json"
    counter = forwarder.DialCounter(path=path)
    counter.record_arrival()
    counter.persist()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["received_total"] == 1
    assert on_disk["schema"] == 5


# ---------------------------------------------------------------------------
# Module surface -- constants unaffected by the move, sanity-pinned so a future
# edit touching them lands here too.
# ---------------------------------------------------------------------------


def test_fixed_port_constant_unchanged():
    assert forwarder.FIXED_PORT == 47623


def test_health_marker_constants():
    assert forwarder.HEALTH_PATH == "/health"
    assert forwarder.DOOR_PROTOCOL_VERSION_KEY == "door_protocol_version"
    assert isinstance(forwarder.PUBLISHED_DOOR_PROTOCOL_VERSION, int)


def test_module_fingerprint_reads_this_files_own_bytes():
    """`module_fingerprint()` with no argument must hash the module's OWN file, not some other
    path -- it is the fact `sessionstart-ensure-http-forwarder.py`-equivalent callers use to tell
    running code from on-disk code."""
    fp = forwarder.module_fingerprint()
    assert fp is not None
    assert fp == forwarder.module_fingerprint(Path(forwarder.__file__))

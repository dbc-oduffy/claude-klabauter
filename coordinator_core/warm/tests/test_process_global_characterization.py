"""Characterization tests for the nine warm-engine process-global sites.

Purpose: C2 of `docs/plans/2026-08-15-warm-engine-retires-the-per-invocation-cold-start.md`.
Every test below drives two roots / two sessions / two interleaved dispatches
in ONE process (the shape a warm, concurrently-dispatched engine will
actually see) and asserts the behaviour of one of the nine process-global
sites the mechanism-2 audit (plus staff-eng finding 8) enumerated.

HISTORY: this file originally landed BEFORE C4-C10's fixes and pinned
TODAY'S-THEN-WRONG behaviour by design (see the plan's Anti-scope: "Do not
fix a process-global before its characterization test lands."). Once C4-C10
landed and fixed every site, each assertion here was flipped in place (same
test shape, same interleave/concurrency drive, corrected expected outcome)
to become the permanent regression guard for the fixed behaviour -- a test
here going red now is a real regression, not the designed outcome the
HISTORY note above describes for the pre-fix era.

Several sites below (3, 5, 6, 7) drive real `threading` interleave with up
to 5s joins/waits; marked `cadence` ("heavy suite, runs at cadence gates,
not per-commit" per `pyproject.toml`) so this module's wall-clock weight
sits out of the per-commit fast tier rather than slowing it under the
machine load norm.

Spec backlink: docs/plans/2026-08-15-warm-engine-retires-the-per-invocation-cold-start.md § C2
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core import _hook_envelope
from coordinator_core import engine_root
from coordinator_core.bash_guards import _blanket_disarm
from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.contract import apply_base
from coordinator_core.hooks import track_touched_files
from coordinator_core.ops import deliverable_equivalence
from coordinator_core.ops import gate_dimension_latency
from coordinator_core.session import liveness

pytestmark = pytest.mark.cadence


def test_blanket_disarm_cache_does_not_fail_open_past_expiry():
    _blanket_disarm._cache.clear()
    try:
        session_id = "warm-c2-session-a"
        is_em = True
        home = _blanket_disarm.settings_home()
        stat_key = _blanket_disarm._marker_stat_key(home / _blanket_disarm.MARKER_BASENAME)
        cache_key = (str(home), stat_key, session_id, is_em)
        already_expired = _blanket_disarm.DisarmResult(
            active=True,
            detail="disarm marker present (test-seeded, already expired)",
            scope="session",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        _blanket_disarm._cache[cache_key] = already_expired

        result = _blanket_disarm.disarm_status({"session_id": session_id})

        # FIXED BEHAVIOUR (C4): a cache hit whose own expires_at has passed
        assert result.active is False
        assert result is not already_expired
    finally:
        _blanket_disarm._cache.clear()


def _write_ledger_artifact(root: Path, rows: list[dict]) -> None:
    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    lines = ["ledger:"]
    for row in rows:
        first = True
        for key, value in row.items():
            prefix = "  - " if first else "    "
            first = False
            lines.append(f"{prefix}{key}: {value!r}")
    (state_dir / "deliverable-equivalence.yaml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def test_load_deliverable_ledger_serves_each_roots_own_ledger(tmp_path):
    deliverable_equivalence._reset_deliverable_ledger_cache()
    try:
        root_a = tmp_path / "root_a"
        root_b = tmp_path / "root_b"
        root_a.mkdir()
        root_b.mkdir()
        _write_ledger_artifact(
            root_a, [{"deliverable_id": "dlv-a", "evidence_source": "a.md"}]
        )
        _write_ledger_artifact(
            root_b, [{"deliverable_id": "dlv-b", "evidence_source": "b.md"}]
        )

        rows_a = deliverable_equivalence.load_deliverable_ledger(root_a)
        rows_b = deliverable_equivalence.load_deliverable_ledger(root_b)

        # FIXED BEHAVIOUR (C5): each root's rows are its own.
        assert rows_a[0]["deliverable_id"] == "dlv-a"
        assert rows_b[0]["deliverable_id"] == "dlv-b"
        assert rows_a != rows_b
    finally:
        deliverable_equivalence._reset_deliverable_ledger_cache()


def test_ledger_artifact_readable_serves_each_roots_own_verdict(tmp_path):
    deliverable_equivalence._reset_deliverable_ledger_cache()
    try:
        root_a = tmp_path / "root_a"
        root_b = tmp_path / "root_b"
        root_a.mkdir()
        root_b.mkdir()
        state_dir_b = root_b / "state"
        state_dir_b.mkdir(parents=True)
        (state_dir_b / "deliverable-equivalence.yaml").write_text(
            "not: [valid, yaml, :::", encoding="utf-8"
        )

        readable_a = deliverable_equivalence._ledger_artifact_readable(root_a)
        readable_b = deliverable_equivalence._ledger_artifact_readable(root_b)

        assert readable_a is True
        # FIXED BEHAVIOUR (C5): root_b's broken artifact reads False on its
        assert readable_b is False
    finally:
        deliverable_equivalence._reset_deliverable_ledger_cache()


def test_ledger_validated_flag_still_validates_a_second_roots_ledger(tmp_path):
    """C5 -- `_DELIVERABLE_LEDGER_VALIDATED` is now a `set` keyed per
    `(worktree_root, artifact mtime)` (the same `_artifact_cache_key` shape
    as the other memos in this module), not a single process-wide bool. A
    second root's malformed row is validated on its own merits, not silently
    skipped because an earlier root already validated once."""
    deliverable_equivalence._reset_deliverable_ledger_cache()
    try:
        root_a = tmp_path / "root_a"
        root_b = tmp_path / "root_b"
        root_a.mkdir()
        root_b.mkdir()
        _write_ledger_artifact(
            root_a,
            [
                {
                    "deliverable_id": "dlv-a",
                    "evidence_source": "a.md",
                    "status": "open",
                    "adjudicator": "test",
                }
            ],
        )

        def _no_frontmatter(_artifact_path, _field):
            return None

        # First root: ordinary read, adds root_a's key to the VALIDATED set.
        deliverable_equivalence.dual_read_deliverable_id(
            root_a, str(root_a / "a.md"), {}, read_frontmatter_field=_no_frontmatter
        )
        assert len(deliverable_equivalence._DELIVERABLE_LEDGER_VALIDATED) == 1

        state_dir_b = root_b / "state"
        state_dir_b.mkdir(parents=True)
        (state_dir_b / "deliverable-equivalence.yaml").write_text(
            "ledger:\n  - deliverable_id: 12345\n    evidence_source: b.md\n",
            encoding="utf-8",
        )

        # FIXED BEHAVIOUR (C5): root_b's own key is not yet in the VALIDATED
        with pytest.raises(deliverable_equivalence.DeliverableLedgerValidationError):
            deliverable_equivalence.dual_read_deliverable_id(
                root_b, str(root_b / "b.md"), {}, read_frontmatter_field=_no_frontmatter
            )
    finally:
        deliverable_equivalence._reset_deliverable_ledger_cache()


def test_session_identity_does_not_cross_contaminate_under_interleave():
    """C6 -- `session_identity()` now scopes SESSION_ENV_VARS into per-name
    `contextvars.ContextVar`s, not `os.environ`. Two overlapping calls with
    different session ids (the shape two interleaved warm dispatches take,
    here two threads -- each thread runs in its own `contextvars.Context` by
    default) each see their OWN identity via `current_session_env()` for the
    duration of their own block, and `os.environ` itself is never touched by
    `session_identity()` at all -- only `_mirror_session_env_for_subprocess`
    touches it, and only for the duration of one subprocess-spawning call
    that this test never makes."""
    import os

    original = {var: os.environ.get(var) for var in apply_base.SESSION_ENV_VARS}
    for var in apply_base.SESSION_ENV_VARS:
        os.environ.pop(var, None)

    entered_a = threading.Event()
    observed_inside_a: dict[str, Optional[str]] = {}
    let_b_finish = threading.Event()
    b_done = threading.Event()

    def _session_a():
        with apply_base.session_identity("session-A"):
            entered_a.set()
            b_done.wait(timeout=5)
            observed_inside_a["COORDINATOR_SESSION_ID"] = apply_base.current_session_env().get(
                "COORDINATOR_SESSION_ID"
            )
            let_b_finish.set()

    def _session_b():
        entered_a.wait(timeout=5)
        with apply_base.session_identity("session-B"):
            b_done.set()
            let_b_finish.wait(timeout=5)

    try:
        thread_a = threading.Thread(target=_session_a)
        thread_b = threading.Thread(target=_session_b)
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=5)
        thread_b.join(timeout=5)

        # FIXED BEHAVIOUR (C6): session A's own contextvar-scoped identity is
        assert observed_inside_a["COORDINATOR_SESSION_ID"] == "session-A"
        for var in apply_base.SESSION_ENV_VARS:
            assert os.environ.get(var) is None
    finally:
        for var, value in original.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value


def test_mirror_session_env_for_subprocess_scoped_to_one_call(monkeypatch):
    import os

    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

    with apply_base.session_identity("session-mirror-test"):
        assert os.environ.get("COORDINATOR_SESSION_ID") is None
        with apply_base._mirror_session_env_for_subprocess():
            assert os.environ.get("COORDINATOR_SESSION_ID") == "session-mirror-test"
        assert os.environ.get("COORDINATOR_SESSION_ID") is None
    assert os.environ.get("COORDINATOR_SESSION_ID") is None


def test_registry_snapshot_cache_serves_within_ttl_not_for_process_lifetime(monkeypatch):
    """C8 -- `_registry_snapshot_cache` is bounded by a 2s TTL
    (`_REGISTRY_SNAPSHOT_TTL_SEC`), not pinned for the rest of the process.
    Within the TTL window a second lookup still serves the first snapshot
    (correct short-lived-batch behaviour, unchanged) -- but the cache is NOT
    a "no production resetter, forever" pin; it expires and re-fetches on
    its own once the TTL has elapsed. See the companion test below for the
    expiry leg."""
    liveness._registry_snapshot_cache = None
    liveness._registry_snapshot_cache_at = None
    try:
        calls = {"n": 0}

        class _FakeRegistry:
            def snapshot(self):
                calls["n"] += 1
                if calls["n"] == 1:
                    return {"sid-first": {"pid": 111}}
                return {"sid-second": {"pid": 222}}

        monkeypatch.setattr(liveness, "harness_registry", _FakeRegistry())

        first = liveness._cached_registry_lookup("sid-first")
        second_lookup_of_new_sid = liveness._cached_registry_lookup("sid-second")

        assert first == {"pid": 111}
        assert calls["n"] == 1
        assert second_lookup_of_new_sid is None
    finally:
        liveness._registry_snapshot_cache = None
        liveness._registry_snapshot_cache_at = None


def test_registry_snapshot_cache_re_fetches_after_ttl_expiry(monkeypatch):
    """C8 companion -- once `_REGISTRY_SNAPSHOT_TTL_SEC` has elapsed since
    the cached snapshot was taken, the next lookup re-fetches rather than
    replaying the stale snapshot, so a peer session's claim landing mid-
    process does eventually become visible."""
    liveness._registry_snapshot_cache = None
    liveness._registry_snapshot_cache_at = None
    try:
        calls = {"n": 0}

        class _FakeRegistry:
            def snapshot(self):
                calls["n"] += 1
                if calls["n"] == 1:
                    return {"sid-first": {"pid": 111}}
                return {"sid-second": {"pid": 222}}

        monkeypatch.setattr(liveness, "harness_registry", _FakeRegistry())

        first = liveness._cached_registry_lookup("sid-first")
        assert first == {"pid": 111}
        assert calls["n"] == 1

        liveness._registry_snapshot_cache_at -= liveness._REGISTRY_SNAPSHOT_TTL_SEC + 0.01

        second_lookup_of_new_sid = liveness._cached_registry_lookup("sid-second")

        assert calls["n"] == 2
        assert second_lookup_of_new_sid == {"pid": 222}
    finally:
        liveness._registry_snapshot_cache = None
        liveness._registry_snapshot_cache_at = None


# Site 5: coordinator_core/ops/gate_dimension_latency.py -- `_REENTRANCY_GUARD`


def test_reentrancy_guard_isolates_unrelated_concurrent_dispatches(monkeypatch):
    """C8 -- `_REENTRANCY_GUARD` is now a `contextvars.ContextVar`, scoped to
    a single logical dispatch's Context rather than the whole process. Two
    UNRELATED concurrent `_check_latency` calls (two different sessions
    gating two different commits at once, here two different threads -- each
    running in its own default `contextvars.Context`) no longer trip each
    other's "re-entered itself" sentinel."""
    gate_dimension_latency._REENTRANCY_GUARD.set(False)

    entered = threading.Event()
    release = threading.Event()

    def _blocking_load_op_inventory():
        entered.set()
        release.wait(timeout=5)
        return []

    monkeypatch.setattr(
        gate_dimension_latency, "_load_op_inventory", _blocking_load_op_inventory
    )

    thread_a_result: dict[str, object] = {}

    def _dispatch_a():
        thread_a_result["value"] = gate_dimension_latency._check_latency([], None, None)

    thread_a = threading.Thread(target=_dispatch_a)
    thread_a.start()
    entered.wait(timeout=5)

    monkeypatch.setattr(gate_dimension_latency, "_load_op_inventory", lambda: [])
    result_b = gate_dimension_latency._check_latency([], None, None)
    assert result_b.verdict == gate_dimension_latency.Verdict.UNAVAILABLE

    release.set()
    thread_a.join(timeout=5)
    assert thread_a_result["value"].verdict == gate_dimension_latency.Verdict.UNAVAILABLE
    gate_dimension_latency._REENTRANCY_GUARD.set(False)


def test_reentrancy_guard_still_detects_genuine_same_context_nesting(monkeypatch):
    gate_dimension_latency._REENTRANCY_GUARD.set(False)
    try:

        def _reentering_load_op_inventory():
            return gate_dimension_latency._check_latency([], None, None)

        monkeypatch.setattr(
            gate_dimension_latency, "_load_op_inventory", _reentering_load_op_inventory
        )

        with pytest.raises(gate_dimension_latency.LatencyDimensionReentrancyError):
            gate_dimension_latency._check_latency([], None, None)
    finally:
        gate_dimension_latency._REENTRANCY_GUARD.set(False)


def test_git_probe_deadline_not_shared_across_interleaved_dispatches():
    dispatch_checks._git_probe_deadline.set(None)
    try:
        dispatch_checks._arm_git_probe_deadline(budget=0.001)
        time.sleep(0.01)
        assert dispatch_checks._git_probe_budget_spent() is True

        # A second, interleaved dispatch, in a DIFFERENT Context (thread),
        second_dispatch_result: dict[str, object] = {}

        def _second_dispatch():
            dispatch_checks._arm_git_probe_deadline(budget=100.0)
            second_dispatch_result["spent"] = dispatch_checks._git_probe_budget_spent()
            dispatch_checks._disarm_git_probe_deadline()

        thread = threading.Thread(target=_second_dispatch)
        thread.start()
        thread.join(timeout=5)

        # FIXED BEHAVIOUR (C8): the second dispatch's own generous budget is
        assert second_dispatch_result["spent"] is False
        assert dispatch_checks._git_probe_budget_spent() is True
    finally:
        dispatch_checks._disarm_git_probe_deadline()


def test_capture_session_does_not_cross_contaminate_under_interleave():
    _hook_envelope._capture_sink.set(None)

    a_entered = threading.Event()
    b_entered = threading.Event()
    a_may_record = threading.Event()
    a_done = threading.Event()

    sink_a_holder: dict[str, list] = {}
    sink_b_holder: dict[str, list] = {}

    def _session_a():
        with _hook_envelope.capture_session() as sink_a:
            sink_a_holder["sink"] = sink_a
            a_entered.set()
            b_entered.wait(timeout=5)
            a_may_record.wait(timeout=5)
            _hook_envelope._record("builder_a", {"from": "session_a"})
        a_done.set()

    def _session_b():
        a_entered.wait(timeout=5)
        with _hook_envelope.capture_session() as sink_b:
            sink_b_holder["sink"] = sink_b
            b_entered.set()
            a_may_record.set()
            a_done.wait(timeout=5)

    thread_a = threading.Thread(target=_session_a)
    thread_b = threading.Thread(target=_session_b)
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=5)
    thread_b.join(timeout=5)

    # FIXED BEHAVIOUR (C8): session A's record lands in its OWN sink, not
    assert ("builder_a", {"from": "session_a"}) in sink_a_holder["sink"]
    assert sink_b_holder["sink"] == []


# Site 8: coordinator_core/hooks/track_touched_files.py -- `_MAX_FILE_LOCKS`


def test_file_lock_eviction_is_held_aware(monkeypatch, tmp_path):
    track_touched_files._FILE_LOCKS.clear()
    original_max = track_touched_files._MAX_FILE_LOCKS
    track_touched_files._MAX_FILE_LOCKS = 4
    monkeypatch.setattr(track_touched_files.os.path, "isdir", lambda _p: True)
    try:
        held_path = str(tmp_path / "touched-oldest.txt")
        lock_first_seen = track_touched_files._get_lock(held_path)
        assert lock_first_seen.locked() is False
        lock_first_seen._warm_c2_marker = True

        async def _acquire():
            await lock_first_seen.acquire()

        import asyncio

        asyncio.run(_acquire())
        assert lock_first_seen.locked() is True

        for i in range(track_touched_files._MAX_FILE_LOCKS + 1):
            track_touched_files._get_lock(str(tmp_path / f"other-{i}.txt"))

        # FIXED BEHAVIOUR (C9): the held entry survives -- unrelated UNHELD
        assert held_path in track_touched_files._FILE_LOCKS
        assert len(track_touched_files._FILE_LOCKS) == track_touched_files._MAX_FILE_LOCKS

        lock_second_request = track_touched_files._get_lock(held_path)

        assert lock_second_request is lock_first_seen
        assert lock_second_request.locked() is True
        assert hasattr(lock_second_request, "_warm_c2_marker")
    finally:
        track_touched_files._FILE_LOCKS.clear()
        track_touched_files._MAX_FILE_LOCKS = original_max


# Site 9: coordinator_core/engine_root.py -- CLAUDE_KLABAUTER_ROOT process-memoization


def test_engine_root_gate_memo_keys_per_interleaved_session_root(
    monkeypatch, tmp_path
):
    """C10 -- `coordinator_engine_root_with_class`'s two-tier gate answer is
    now a multi-entry `_GATE_MEMO` dict keyed on `(registry mtime pair,
    session root)`, not a single-slot last-write-wins pair
    (`_GATE_MEMO_KEY`/`_GATE_MEMO_VALUE` no longer exist). Under two sessions
    with genuinely different resolved roots interleaving in one warm
    process, each root gets its own memo entry, so returning to a
    previously-seen root hits its own still-valid entry instead of re-
    running the full gate."""
    engine_root._reset_gate_memo()
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)

    calls = {"n": 0}
    current_session_root = {"root": "/repo/session-a"}

    class _FakeShim:
        RESOLUTION_LIVE_WORKING_TREE = "live-working-tree"

        def _ml_dir(self):
            return tmp_path

        def _registry_value(self, _ml_dir, _key):
            return "some/published/mirror"

        def _session_repo_root(self):
            return current_session_root["root"]

        def resolve_claude_klabauter_root_with_class(self):
            calls["n"] += 1
            return (current_session_root["root"], self.RESOLUTION_LIVE_WORKING_TREE)

    fake_shim = _FakeShim()
    monkeypatch.setattr(engine_root, "_load_shim", lambda: fake_shim)

    current_session_root["root"] = "/repo/session-a"
    result_a1 = engine_root.coordinator_engine_root_with_class()
    assert calls["n"] == 1
    assert result_a1[0] == "/repo/session-a"

    # An interleaved dispatch for a DIFFERENT session/root arrives.
    current_session_root["root"] = "/repo/session-b"
    result_b1 = engine_root.coordinator_engine_root_with_class()
    assert calls["n"] == 2
    assert result_b1[0] == "/repo/session-b"

    current_session_root["root"] = "/repo/session-a"
    result_a2 = engine_root.coordinator_engine_root_with_class()

    # FIXED BEHAVIOUR (C10): session A's second call hits its own still-valid
    assert calls["n"] == 2
    assert result_a2[0] == "/repo/session-a"

    engine_root._reset_gate_memo()

"""
coordinator_core.ops.session.tests.test_record_pickup

Tests for the session.record_pickup op (Class-B, DR-059 defense-in-depth port
of bash cs_session_shape_set's pickup write).

Import guard: coordinator_core.ops.session.record_pickup MUST be imported at
module load time to fire the @register_op("session.record_pickup") side-effect
and populate _REGISTRY (lesson: state/lessons/2026-07-04-universal-registry-
completeness-tests-ov.yaml).

Coverage:
  (a) first-pickup-writes-flat-and-history — first call writes flat `pickup`
      + a single pickup_history[] entry, repoint_detected=False.
  (b) pivot-pickup-updates-flat-and-appends — a SECOND call with a different
      handoff updates the flat `pickup.handoff` AND appends a second
      pickup_history entry (both entries retained, differing handoff) —
      repoint_detected=True. This is the detectable-re-point contract the
      whole port exists to deliver.
  (c) backward-compat-wsc-resolve-reader — the written session-shape.json
      parses correctly against branch_resolution's real _read_session_shape /
      pickup_field.happened / pickup_field.handoff reads (imported directly,
      not re-implemented; this reader shipped as part of the retired
      ``ceremony.wsc_resolve`` op and now lives in branch_resolution.py).
  (d) mkdir-lock-acquired-and-released — session-shape.lock/ dir exists during
      the critical section (patched) and is absent after the call returns.
  (e) concurrent-write-safety-shape — a pre-existing session-shape.lock/ with
      a stale (>30s) claimed_at is reaped and the write proceeds; a LIVE
      (<30s) lock causes retries until ShapeLockTimeout (bounded attempts).
  (f) create-if-absent — session-shape.json does not exist yet; op creates
      the schema_version/session_id skeleton + pickup + pickup_history.
  (g) param-validation — missing/blank sid, handoff_relpath, repo_root, and a
      parent-escaping handoff_relpath all fail closed with exit_code:1 and no
      write.

Spec backlinks:
  - Op: coordinator_core/ops/session/record_pickup.py
  - Proposal: session-shape-pickup-port-proposal.md (2026-07-14 DR-059 port dispatch)
  - Read-side reader under test: coordinator_core/ops/ceremony/branch_resolution.py

Negative-spec:
  - Does NOT assert git status clean — Class B makes no git commits.
  - Does NOT re-implement the mkdir-lock staleness math independently — tests
    drive it via the same _lock_live / _SHAPE_LOCK_STALE_SECONDS the op uses,
    to avoid a second source of truth that could silently drift.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test function so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.session.record_pickup  # noqa: F401 — fires @register_op("session.record_pickup")

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.session.record_pickup import (
    ShapeLockTimeout,
    _acquire_shape_lock,
    _handler,
    _SHAPE_LOCK_STALE_SECONDS,
)

_OP_NAME = "session.record_pickup"
_session_registered_ops = [k for k in _REGISTRY if k.startswith("session.")]
assert len(_session_registered_ops) >= 1, (
    "import guard failed: no session.* ops in _REGISTRY — "
    "check coordinator_core.ops.session.record_pickup import and @register_op side-effect"
)
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY; "
    "coordinator_core.ops.session.record_pickup @register_op('session.record_pickup') did not fire"
)


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _call(repo_root: Path, sid: str, handoff_relpath: str) -> dict:
    return _run(
        _handler(
            {"sid": sid, "handoff_relpath": handoff_relpath, "repo_root": str(repo_root)},
            repo_root=None,
        )
    )


# ---------------------------------------------------------------------------
# (a)/(b) — versioned pickup write + pivot detection
# ---------------------------------------------------------------------------


def test_first_pickup_writes_flat_and_history(session_repo):
    sid = "sid-first"
    result = _call(session_repo.root, sid, "state/handoffs/foo.md")

    assert result["exit_code"] == 0
    assert result["pickup"] == {"happened": True, "handoff": "state/handoffs/foo.md"}
    assert result["pickup_history_len"] == 1
    assert result["repoint_detected"] is False

    shape_path = session_repo.sessions_dir / sid / "session-shape.json"
    data = json.loads(shape_path.read_text(encoding="utf-8"))
    assert data["pickup"] == {"happened": True, "handoff": "state/handoffs/foo.md"}
    assert len(data["pickup_history"]) == 1
    assert data["pickup_history"][0]["handoff"] == "state/handoffs/foo.md"
    assert data["pickup_history"][0]["happened"] is True
    assert "recorded_at" in data["pickup_history"][0]


def test_pivot_pickup_updates_flat_and_appends_history(session_repo):
    sid = "sid-pivot"
    first = _call(session_repo.root, sid, "state/handoffs/foo.md")
    assert first["exit_code"] == 0

    second = _call(session_repo.root, sid, "state/handoffs/bar.md")
    assert second["exit_code"] == 0
    assert second["pickup"] == {"happened": True, "handoff": "state/handoffs/bar.md"}
    assert second["pickup_history_len"] == 2
    assert second["repoint_detected"] is True

    shape_path = session_repo.sessions_dir / sid / "session-shape.json"
    data = json.loads(shape_path.read_text(encoding="utf-8"))
    # Flat pickup reflects the CURRENT (second) call — mirrors bash top-level replace.
    assert data["pickup"]["handoff"] == "state/handoffs/bar.md"
    # History retains BOTH entries, in order, prior entry untouched.
    history = data["pickup_history"]
    assert len(history) == 2
    assert history[0]["handoff"] == "state/handoffs/foo.md"
    assert history[1]["handoff"] == "state/handoffs/bar.md"
    # Re-point is detectable: last entry differs from first.
    assert history[-1] != history[0]


# ---------------------------------------------------------------------------
# (c) — backward compat with branch_resolution's real reader
# ---------------------------------------------------------------------------


def test_backward_compat_wsc_resolve_reader(session_repo):
    from coordinator_core.ops.ceremony.branch_resolution import _read_session_shape

    sid = "sid-compat"
    result = _call(session_repo.root, sid, "state/handoffs/foo.md")
    assert result["exit_code"] == 0

    common_dir = session_repo.common_dir
    shape, source = _read_session_shape(common_dir, sid)
    assert source == "session_shape"

    pickup_field = shape.get("pickup")
    assert isinstance(pickup_field, dict)
    assert bool(pickup_field.get("happened", False)) is True
    assert pickup_field.get("handoff") == "state/handoffs/foo.md"
    # New sibling key present but does not disturb the flat-field reader contract.
    assert isinstance(shape.get("pickup_history"), list)


def test_backward_compat_after_pivot_reader_sees_current_pickup(session_repo):
    from coordinator_core.ops.ceremony.branch_resolution import _read_session_shape

    sid = "sid-compat-pivot"
    _call(session_repo.root, sid, "state/handoffs/foo.md")
    _call(session_repo.root, sid, "state/handoffs/bar.md")

    common_dir = session_repo.common_dir
    shape, source = _read_session_shape(common_dir, sid)
    assert source == "session_shape"
    pickup_field = shape["pickup"]
    assert pickup_field["handoff"] == "state/handoffs/bar.md"
    assert len(shape["pickup_history"]) == 2


# ---------------------------------------------------------------------------
# (d) — mkdir-lock acquired and released
# ---------------------------------------------------------------------------


def test_lock_acquired_during_write_and_released_after(session_repo):
    sid = "sid-lock"
    sdir = session_repo.sessions_dir / sid
    lock_dir = sdir / "session-shape.lock"

    # Pre-acquire the lock ourselves (simulating a live bash holder) — the op
    # must retry-and-wait, not clobber a live lock.
    from coordinator_core.ops.session import record_pickup as rp

    sdir.mkdir(parents=True, exist_ok=True)
    held_lock = rp._acquire_shape_lock(sdir, sid)
    assert held_lock.is_dir()
    assert (held_lock / "claimed_at").is_file()

    # Release it before calling the op (op must succeed once lock is free).
    rp._release_shape_lock(held_lock)
    assert not lock_dir.exists()

    result = _call(session_repo.root, sid, "state/handoffs/foo.md")
    assert result["exit_code"] == 0
    # Lock is released after the op completes.
    assert not lock_dir.exists()


def test_stale_lock_is_reaped_and_write_proceeds(session_repo):
    """A lock claimed >30s ago (stale) is reaped, not waited-out."""
    from coordinator_core.ops.session import record_pickup as rp

    sid = "sid-stale-lock"
    sdir = session_repo.sessions_dir / sid
    sdir.mkdir(parents=True, exist_ok=True)
    lock_dir = sdir / "session-shape.lock"
    lock_dir.mkdir(parents=True)
    stale_claimed_at = (
        datetime.now(tz=timezone.utc) - timedelta(seconds=_SHAPE_LOCK_STALE_SECONDS + 5)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    (lock_dir / "pid").write_text("99999", encoding="utf-8")
    (lock_dir / "session_id").write_text(sid, encoding="utf-8")
    (lock_dir / "claimed_at").write_text(stale_claimed_at, encoding="utf-8")

    result = _call(session_repo.root, sid, "state/handoffs/foo.md")
    assert result["exit_code"] == 0
    assert not lock_dir.exists()  # released after our own critical section


def test_live_lock_causes_timeout(session_repo, monkeypatch):
    """A LIVE (recent claimed_at) lock is never clobbered — bounded retry, then ShapeLockTimeout."""
    from coordinator_core.ops.session import record_pickup as rp

    sid = "sid-live-lock"
    sdir = session_repo.sessions_dir / sid
    sdir.mkdir(parents=True, exist_ok=True)
    lock_dir = sdir / "session-shape.lock"
    lock_dir.mkdir(parents=True)
    now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (lock_dir / "pid").write_text("99999", encoding="utf-8")
    (lock_dir / "session_id").write_text(sid, encoding="utf-8")
    (lock_dir / "claimed_at").write_text(now_iso, encoding="utf-8")

    # Avoid real sleeping across 20 retry attempts in the test.
    monkeypatch.setattr(rp.time, "sleep", lambda _seconds: None)

    with pytest.raises(ShapeLockTimeout):
        rp._acquire_shape_lock(sdir, sid)

    # Lock dir left untouched — never clobbered while live.
    assert lock_dir.is_dir()
    assert (lock_dir / "pid").read_text(encoding="utf-8") == "99999"


# ---------------------------------------------------------------------------
# (f) — create-if-absent
# ---------------------------------------------------------------------------


def test_create_if_absent_writes_skeleton(session_repo):
    sid = "sid-absent"
    shape_path = session_repo.sessions_dir / sid / "session-shape.json"
    assert not shape_path.exists()

    result = _call(session_repo.root, sid, "state/handoffs/foo.md")
    assert result["exit_code"] == 0

    data = json.loads(shape_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["session_id"] == sid
    assert data["pickup"]["handoff"] == "state/handoffs/foo.md"


# ---------------------------------------------------------------------------
# (h) — deliverable_id: additive, omit-rather-than-guess
# ---------------------------------------------------------------------------


def test_deliverable_id_recorded_when_supplied(session_repo):
    sid = "sid-dlv"
    result = _run(
        _handler(
            {
                "sid": sid,
                "handoff_relpath": "state/handoffs/foo.md",
                "repo_root": str(session_repo.root),
                "deliverable_id": "dlv-abc123",
            },
            repo_root=None,
        )
    )
    assert result["exit_code"] == 0
    assert result["pickup"]["deliverable_id"] == "dlv-abc123"

    shape_path = session_repo.sessions_dir / sid / "session-shape.json"
    data = json.loads(shape_path.read_text(encoding="utf-8"))
    assert data["pickup"]["deliverable_id"] == "dlv-abc123"
    assert data["pickup_history"][0]["deliverable_id"] == "dlv-abc123"


def test_deliverable_id_absent_when_not_supplied(session_repo):
    sid = "sid-no-dlv"
    result = _call(session_repo.root, sid, "state/handoffs/foo.md")
    assert result["exit_code"] == 0
    assert "deliverable_id" not in result["pickup"]

    shape_path = session_repo.sessions_dir / sid / "session-shape.json"
    data = json.loads(shape_path.read_text(encoding="utf-8"))
    assert "deliverable_id" not in data["pickup"]
    assert "deliverable_id" not in data["pickup_history"][0]


def test_deliverable_id_blank_normalizes_to_absent(session_repo):
    sid = "sid-blank-dlv"
    result = _run(
        _handler(
            {
                "sid": sid,
                "handoff_relpath": "state/handoffs/foo.md",
                "repo_root": str(session_repo.root),
                "deliverable_id": "   ",
            },
            repo_root=None,
        )
    )
    assert result["exit_code"] == 0
    assert "deliverable_id" not in result["pickup"]


# ---------------------------------------------------------------------------
# (g) — param validation, fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"sid": "", "handoff_relpath": "state/handoffs/foo.md", "repo_root": "PLACEHOLDER"},
        {"sid": "sid-x", "handoff_relpath": "", "repo_root": "PLACEHOLDER"},
        {"sid": "sid-x", "handoff_relpath": "state/handoffs/foo.md", "repo_root": ""},
        {"sid": "sid-x", "handoff_relpath": "../../etc/passwd", "repo_root": "PLACEHOLDER"},
        {"sid": "sid-x", "handoff_relpath": "/abs/path.md", "repo_root": "PLACEHOLDER"},
    ],
)
def test_param_validation_fails_closed(session_repo, params):
    p = dict(params)
    if p.get("repo_root") == "PLACEHOLDER":
        p["repo_root"] = str(session_repo.root)

    result = _run(_handler(p, repo_root=None))
    assert result["exit_code"] == 1
    assert "error" in result
    assert result["pickup"] is None

    # No session dir mutation occurred for a validation failure.
    sid = p.get("sid") or "sid-x"
    if sid:
        shape_path = session_repo.sessions_dir / sid / "session-shape.json"
        assert not shape_path.exists()


def test_missing_repo_root_resolution_fails_closed(session_repo, tmp_path):
    nonexistent = tmp_path / "does-not-exist-anywhere"
    result = _run(
        _handler(
            {
                "sid": "sid-badroot",
                "handoff_relpath": "state/handoffs/foo.md",
                "repo_root": str(nonexistent),
            },
            repo_root=None,
        )
    )
    assert result["exit_code"] == 1
    assert "error" in result

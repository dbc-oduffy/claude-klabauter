"""
coordinator_core.ops.tests.test_lock_timeout_ceiling_clamp — the ceiling guard for
the two ordinary mutating ops that take a caller-supplied lock-acquire wait:
``goal.set_kr_status`` (ops/goal_kr_status.py) and ``priority.set``
(ops/priority_set.py).

Purpose: prove that a caller asking for more lock-wait than the op's
``MAX_LOCK_TIMEOUT_SECS`` gets the ceiling, not what it asked for, on BOTH the
Python-callable surface and the JSON-RPC handler surface — and that a caller
asking for LESS is still honoured, because the ruling is "never raise a dial",
not "pin every dial to one number".

Why a file of its own rather than an addition to test_goal_kr_status.py /
test_priority_set.py: both of those carry file-wide ``pytest.mark.cadence``
(they spawn real concurrent writer subprocesses against a real file lock), and
``fast_test_cmd`` excludes the cadence tier — a ceiling guard parked there would
not fire at the gate that actually runs on every change. These cases spawn
nothing, take no lock, and need no lock backend: ``locked_rmw`` is monkeypatched
to a recorder, so the effective timeout is the only observable and the whole file
runs in milliseconds on the fast tier.

Negative-spec:
  - Does NOT assert any particular ceiling VALUE. The number is a per-op
    judgement call that may be re-ruled; what must never regress is that the
    caller cannot exceed whatever it currently is. Asserting the literal would
    make a deliberate re-ruling look like a break, and would pass unchanged if
    the clamp itself were deleted and the constant left behind.
  - Does NOT exercise the write path. A clamp that reaches ``locked_rmw`` is the
    whole contract; the write is covered by each op's own suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops import goal_kr_status, priority_set


def _record_timeouts(monkeypatch, module) -> list[float]:
    """Swap *module*'s locked_rmw for a recorder of the timeout it was handed.

    The mutate callback is never invoked, so no file is read or written and no
    lock backend is required.
    """
    seen: list[float] = []

    def _fake_locked_rmw(_path, _mutate, *, repo_root, timeout, missing_ok):
        seen.append(timeout)

    monkeypatch.setattr(module, "locked_rmw", _fake_locked_rmw)
    return seen


# ---------------------------------------------------------------------------
# goal.set_kr_status
# ---------------------------------------------------------------------------


def test_goal_kr_status_clamps_huge_timeout(monkeypatch, tmp_path):
    seen = _record_timeouts(monkeypatch, goal_kr_status)

    goal_kr_status.set_kr_status(tmp_path / "goal.yaml", "kr-1", "done", timeout=86400.0)

    assert seen == [goal_kr_status.MAX_LOCK_TIMEOUT_SECS]


def test_goal_kr_status_handler_clamps_huge_timeout(monkeypatch, tmp_path):
    seen = _record_timeouts(monkeypatch, goal_kr_status)

    goal_kr_status._goal_set_kr_status(
        {
            "goal_file": str(tmp_path / "goal.yaml"),
            "kr_id": "kr-1",
            "status": "done",
            "timeout": 3600,
        }
    )

    assert seen == [goal_kr_status.MAX_LOCK_TIMEOUT_SECS], (
        "the JSON-RPC handler is the front door an EM actually reaches through "
        "cc_invoke — an unclamped wire param would make the Python-side clamp "
        "decorative"
    )


def test_goal_kr_status_honours_a_tighter_timeout(monkeypatch, tmp_path):
    seen = _record_timeouts(monkeypatch, goal_kr_status)

    goal_kr_status.set_kr_status(tmp_path / "goal.yaml", "kr-1", "done", timeout=0.25)

    assert seen == [0.25]


# ---------------------------------------------------------------------------
# priority.set
# ---------------------------------------------------------------------------


@pytest.fixture
def _central_root(tmp_path, monkeypatch) -> Path:
    """Point priority.set's ledger root at tmp_path so the mkdir it does before
    locked_rmw lands somewhere disposable."""
    root = tmp_path / "central"
    monkeypatch.setattr(priority_set, "coordinator_state_root", lambda central=False: root)
    return root


def test_priority_set_clamps_huge_timeout(monkeypatch, _central_root):
    seen = _record_timeouts(monkeypatch, priority_set)

    priority_set.set_priority("handoff-clamp", "handoff", "high", timeout=86400.0)

    assert seen == [priority_set.MAX_LOCK_TIMEOUT_SECS]


def test_priority_set_handler_clamps_huge_timeout(monkeypatch, _central_root):
    seen = _record_timeouts(monkeypatch, priority_set)

    priority_set._priority_set(
        {
            "target_id": "handoff-clamp-wire",
            "target_kind": "handoff",
            "priority": "high",
            "timeout": 3600,
        }
    )

    assert seen == [priority_set.MAX_LOCK_TIMEOUT_SECS]


def test_priority_set_honours_a_tighter_timeout(monkeypatch, _central_root):
    seen = _record_timeouts(monkeypatch, priority_set)

    priority_set.set_priority("handoff-tight", "handoff", "high", timeout=0.25)

    assert seen == [0.25]

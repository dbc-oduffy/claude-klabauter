"""Pin for `wire_contract.publish_contention_wait_secs` (C1,
docs/plans/2026-08-30-a-second-percolate-round-stops-sleeping.md).

Default posture is deny-at-once (0.0); `COORDINATOR_ALLOW_PERCOLATE_QUEUE`
opts back into the existing `contended_lock_wait_secs()` clamp/ceiling, with
no second source of truth for the 180s number.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate import wire_contract  # noqa: E402
from coordinator_core.locked_write import (  # noqa: E402
    CONTENDED_LOCK_WAIT_ENV,
    contended_lock_wait_secs,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(wire_contract.COORDINATOR_ALLOW_PERCOLATE_QUEUE_ENV, raising=False)
    monkeypatch.delenv(CONTENDED_LOCK_WAIT_ENV, raising=False)


def test_default_denies_at_once():
    assert wire_contract.publish_contention_wait_secs() == 0.0


@pytest.mark.parametrize("falsy", ["", "0", "false", "no", "off", "FALSE", "Off"])
def test_every_falsy_spelling_denies_at_once(monkeypatch, falsy):
    monkeypatch.setenv(wire_contract.COORDINATOR_ALLOW_PERCOLATE_QUEUE_ENV, falsy)
    assert wire_contract.publish_contention_wait_secs() == 0.0


def test_key_set_resolves_contended_lock_wait_secs(monkeypatch):
    monkeypatch.setenv(wire_contract.COORDINATOR_ALLOW_PERCOLATE_QUEUE_ENV, "1")
    # Both halves are load-bearing: the delegation check alone would pass against
    # any implementation that forwards to `contended_lock_wait_secs()`, including
    # one forwarding a wrong default. The literal pins the value the queueing path
    # actually restores -- the same independent-literal discipline the
    # CONTENDED_LOCK_WAIT_SECS ratchet test uses.
    assert wire_contract.publish_contention_wait_secs() == contended_lock_wait_secs()
    assert wire_contract.publish_contention_wait_secs() == 180.0


def test_key_set_alongside_narrowing_override_resolves_narrowed(monkeypatch):
    monkeypatch.setenv(wire_contract.COORDINATOR_ALLOW_PERCOLATE_QUEUE_ENV, "1")
    monkeypatch.setenv(CONTENDED_LOCK_WAIT_ENV, "30")
    assert wire_contract.publish_contention_wait_secs() == 30.0


def test_key_set_alongside_over_ceiling_override_clamps_at_ceiling(monkeypatch):
    monkeypatch.setenv(wire_contract.COORDINATOR_ALLOW_PERCOLATE_QUEUE_ENV, "1")
    monkeypatch.setenv(CONTENDED_LOCK_WAIT_ENV, "900")
    assert wire_contract.publish_contention_wait_secs() == 180.0


# Non-positive/unparseable `CONTENDED_LOCK_WAIT_ENV` falls back to the 180s
# ceiling, NOT 0.0 -- reviewer finding (code-reviewer P2): this delegates to
# `contended_lock_wait_secs()`, whose own docstring says a malformed value
# "falls back to the ceiling rather than raising" (locked_write.py). Pinned
# here so the delegation itself, not just the underlying function, is
# verified against the real ceiling rather than assumed.
@pytest.mark.parametrize("bad", ["0", "-5", "abc", "nan"])
def test_key_set_alongside_non_positive_or_unparseable_override_clamps_at_ceiling(
    monkeypatch, bad
):
    monkeypatch.setenv(wire_contract.COORDINATOR_ALLOW_PERCOLATE_QUEUE_ENV, "1")
    monkeypatch.setenv(CONTENDED_LOCK_WAIT_ENV, bad)
    assert wire_contract.publish_contention_wait_secs() == 180.0


# ---------------------------------------------------------------------------
# `lock_busy_message` content contract (staff-eng-review finding 4, C3).
# `percolate-round.py`, `percolate-mirror.py` (via `_round._lock_busy_message`),
# and `publish.py`'s own inline BUSY branch all delegate to this one builder
# now, so its text is asserted here, directly on the function that owns it,
# rather than re-derived in each of those four entrypoint suites. Those
# suites keep only what is genuinely per-entrypoint: exit code, timing, which
# lock is taken, and (where relevant) a parity check that they emit exactly
# this builder's output.
# ---------------------------------------------------------------------------

def test_lock_busy_message_content_contract():
    exc = Exception("held by pid 1234 since 2026-08-30T00:00:00Z (within 0s)")
    msg = wire_contract.lock_busy_message("some-dest", exc)
    # Holder named -- the underlying LockTimeout's own text is folded in.
    assert "held by pid 1234" in msg
    # Mechanism page named.
    assert "docs/reference/percolate-lock-contention.md" in msg
    # Override-key token absent (B6: unresolved-audience degrades to silence
    # about the bypass mechanism).
    assert wire_contract.COORDINATOR_ALLOW_PERCOLATE_QUEUE_ENV not in msg
    # No re-run/retry imperative (DR-344 respawn-risk finding).
    assert "re-run" not in msg.lower()
    assert "retry" not in msg.lower()
    assert "waited" not in msg.lower()

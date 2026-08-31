"""test_percolate_round_lock_denies_fast — B6 regression: a second round
against an already-held destination refuses AT ONCE instead of sleeping.

Binds docs/plans/2026-08-30-a-second-percolate-round-stops-sleeping.md § C2:
`_cmd_round_default` now passes `publish_contention_wait_secs()` (default
0.0 — deny at once) to `_round_held_lock`'s `timeout=`, in place of the old
`_round_lock_wait_secs()` (which always resolved to the full 180s wait). The
opt-in queueing path (`COORDINATOR_ALLOW_PERCOLATE_QUEUE=1`) still resolves
through `contended_lock_wait_secs()`, same clamp, same ceiling.

Uses a REAL held lock (`_round_held_lock` is documented non-reentrant even
WITHIN one process, coordinator_core/locked_write.py's own module
docstring), never a fake context manager -- so the second acquisition
attempt below hits the real `LockTimeout` path, including the real
`_describe_holder` metadata read `_lock_busy_message` now embeds verbatim.

Run: python -m pytest coordinator/bin/tests/test_percolate_round_lock_denies_fast.py -q
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_round_lock_denies_fast", _BIN_DIR / "percolate-round.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _run_cmd_round_default_under_held_lock(dest: Path) -> tuple:
    """Holds the real lock on `dest`, then calls `_cmd_round_default` against
    that SAME dest -- its own `with _round_held_lock(...)` is the very first
    thing the function reaches (before any subprocess/branch0 machinery), so
    this exercises the real timeout path with nothing else stubbed.

    Returns `(rc, elapsed_seconds, stderr_text)`.
    """
    _mod._bootstrap_engine()
    args = SimpleNamespace(yes=True, invocation_authorized=False, no_publish=False)
    with _mod._round_held_lock(Path(dest), holder_label="peer:percolate-round"):
        with tempfile.TemporaryDirectory() as tmpdir:
            buf = io.StringIO()
            start = time.monotonic()
            with contextlib.redirect_stderr(buf):
                rc = _mod._cmd_round_default(
                    args, "alpha", "unused-percolate-root",
                    "unused-source-dir", str(dest), Path(tmpdir),
                )
            elapsed = time.monotonic() - start
    return rc, elapsed, buf.getvalue()


def test_default_deny_denies_fast_and_message_is_pointer_only(tmp_path, monkeypatch):
    """Leg 1 (default posture, `COORDINATOR_ALLOW_PERCOLATE_QUEUE` unset):
    exits 75 in well under a second. The refusal text's own content contract
    (holder named, mechanism page present, override key/re-run imperative
    absent) is asserted once, directly on `wire_contract.lock_busy_message`
    (test_wire_contract_publish_contention.py); this leg only checks the
    holder's pid metadata is folded in, per-entrypoint deny-at-once timing."""
    monkeypatch.delenv("COORDINATOR_ALLOW_PERCOLATE_QUEUE", raising=False)
    dest = tmp_path / "dest"
    dest.mkdir()

    rc, elapsed, err = _run_cmd_round_default_under_held_lock(dest)

    assert rc == _mod._EXIT_LOCK_BUSY
    assert elapsed < 1.0, f"deny-at-once took {elapsed}s"
    assert "pid=" in err  # `_describe_holder`'s own holder metadata


def test_allow_queue_enters_poll_loop_and_still_denies(tmp_path, monkeypatch):
    """Leg 2: with the opt-in queueing env var set and a short probe wait
    (`COORDINATOR_LOCK_WAIT_SECS`, narrowing-only), the round actually
    polls -- and, since the lock stays held the whole time, still exits 75.
    A short wait here is load-bearing: without it this leg sleeps the full
    180s ceiling and wrecks the suite."""
    monkeypatch.setenv("COORDINATOR_ALLOW_PERCOLATE_QUEUE", "1")
    monkeypatch.setenv("COORDINATOR_LOCK_WAIT_SECS", "0.3")
    dest = tmp_path / "dest"
    dest.mkdir()

    rc, elapsed, err = _run_cmd_round_default_under_held_lock(dest)

    assert rc == _mod._EXIT_LOCK_BUSY
    assert elapsed < 5.0, f"probe wait should be seconds, not the 180s ceiling: {elapsed}s"
    assert "held by" in err
    # Review: coordinatorcode-reviewer (finding 6) — leg 2 keeps its own
    # negatives even though leg 1 centralises the content contract onto
    # wire_contract.lock_busy_message: this exception is a real LockTimeout
    # raised after an actual multi-attempt poll loop, a genuinely different
    # `exc` shape (`within {timeout}s`) than leg 1's instant single-try
    # deny. A regression reintroducing a retry imperative only on the
    # polling path would be caught by no other test.
    assert "COORDINATOR_ALLOW_PERCOLATE_QUEUE" not in err
    assert "Re-run" not in err
    assert "retry" not in err.lower()


@pytest.mark.parametrize("dest_name", ["dest-a"])
def test_publish_contention_wait_secs_wired_to_lock_call(tmp_path, monkeypatch, dest_name):
    """Unit-level pin: `_cmd_round_default`'s lock acquisition passes
    `publish_contention_wait_secs()`, not the old `_round_lock_wait_secs()`
    (which always resolved to the full 180s wait regardless of the deny
    posture)."""
    _mod._bootstrap_engine()
    calls = []

    class _RecordingLockCtx:
        def __init__(self, target, **kw):
            calls.append(kw)

        def __enter__(self):
            raise _mod._RoundLockTimeout("stand-in, never reached past the assertion below")

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(_mod, "_round_held_lock", lambda target, **kw: _RecordingLockCtx(target, **kw))
    monkeypatch.delenv("COORDINATOR_ALLOW_PERCOLATE_QUEUE", raising=False)

    dest = tmp_path / dest_name
    dest.mkdir()
    args = SimpleNamespace(yes=True, invocation_authorized=False, no_publish=False)
    with tempfile.TemporaryDirectory() as tmpdir:
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            _mod._cmd_round_default(
                args, "alpha", "unused-percolate-root",
                "unused-source-dir", str(dest), Path(tmpdir),
            )

    assert len(calls) == 1
    assert calls[0]["timeout"] == _mod.publish_contention_wait_secs()
    assert calls[0]["timeout"] == 0.0

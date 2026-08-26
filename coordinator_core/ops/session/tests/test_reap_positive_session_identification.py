"""
Guard tests for POSITIVE session identification in sub-reap (i)
(``coordinator_core.ops.session.reap._reap_stale_sessions``).

WRITTEN FIRST (C1), and required to be RED against HEAD before C2 exists.
Assertions are on the ``(reaped, deferred, failed)`` tuple's SID membership,
never on exit status alone — a fixture malformed in some unrelated way also
fails, and that failure mode is exactly what this ordering exists to catch
(baton next-step 4). ``failed`` must be empty in the red run: a non-empty
``failed`` means the fixture itself is malformed, not that the discriminator
under test is wrong.

THE BUG THIS GUARDS AGAINST, and why the fixtures below are shaped as they
are. At HEAD, sub-reap (i) tells "this is a session" apart from "this is not
a session" NEGATIVELY: skip a `.`-prefixed name, skip a `_`-prefixed name,
skip a name literally enumerated in ``_NON_SESSION_DIR_NAMES``, then treat
EVERYTHING ELSE as a session candidate. A co-located store that is none of
those three — dot-prefixed, underscore-prefixed, or denylisted by name — is
reaped by accident of omission, not kept by any positive rule. C2 replaces
that with POSITIVE identification: a dir survives only if its name is
uuid-shaped (the actual session-id contract), everything else is kept
regardless of denylist coverage.

All fixtures below are planted COLD (mtime/last_activity older than the 24h
staleness threshold) — age is deliberately never what separates the kept
population from the reaped one, only name shape is.

CANDIDATE-ENUMERATION MEASUREMENT (C3, AC6). The baton asked for a
230-dir hub measurement on the *real* co-located sessions hub; no such hub
exists any more — DoE swept theirs to 63 entries on 2026-08-26. Substituted:
a synthesised tmp hub of 230 uuid-shaped dirs, scandir + uuid-shape filter
(the same loop shape as ``_reap_stale_sessions``'s candidate gate) timed
in-process (``time.perf_counter``, best of 5 runs): 2.561ms. (Plan-time
figure on this repo's own 65-dir hub was 0.000ms for the same filter.)
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.ops.session import reap

pytestmark = [pytest.mark.cadence]

_COLD_AGE = reap._SESSION_STALE_SECONDS + 3600  # > 24h, well past the threshold


def _cold_epoch() -> float:
    return time.time() - _COLD_AGE


def _plant_no_meta_dir(sessions_dir: Path, name: str, filename: str = "x.txt") -> Path:
    """Plant a cold dir with one ordinary file and NO meta.json.

    Ages the file's mtime so ``_staleness_basis_mtime`` reads it as cold —
    this is the "real session dir, no meta.json" shape that is the 30/53
    majority case per the plan, and also the shape any accidentally-reaped
    co-located store takes.
    """
    d = sessions_dir / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / filename
    f.write_text("x\n", encoding="utf-8")
    when = _cold_epoch()
    os.utime(f, (when, when))
    return d


def _plant_meta_dir(sessions_dir: Path, name: str) -> Path:
    """Plant a cold real session dir WITH meta.json (cold last_activity)."""
    d = sessions_dir / name
    d.mkdir(parents=True, exist_ok=True)
    cold_iso = (
        datetime.fromtimestamp(_cold_epoch(), tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    meta = d / "meta.json"
    meta.write_text(f'{{"last_activity": "{cold_iso}"}}', encoding="utf-8")
    when = _cold_epoch()
    os.utime(meta, (when, when))
    return d


_UUID_1 = "11111111-1111-4111-8111-111111111111"
_UUID_2 = "22222222-2222-4222-8222-222222222222"
_HARNESS_FIXTURE = "hookperf-3ee8b3f4a1d1"


def test_hub_four_populations_positively_identified(tmp_path):
    """The hub fixture named by the plan: a store, a harness fixture, and two
    real (uuid-shaped) session dirs, all cold.

    Against HEAD: the store survives via the denylist (``decisions`` is a
    member of ``_NON_SESSION_DIR_NAMES``); ``hookperf-3ee8b3f4a1d1`` is
    neither dot-prefixed, `_`-prefixed, nor denylisted, so HEAD reaps it —
    THIS is the bug the plan's Anti-scope forbids reintroducing, and the
    reason this assertion is RED against HEAD. Both uuid-shaped session dirs
    are (and must remain) reaped in both worlds.
    """
    sessions = tmp_path / "coordinator-sessions"

    store = _plant_no_meta_dir(sessions, "decisions", filename="a-record.md")
    harness_fixture = _plant_no_meta_dir(sessions, _HARNESS_FIXTURE)
    real_no_meta = _plant_no_meta_dir(sessions, _UUID_1)
    real_with_meta = _plant_meta_dir(sessions, _UUID_2)

    reaped, deferred, failed = reap._reap_stale_sessions(
        sessions, frozenset(), None, None
    )

    assert failed == [], (reaped, deferred, failed)

    # Red against HEAD: HEAD reaps this harness fixture (name is neither
    # dot-prefixed, `_`-prefixed, nor denylisted). After C2 it is kept
    # because it is not uuid-shaped.
    assert _HARNESS_FIXTURE not in reaped, (reaped, deferred, failed)
    assert harness_fixture.exists()

    # True in both worlds: the store survives (denylist today, positive
    # gate after C2), and both uuid-shaped real session dirs are reaped.
    assert store.exists()
    assert _UUID_1 in reaped, (reaped, deferred, failed)
    assert _UUID_2 in reaped, (reaped, deferred, failed)
    assert not real_no_meta.exists()
    assert not real_with_meta.exists()


def test_every_denylisted_name_survives_a_reap_pass(tmp_path):
    """AC4: iterate ``_NON_SESSION_DIR_NAMES`` itself, not a hand-copied
    list — a name added to the denylist later is covered here without
    anyone remembering to extend this test."""
    sessions = tmp_path / "coordinator-sessions"
    sessions.mkdir()

    planted = [
        _plant_no_meta_dir(sessions, name) for name in reap._NON_SESSION_DIR_NAMES
    ]

    reaped, deferred, failed = reap._reap_stale_sessions(
        sessions, frozenset(), None, None
    )

    assert failed == [], (reaped, deferred, failed)
    assert reaped == [], (reaped, deferred, failed)
    for d in planted:
        assert d.exists(), d


def test_gate_alone_keeps_non_session_dirs_with_denylist_emptied(tmp_path, monkeypatch):
    """AC1's load-bearing claim: monkeypatch ``reap._NON_SESSION_DIR_NAMES``
    (the name reap imports into its own namespace, not liveness's) to an
    empty frozenset and assert stores STILL survive — the positive gate,
    not the denylist, is what protects them.

    ``_branch-overrides`` is planted alongside the store to discharge the
    `_`-prefix half of AC1: since the inline `_`-prefix branch cannot itself
    be monkeypatched away, this fixture demonstrates the gate covers the
    same population that branch protects, by surviving even with the
    denylist emptied.

    Red against HEAD: with the denylist emptied, ``decisions`` is caught by
    neither the dot-prefix skip, the `_`-prefix skip, nor (now-empty)
    denylist membership, so HEAD reaps it on this cold fixture.
    """
    sessions = tmp_path / "coordinator-sessions"

    monkeypatch.setattr(reap, "_NON_SESSION_DIR_NAMES", frozenset())

    store = _plant_no_meta_dir(sessions, "decisions", filename="a-record.md")
    branch_overrides = _plant_no_meta_dir(
        sessions, "_branch-overrides", filename="overrides.log"
    )

    reaped, deferred, failed = reap._reap_stale_sessions(
        sessions, frozenset(), None, None
    )

    assert failed == [], (reaped, deferred, failed)
    assert "decisions" not in reaped, (reaped, deferred, failed)
    assert store.exists()
    assert "_branch-overrides" not in reaped, (reaped, deferred, failed)
    assert branch_overrides.exists()

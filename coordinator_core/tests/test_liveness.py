"""
coordinator_core.tests.test_liveness — targeted coverage for liveness._lib_path()'s
engine-root-anchored resolution of the coordinator-session.sh successor.

2026-07-22 rewrite: coordinator-session.sh (and the __file__-walk +
resolve-coordinator-clone subprocess ladder that used to hunt it) is retired
repo-wide. _lib_path() now resolves a single candidate,
<claude_klabauter_root>/coordinator/lib/coordinator_session.py, via
coordinator_core.engine_root.coordinator_engine_root() — pinned
deterministically in tests via CLAUDE_KLABAUTER_ROOT env, never the real machine
registry or DoE checkout. Superseded coverage (the old 3-rung ladder's
success/nonzero-rc/timeout/OSError branches) is gone with the ladder itself.

Spec backlink: pln-repoint-coordinator-core-claud-56d805 § C4 (site 1)
"""

from __future__ import annotations

import pytest

import coordinator_core.liveness as _liveness


@pytest.fixture(autouse=True)
def _reset_live_ids_cache_between_tests():
    """Autouse: reset the resolve_live_session_ids() TTL cache before every test
    in this module. Without this, a test that populates ``_live_ids_cache``
    under a monkeypatched ``time.monotonic()`` (e.g.
    ``test_resolve_live_ids_recomputes_after_ttl_expiry``, which leaves a
    cached_at of a fixed fake value like 1002.0) leaks that module-global into
    the next test — ``monkeypatch`` only reverts the attributes it patched
    (the clock, the TTL, the uncached body), never this side-effected global.
    The next test then races the REAL ``time.monotonic()`` against that
    leftover fake timestamp: whether it reads as "still within TTL" (spurious
    cache hit) depends on the real clock's arbitrary reference point, which
    varies run to run — a genuine order-independent-but-timing-flaky failure,
    not a one-off. Resetting here, not by adding a per-test call, ensures no
    future test in this module can reintroduce the same leak."""
    _liveness._reset_live_ids_cache()
    yield
    _liveness._reset_live_ids_cache()


def _reset_cache(monkeypatch):
    """_lib_path() memoizes into module-level _CACHED_LIB; reset it per-test so
    mocked subprocess behavior is actually exercised rather than short-circuited
    by a prior test's cached result."""
    monkeypatch.setattr(_liveness, "_CACHED_LIB", None)


def test_lib_path_resolves_via_engine_root_when_file_present(tmp_path, monkeypatch):
    """CLAUDE_KLABAUTER_ROOT resolvable and coordinator/lib/coordinator_session.py present
    under it -> that path is returned and cached."""
    _reset_cache(monkeypatch)

    claude_klabauter_root = tmp_path / "claude-klabauter"
    script = claude_klabauter_root / "coordinator" / "lib" / "coordinator_session.py"
    script.parent.mkdir(parents=True)
    script.touch()
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", str(claude_klabauter_root))

    result = _liveness._lib_path()

    assert result == str(script), f"Expected engine-root-anchored successor path; got {result}"


def test_lib_path_missing_successor_file_returns_none(tmp_path, monkeypatch):
    """CLAUDE_KLABAUTER_ROOT resolvable but the successor file doesn't exist under it ->
    None (no invented fallback)."""
    _reset_cache(monkeypatch)

    claude_klabauter_root = tmp_path / "claude-klabauter"
    claude_klabauter_root.mkdir()
    monkeypatch.setenv("CLAUDE_KLABAUTER_ROOT", str(claude_klabauter_root))

    result = _liveness._lib_path()

    assert result is None


def test_lib_path_unresolvable_engine_root_degrades_to_none(monkeypatch):
    """coordinator_engine_root() raising RuntimeError (no CLAUDE_KLABAUTER_ROOT env, no
    settings-home pointer, no machine-local registry entry) must degrade to
    None rather than raise — matching _lib_path()'s "or None if not found"
    contract, never a __file__-walk or subprocess fallback."""
    _reset_cache(monkeypatch)
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)

    def _raise():
        raise RuntimeError("coordinator_engine_root: cannot resolve CLAUDE_KLABAUTER_ROOT")

    monkeypatch.setattr(_liveness, "coordinator_engine_root", _raise)

    result = _liveness._lib_path()

    assert result is None


# ---------------------------------------------------------------------------
# resolve_live_session_ids() TTL cache (C12 — Windows bash-spawn cost hardening)
# ---------------------------------------------------------------------------


def test_resolve_live_session_ids_caches_within_ttl(monkeypatch):
    """Two calls within the TTL window must hit the uncached body only once —
    this is the whole point of the C12 hardening (collapse repeated spawns
    within one archival/pickup scan pass into a single bash spawn)."""
    _liveness._reset_live_ids_cache()
    monkeypatch.setattr(_liveness, "_LIVE_IDS_CACHE_TTL_SEC", 60.0)

    calls = []

    def _fake_uncached():
        calls.append(1)
        return frozenset({"sid-1"})

    monkeypatch.setattr(_liveness, "_resolve_live_session_ids_uncached", _fake_uncached)

    first = _liveness.resolve_live_session_ids()
    second = _liveness.resolve_live_session_ids()

    assert first == frozenset({"sid-1"})
    assert second == frozenset({"sid-1"})
    assert len(calls) == 1, "second call within TTL must reuse the cached value, not re-invoke the bash bridge"


def test_resolve_live_session_ids_recomputes_after_ttl_expiry(monkeypatch):
    """Once the TTL window elapses, the next call must re-invoke the uncached
    body — the cache must not be permanent (liveness verdicts do change)."""
    _liveness._reset_live_ids_cache()
    monkeypatch.setattr(_liveness, "_LIVE_IDS_CACHE_TTL_SEC", 1.0)

    calls = []

    def _fake_uncached():
        calls.append(1)
        return frozenset({"sid-%d" % len(calls)})

    monkeypatch.setattr(_liveness, "_resolve_live_session_ids_uncached", _fake_uncached)

    fake_now = [1000.0]
    monkeypatch.setattr(_liveness.time, "monotonic", lambda: fake_now[0])

    first = _liveness.resolve_live_session_ids()
    fake_now[0] += 2.0  # advance past the 1.0s TTL
    second = _liveness.resolve_live_session_ids()

    assert len(calls) == 2, "call after TTL expiry must recompute rather than reuse the stale cached value"
    assert first != second


def test_reset_live_ids_cache_forces_recompute(monkeypatch):
    """_reset_live_ids_cache() (test-only helper) must force the very next call
    to recompute regardless of TTL."""
    monkeypatch.setattr(_liveness, "_LIVE_IDS_CACHE_TTL_SEC", 60.0)

    calls = []

    def _fake_uncached():
        calls.append(1)
        return frozenset({"sid-x"})

    monkeypatch.setattr(_liveness, "_resolve_live_session_ids_uncached", _fake_uncached)

    _liveness.resolve_live_session_ids()
    _liveness._reset_live_ids_cache()
    _liveness.resolve_live_session_ids()

    assert len(calls) == 2, "_reset_live_ids_cache must clear the cache so the next call recomputes"


def test_resolve_live_session_ids_uncached_degrades_to_empty_on_error(monkeypatch):
    """_resolve_live_session_ids_uncached's own try/except body is the
    deliberate degrade-not-raise seam this module's docstring documents — every
    other test in this file replaces the function wholesale via monkeypatch and
    so never exercises its real except-Exception path. This pins the
    intentional-degrade contract directly, mirroring
    test_cs_claim_holder_live_indeterminate_propagates_not_false's pin of the
    opposite (propagate-not-swallow) contract below.
    # Review: code-reviewer — Finding 8, this seam had no direct coverage."""

    def _raise():
        raise RuntimeError("boom")

    monkeypatch.setattr(_liveness._session_liveness, "live_session_ids", _raise)

    assert _liveness._resolve_live_session_ids_uncached() == frozenset()


# ---------------------------------------------------------------------------
# cs_claim_holder_live() — confirmed-alive / confirmed-dead / indeterminate
#
# Spec backlink: cross-repo/inbox/2026-07-14-claude-central-em-claim-lock-fleet-fanout-accept.md
# ("One flag back to your engine tier — the exception-swallow is on YOUR side")
#
# 2026-07-21 fix: cs_claim_holder_live previously caught every exception from
# the native port and returned False — indistinguishable from a confirmed-dead
# verdict, and downstream that False authorizes claim takeover / reaping of a
# session that might still be alive. It must now PROPAGATE an indeterminate/
# errored read rather than collapse it to a dead verdict; callers own the
# fail-closed-to-keep decision (see liveness.py's module + function docstrings
# and each caller's own try/except).
# ---------------------------------------------------------------------------


def test_cs_claim_holder_live_confirmed_alive(monkeypatch):
    """A clean, successful liveness read that resolves to alive returns True."""
    monkeypatch.setattr(
        _liveness._session_liveness, "claim_holder_live", lambda claim_path: True
    )

    assert _liveness.cs_claim_holder_live("/some/claim/dir") is True


def test_cs_claim_holder_live_confirmed_dead(monkeypatch):
    """A clean, successful liveness read that resolves to dead returns False."""
    monkeypatch.setattr(
        _liveness._session_liveness, "claim_holder_live", lambda claim_path: False
    )

    assert _liveness.cs_claim_holder_live("/some/claim/dir") is False


def test_cs_claim_holder_live_indeterminate_propagates_not_false(monkeypatch):
    """An errored/indeterminate read (native port raises) MUST propagate — it
    must NOT be swallowed and reported as False (confirmed-dead). Downstream
    fail-closed-to-keep logic (session.reap, ceremony_lock,
    archive_actioned_memos, archive_handoffs, handoff_reconcile) all depend on
    actually SEEING the exception to defer/keep rather than reap/archive/
    reclaim; a swallowed-to-False verdict silently authorizes exactly that."""
    import pytest

    def _raise(claim_path):
        raise OSError("simulated indeterminate liveness read")

    monkeypatch.setattr(_liveness._session_liveness, "claim_holder_live", _raise)

    with pytest.raises(OSError):
        _liveness.cs_claim_holder_live("/some/claim/dir")


def test_cs_claim_holder_live_empty_claim_path_raises_valueerror():
    """Empty/missing claim_path raises ValueError (native port contract) — this
    is a caller-contract violation, not an indeterminate liveness read, and
    must also propagate rather than degrade to False."""
    import pytest

    with pytest.raises(ValueError):
        _liveness.cs_claim_holder_live("")

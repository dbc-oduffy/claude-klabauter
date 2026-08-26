"""C9 (docs/plans/2026-08-25-reconcile-open-comes-back-under-the-bar.md § C9,
discharging AC1b): the `gate_evidence` I/O-kind leg re-resolution cache.

AC1b measurement (recorded here, not re-derived by this test): wall-clock,
n=5, taken directly against `resolve_leg`/`is_ancestor` on this box —

  - `frontmatter_field` leg (local worktree read): ~0.35-1.5ms mean.
  - `commit_ancestor` leg (spawns `git merge-base --is-ancestor`, per
    `is_ancestor`): ~19-25ms mean -- the dominant per-leg cost, and the one
    `process_time`/cProfile are structurally blind to (wall, not CPU).
  - Corpus at measurement time: 5 `awaiting_gate` handoffs carry
    `gate_evidence`, 10 legs total (7 `human`, 3 `frontmatter-field`, 0
    `commit-ancestor` currently on disk).

This file pins the CAP, not the measurement: `_reresolve_gate_evidence_leg`
must not re-pay live `resolve_leg` I/O for two legs (same or different
handoffs) that resolve to the same `sibling_fact` request within the TTL
window, and must still hit the real code path again once cold.

NEGATIVE-SPEC exercised here, not merely described:
  - `leg_id` differing across two legs sharing the same (kind, repo, ref-
    derived) request still hits ONE cache entry -- `leg_id` is excluded from
    the cache key by design (a sweep re-declaring the same underlying fact
    on many handoffs must not re-pay it once per handoff).
  - A `human`/`deadline` leg never touches `resolve_leg` or the cache at all.
  - A cache entry older than the TTL is NOT served stale -- `resolve_leg` is
    called again, never skipped indefinitely.
  - Cache overflow past the size cap clears the whole cache (crude, on
    purpose) rather than growing unboundedly across a long-lived process.
"""

import time

import pytest

from coordinator_core.ops import handoff_transition as ht


@pytest.fixture(autouse=True)
def _clean_cache():
    """Every test starts and ends with an empty module-level cache -- this
    dict is process-global state, and a leaked entry from one test would
    silently change whether the next test's request is a cache hit."""
    ht._gate_evidence_leg_cache.clear()
    yield
    ht._gate_evidence_leg_cache.clear()


def _leg(leg_id, kind="frontmatter-field", repo="claude_klabauter", ref="a/b.md#field"):
    return {"leg_id": leg_id, "kind": kind, "repo": repo, "ref": ref}


def _observation(read_ok=True, observed="x", error=None):
    return {"read_ok": read_ok, "observed": observed, "error": error}


class TestSweepMemoization:
    """The AC1b-motivating case: a sweep re-declaring the same fact on many
    handoffs pays live I/O once, not N times."""

    def test_same_request_two_different_leg_ids_hits_resolve_leg_once(self, monkeypatch):
        calls = []

        def _fake_resolve_leg(request):
            calls.append(request)
            return _observation()

        monkeypatch.setattr(ht, "resolve_leg", _fake_resolve_leg)

        leg_a = _leg("leg-on-handoff-one")
        leg_b = _leg("leg-on-handoff-two")  # different leg_id, same fact

        ht._reresolve_gate_evidence_leg(leg_a, "2026-08-26")
        ht._reresolve_gate_evidence_leg(leg_b, "2026-08-26")

        assert len(calls) == 1, (
            "two legs declaring the identical (kind, repo, ref) fact under "
            "different leg_ids must share one resolve_leg call"
        )

    def test_distinct_facts_each_pay_their_own_resolve_leg_call(self, monkeypatch):
        calls = []

        def _fake_resolve_leg(request):
            calls.append(request)
            return _observation()

        monkeypatch.setattr(ht, "resolve_leg", _fake_resolve_leg)

        ht._reresolve_gate_evidence_leg(_leg("leg-1", ref="a/b.md#field"), "2026-08-26")
        ht._reresolve_gate_evidence_leg(_leg("leg-2", ref="a/c.md#field"), "2026-08-26")

        assert len(calls) == 2, "genuinely distinct facts must not collapse into one cache entry"

    def test_read_gate_evidence_resolved_across_many_handoffs_shares_one_call(
        self, monkeypatch, tmp_path
    ):
        """The real caller shape: N `awaiting_gate` handoffs on disk all
        gating on the SAME sibling fact must pay resolve_leg once for the
        whole sweep, not once per handoff."""
        calls = []

        def _fake_resolve_leg(request):
            calls.append(request)
            return _observation()

        monkeypatch.setattr(ht, "resolve_leg", _fake_resolve_leg)

        import datetime

        def _handoff(name, leg_id):
            p = tmp_path / name
            p.write_text(
                "---\n"
                "deployment_state: awaiting_gate\n"
                "gate_evidence:\n"
                "  legs:\n"
                f"    - leg_id: {leg_id}\n"
                "      kind: frontmatter-field\n"
                "      repo: claude_klabauter\n"
                "      ref: shared/path.md#field\n"
                "---\n\nbody\n",
                encoding="utf-8",
            )
            return p

        p1 = _handoff("2026-08-26-a.md", "leg-a")
        p2 = _handoff("2026-08-26-b.md", "leg-b")
        p3 = _handoff("2026-08-26-c.md", "leg-c")

        today = datetime.date(2026, 8, 26)
        for p in (p1, p2, p3):
            resolved = ht._read_gate_evidence_resolved(p, today)
            assert resolved is not None

        assert len(calls) == 1, (
            "three handoffs declaring the identical fact must share one "
            "resolve_leg call across the sweep"
        )


class TestNonIoKindsNeverTouchTheCacheOrResolveLeg:
    def test_human_leg_never_calls_resolve_leg(self, monkeypatch):
        calls = []
        monkeypatch.setattr(ht, "resolve_leg", lambda request: calls.append(request))

        ht._reresolve_gate_evidence_leg({"leg_id": "h1", "kind": "human"}, "2026-08-26")

        assert calls == []
        assert ht._gate_evidence_leg_cache == {}

    def test_deadline_leg_never_calls_resolve_leg(self, monkeypatch):
        calls = []
        monkeypatch.setattr(ht, "resolve_leg", lambda request: calls.append(request))

        ht._reresolve_gate_evidence_leg(
            {"leg_id": "d1", "kind": "deadline", "ref": "2026-01-01"}, "2026-08-26"
        )

        assert calls == []
        assert ht._gate_evidence_leg_cache == {}


class TestTtlExpiry:
    def test_stale_entry_is_not_served_and_resolve_leg_is_called_again(self, monkeypatch):
        calls = []

        def _fake_resolve_leg(request):
            calls.append(request)
            return _observation()

        monkeypatch.setattr(ht, "resolve_leg", _fake_resolve_leg)
        monkeypatch.setattr(ht, "_GATE_EVIDENCE_LEG_CACHE_TTL_S", 0.0)

        leg = _leg("leg-1")
        ht._reresolve_gate_evidence_leg(leg, "2026-08-26")

        # TTL is 0: the entry is stamped in the past relative to "now" the
        # instant it is checked, so the very next lookup must be a live miss.
        time.sleep(0.01)
        ht._reresolve_gate_evidence_leg(_leg("leg-2", ref=leg["ref"]), "2026-08-26")

        assert len(calls) == 2, "an expired entry must not be served as a cache hit"

    def test_fresh_entry_within_ttl_is_served_from_cache(self, monkeypatch):
        calls = []

        def _fake_resolve_leg(request):
            calls.append(request)
            return _observation()

        monkeypatch.setattr(ht, "resolve_leg", _fake_resolve_leg)
        monkeypatch.setattr(ht, "_GATE_EVIDENCE_LEG_CACHE_TTL_S", 60.0)

        leg = _leg("leg-1")
        ht._reresolve_gate_evidence_leg(leg, "2026-08-26")
        ht._reresolve_gate_evidence_leg(_leg("leg-2", ref=leg["ref"]), "2026-08-26")

        assert len(calls) == 1


class TestSizeCapOverflow:
    def test_overflow_clears_the_whole_cache_rather_than_growing_unboundedly(self, monkeypatch):
        monkeypatch.setattr(ht, "_GATE_EVIDENCE_LEG_CACHE_MAX_ENTRIES", 2)

        ht._gate_evidence_leg_cache_put(("a",), _observation())
        ht._gate_evidence_leg_cache_put(("b",), _observation())
        assert len(ht._gate_evidence_leg_cache) == 2

        # This third put crosses the cap: the crude, documented behaviour is
        # a whole-cache clear, then insert the new entry -- never an
        # unbounded dict.
        ht._gate_evidence_leg_cache_put(("c",), _observation())

        assert len(ht._gate_evidence_leg_cache) == 1
        assert ("c",) in ht._gate_evidence_leg_cache
        assert ("a",) not in ht._gate_evidence_leg_cache


class TestCacheKeyExcludesLegId:
    def test_key_is_identical_for_two_requests_differing_only_by_leg_id(self):
        key_a = ht._gate_evidence_leg_cache_key(
            {"leg_id": "leg-a", "kind": "frontmatter_field", "repo": "r", "path": "p", "field": "f"}
        )
        key_b = ht._gate_evidence_leg_cache_key(
            {"leg_id": "leg-b", "kind": "frontmatter_field", "repo": "r", "path": "p", "field": "f"}
        )

        assert key_a == key_b

    def test_key_differs_when_a_non_leg_id_field_differs(self):
        key_a = ht._gate_evidence_leg_cache_key(
            {"leg_id": "leg-a", "kind": "frontmatter_field", "repo": "r", "path": "p", "field": "f"}
        )
        key_b = ht._gate_evidence_leg_cache_key(
            {"leg_id": "leg-a", "kind": "frontmatter_field", "repo": "r", "path": "other", "field": "f"}
        )

        assert key_a != key_b

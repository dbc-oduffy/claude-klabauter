"""KPI/regression test for ``PriorityResolveCache`` (C6b perf hoist).

Regression target: ``resolve_priority()`` used to rebuild a ``dag.walk_forward()``
DFS AND a ``_build_parent_map()`` predecessor-spine map on EVERY call, even
though both are invariant for a whole ``handoffs.collect()`` run over the same
repo corpus — the profile that motivated this fix showed ``_build_parent_map``
alone accounting for 22.6s of a 38.4s aggregate at 360 handoffs (once per
handoff, over the identical corpus each time). See
``coordinator_core/ops/emit/priority_resolve.py``'s ``PriorityResolveCache``
docstring for the full correctness argument (why sharing the cache across
calls is byte-identical, not merely faster).

This test pins the CALL-COUNT shape (a cache-scoped build happens once, not
once per resolve_priority() call), not wall-clock time — wall-clock is
measured manually in the dispatch report (git-log/subprocess variance makes a
timing assertion flaky in CI), but a call-count regression is exactly what
would silently reintroduce the N-rebuilds-over-one-corpus shape this fix
removes, so THAT'S what a future change is guarded against here.

Spec backlink: coordinator-claude docs/plans/2026-07-26-priority-ledger.md § C5, § C10
(cache is a C6b addendum to the same priority-resolution work).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import dag
from coordinator_core.ops.emit import priority_resolve as pr_mod
from coordinator_core.ops.emit.priority_resolve import (
    PriorityResolveCache,
    resolve_priority,
)
from coordinator_core.ops.emit.tests.conftest import _ledger, _write_node  # noqa: F401


@pytest.fixture()
def chain_repo(tmp_path: Path) -> Path:
    """A..E predecessor chain (A <- B <- C <- D <- E), all under one
    handoff_dir — only A carries an explicit ledger entry, so B..E all
    resolve "inherited" back to A.
    """
    d = tmp_path / "state" / "handoffs"
    d.mkdir(parents=True)
    _write_node(d, "A.md", handoff_id="A_id", predecessor=None)
    _write_node(d, "B.md", handoff_id="B_id", predecessor="A.md")
    _write_node(d, "C.md", handoff_id="C_id", predecessor="B.md")
    _write_node(d, "D.md", handoff_id="D_id", predecessor="C.md")
    _write_node(d, "E.md", handoff_id="E_id", predecessor="D.md")
    return tmp_path


@pytest.fixture()
def node_dir_generic(tmp_path: Path) -> Path:
    """Bare ``state/handoffs`` dir with no fixture nodes pre-written — for
    tests that write their own small, purpose-specific node set (mirrors
    ``test_priority_resolve.py``'s ``node_dir`` fixture).
    """
    d = tmp_path / "state" / "handoffs"
    d.mkdir(parents=True)
    return d


def test_cache_builds_parent_map_once_across_many_resolve_priority_calls(
    chain_repo: Path, monkeypatch
):
    """The KPI this guards: N resolve_priority() calls sharing ONE
    PriorityResolveCache build the corpus-wide parent map ONCE (per distinct
    handoff_dir), not once per call — the exact shape of the regression this
    fix removes.
    """
    repo_root = str(chain_repo)
    handoff_dir = str(chain_repo / "state" / "handoffs")
    ledger = _ledger(A_id="high")

    build_calls = []
    orig_build = pr_mod._build_parent_map

    def _counting_build(*args, **kwargs):
        build_calls.append(1)
        return orig_build(*args, **kwargs)

    monkeypatch.setattr(pr_mod, "_build_parent_map", _counting_build)

    cache = PriorityResolveCache(repo_root)

    results = []
    for letter in ("B", "C", "D", "E"):
        path = str(chain_repo / "state" / "handoffs" / f"{letter}.md")
        results.append(
            resolve_priority(
                path,
                f"{letter}_id",
                ledger_entries=ledger,
                repo_root=repo_root,
                handoff_dir=handoff_dir,
                cache=cache,
            )
        )

    # KPI assertion: ONE build for four calls sharing one handoff_dir — not
    # four (what the pre-fix per-call rebuild would have produced).
    assert len(build_calls) == 1

    for result in results:
        assert result == {"effective_priority": "high", "origin": "inherited", "source_id": "A_id"}


def test_cache_and_no_cache_paths_agree(chain_repo: Path):
    """Cross-check: the cache-backed fast path and the legacy per-call
    walk_forward()+_build_parent_map() path must resolve identically for the
    same node — the correctness half of the KPI test above.
    """
    repo_root = str(chain_repo)
    handoff_dir = str(chain_repo / "state" / "handoffs")
    ledger = _ledger(A_id="high")
    c_path = str(chain_repo / "state" / "handoffs" / "C.md")

    no_cache_result = resolve_priority(
        c_path, "C_id", ledger_entries=ledger, repo_root=repo_root, handoff_dir=handoff_dir
    )

    cache = PriorityResolveCache(repo_root)
    cached_result = resolve_priority(
        c_path,
        "C_id",
        ledger_entries=ledger,
        repo_root=repo_root,
        handoff_dir=handoff_dir,
        cache=cache,
    )

    assert no_cache_result == cached_result == {
        "effective_priority": "high",
        "origin": "inherited",
        "source_id": "A_id",
    }


def test_cache_repo_root_mismatch_raises(chain_repo: Path, tmp_path_factory):
    """A cache built against one repo corpus is not valid for a different
    repo_root — a mismatch must fail loud, not silently resolve against the
    wrong corpus (dispatch brief's per-run, not process-lifetime, scope
    requirement)."""
    repo_root = str(chain_repo)
    other_root = str(tmp_path_factory.mktemp("other-repo"))
    cache = PriorityResolveCache(repo_root)

    c_path = str(chain_repo / "state" / "handoffs" / "C.md")

    with pytest.raises(ValueError, match="cache.repo_root"):
        resolve_priority(
            c_path,
            "C_id",
            ledger_entries=_ledger(A_id="high"),
            repo_root=other_root,
            handoff_dir=str(chain_repo / "state" / "handoffs"),
            cache=cache,
        )


def test_cache_scans_corpus_once_not_per_call(chain_repo: Path, monkeypatch):
    """PriorityResolveCache.__init__ scans + parses the corpus ONCE; repeated
    resolve_priority() calls sharing the cache must not trigger additional
    corpus scans (the other half of the dead-work this fix removes — see
    PriorityResolveCache's docstring on why the id-index/corpus scan a
    per-call walk_forward() used to pay for was already unreachable through
    parent_map traversal, hence simply removed rather than cached)."""
    repo_root = str(chain_repo)
    handoff_dir = str(chain_repo / "state" / "handoffs")
    ledger = _ledger(A_id="high")

    scan_calls = []
    orig_scan = pr_mod.scan_repo_handoff_corpus

    def _counting_scan(*args, **kwargs):
        scan_calls.append(1)
        return orig_scan(*args, **kwargs)

    monkeypatch.setattr(pr_mod, "scan_repo_handoff_corpus", _counting_scan)

    cache = PriorityResolveCache(repo_root)
    assert len(scan_calls) == 1

    for letter in ("B", "C", "D", "E"):
        path = str(chain_repo / "state" / "handoffs" / f"{letter}.md")
        resolve_priority(
            path,
            f"{letter}_id",
            ledger_entries=ledger,
            repo_root=repo_root,
            handoff_dir=handoff_dir,
            cache=cache,
        )

    assert len(scan_calls) == 1


def test_cache_builds_git_history_cache_once_and_threads_it_through(
    chain_repo: Path, monkeypatch
):
    """PriorityResolveCache.__init__ primes ONE dag.build_git_history_cache()
    pass (a single ``git log --all --name-only`` sweep) rather than letting
    every unresolved edge in ``_build_parent_map``'s ``resolve_target`` calls
    spawn its own ``git log --all -- <path>`` subprocess.

    Regression target: profiled against a real corpus, ``resolve_target``'s
    tier-3 fallback (``dag._git_path_ever_tracked``) accounted for 137
    subprocess spawns / ~10.2s of a single emit() run. ``build_git_history_cache``
    already existed as an opt-in perf primitive (dag.py's own module docstring
    names it for exactly this "many resolve_target calls in one sweep" shape)
    but was never wired into PriorityResolveCache — this pins that it now is.

    Asserts the CALL SHAPE (build_git_history_cache called once; the resulting
    cache is passed by identity into every resolve_target() call inside
    _build_parent_map), not wall-clock — see the module's own convention above.
    """
    repo_root = str(chain_repo)
    ledger = _ledger(A_id="high")

    build_calls = []
    orig_build = pr_mod.build_git_history_cache

    def _counting_build(*args, **kwargs):
        build_calls.append(args)
        return orig_build(*args, **kwargs)

    monkeypatch.setattr(pr_mod, "build_git_history_cache", _counting_build)

    resolve_target_calls = []
    orig_resolve_target = pr_mod.resolve_target

    def _counting_resolve_target(*args, **kwargs):
        resolve_target_calls.append(kwargs.get("git_history_cache", "MISSING"))
        return orig_resolve_target(*args, **kwargs)

    monkeypatch.setattr(pr_mod, "resolve_target", _counting_resolve_target)

    cache = PriorityResolveCache(repo_root)
    assert len(build_calls) == 1, "build_git_history_cache must be primed exactly once per cache instance"

    for letter in ("B", "C", "D", "E"):
        path = str(chain_repo / "state" / "handoffs" / f"{letter}.md")
        resolve_priority(
            path,
            f"{letter}_id",
            ledger_entries=ledger,
            repo_root=repo_root,
            handoff_dir=str(chain_repo / "state" / "handoffs"),
            cache=cache,
        )

    # build_git_history_cache is still only ever primed once, even after
    # several resolve_priority() calls sharing the cache.
    assert len(build_calls) == 1
    # Every resolve_target() call made while building the parent map received
    # the SAME cache object built above (never "MISSING"/never a fresh None) —
    # this is the actual perf win: no call site quietly bypasses it.
    assert resolve_target_calls, "expected at least one resolve_target call while building the parent map"
    assert all(c is cache._git_history_cache for c in resolve_target_calls)


def test_id_shaped_predecessor_ref_cached_and_uncached_agree(node_dir_generic: Path):
    """Regression pin for the NEGATIVE-SPEC in priority_resolve.py's module
    comment block above ``PriorityResolveCache`` — the argument that
    justifies skipping ``dag.walk_forward()`` entirely on the cached path.

    ``_build_parent_map``'s own ``resolve_target()`` call has NEVER been
    passed ``id_index`` (verified by reading its call site, not assumed) —
    so a predecessor expressed ONLY via the id-shaped ``predecessor_id``
    alias (never a filename/path — the C6 pointer-normalization shape) is
    UNREACHABLE through ``parent_map`` in BOTH the walk_forward-based
    (uncached) path and the cache-based path, even though ``walk_forward``'s
    OWN internal DFS — id_index-aware — CAN discover A as a node via that
    same ref. The two paths must therefore agree, and agree on "unreachable"
    (C does NOT inherit A's explicit "urgent"), not on "inherited".

    If a future change makes ``_build_parent_map``'s ``resolve_target()``
    call id_index-aware (closing the C6 gap this test documents), the
    asserted origin below flips from "none" to "inherited" and this test
    fails — forcing a deliberate revisit of the NEGATIVE-SPEC comment block
    (and of whether skipping walk_forward is still safe) rather than a
    silent divergence between the cached and uncached paths.
    """
    d = node_dir_generic
    repo_root = str(d.parent.parent)
    _write_node(d, "A.md", handoff_id="A_id", predecessor=None)
    # predecessor: none (the on-disk sentinel) + predecessor_id: A_id (the
    # id-shaped alias) — A is reachable ONLY via the alias, never a filename.
    c_path = _write_node(
        d, "C.md", handoff_id="C_id", predecessor=None, predecessor_id="A_id"
    )

    ledger = _ledger(A_id="urgent")

    no_cache_result = resolve_priority(str(c_path), "C_id", ledger_entries=ledger)

    cache = PriorityResolveCache(repo_root)
    cached_result = resolve_priority(
        str(c_path), "C_id", ledger_entries=ledger, cache=cache
    )

    assert no_cache_result == cached_result
    # Documents present (pre-existing, unchanged-by-this-fix) behaviour: the
    # id-shaped ref does not resolve through parent_map, so C gets no
    # inherited value from A at all.
    assert no_cache_result == {
        "effective_priority": None,
        "origin": "none",
        "source_id": None,
    }


def test_build_parent_map_git_history_only_ref_matches_include_history_tier_true(
    node_dir_generic: Path, monkeypatch
):
    """Regression pin for the claim's own un-exercised edge (P3 nit, code
    review of commit 2993c608f398aac91221dd82e0b4adc9e2371b4c): a ref that
    WOULD resolve via tier 3 (git-history-only presence — genuinely
    deleted/relocated, ``ever_tracked() == True``) must produce the SAME
    ``parent_map`` under ``_build_parent_map``'s ``include_history_tier=False``
    call as it would under ``include_history_tier=True`` — i.e. the
    ``'git-history'`` sentinel is discarded identically to ``None`` by the
    ``if target and target != "git-history"`` check either way, so skipping
    tier 3 entirely never changes the resulting parent set.

    ``test_build_parent_map_skips_git_history_tier`` (above) only covers the
    guaranteed-miss shape (``ever_tracked()`` would be False regardless); this
    test constructs the complementary shape where tier 3 WOULD have hit.
    """
    d = node_dir_generic
    repo_root = str(d.parent.parent)
    orphaned_ref = "genuinely-relocated-ref.md"
    c_path = _write_node(d, "C.md", handoff_id="C_id", predecessor=orphaned_ref)

    # Simulate git-history-only presence: no on-disk candidate exists for
    # orphaned_ref (tiers 1/2 miss), but git history says it was once
    # tracked (tier 3 would hit).
    monkeypatch.setattr(
        "coordinator_core.dag._git_path_ever_tracked",
        lambda *a, **k: True,
    )

    # Confirm the premise: with include_history_tier=True, this ref DOES
    # resolve to the 'git-history' sentinel (tier 3 would have fired).
    with_history = dag.resolve_target(
        orphaned_ref, str(d), repo_root, include_history_tier=True
    )
    assert with_history == "git-history"

    # And the production call shape (include_history_tier=False, as
    # _build_parent_map always passes) resolves to None instead — tier 3
    # never runs.
    without_history = dag.resolve_target(
        orphaned_ref, str(d), repo_root, include_history_tier=False
    )
    assert without_history is None

    # Both are discarded identically by _build_parent_map's own consumption
    # check, so the resulting parent_map entry for C is empty either way.
    assert not (with_history and with_history != "git-history")
    assert not (without_history and without_history != "git-history")

    # End-to-end: the real _build_parent_map (always include_history_tier=False)
    # produces an empty parent list for C — same as the guaranteed-miss case —
    # confirming resolve_priority's actual output is unaffected by this edge.
    ledger = _ledger()
    result = resolve_priority(str(c_path), "C_id", ledger_entries=ledger)
    assert result == {"effective_priority": None, "origin": "none", "source_id": None}


def test_build_parent_map_skips_git_history_tier(node_dir_generic: Path, monkeypatch):
    """Regression pin: ``_build_parent_map``'s ``resolve_target()`` call
    discards the ``'git-history'`` sentinel identically to ``None`` (see
    ``if target and target != "git-history"`` in its loop), so tier 3 (the
    ``git log --all -- <path>`` subprocess fallback) has never produced a
    distinguishable outcome for this call site — see the dispatch brief for
    the full accounting (~210 of ~239 tier-3 spawns on this corpus were
    well-formed ``predecessor_id`` handoff-ids reaching a path oracle for a
    guaranteed miss, because this call site omits ``id_index``).

    Pins the fix at the call site: every ``resolve_target()`` call made
    while building the parent map must pass ``include_history_tier=False``,
    and ``dag._git_path_ever_tracked`` (the actual subprocess spawn) must
    never fire even for a predecessor ref with zero on-disk match — asserts
    the CALL SHAPE / subprocess-reachability, not wall-clock, per this
    module's own convention (machine-load-norm makes timing assertions
    worthless here).
    """
    d = node_dir_generic
    # A predecessor ref that resolves in neither of tiers 1/2 (no matching
    # file anywhere under handoff_dir/state/handoffs/archive/handoffs) — the
    # exact shape that, pre-fix, fell through to a tier-3 git-history spawn.
    c_path = _write_node(
        d, "C.md", handoff_id="C_id", predecessor="totally-orphaned-ref.md"
    )

    ledger = _ledger()

    ever_tracked_calls = []
    monkeypatch.setattr(
        "coordinator_core.dag._git_path_ever_tracked",
        lambda *a, **k: (ever_tracked_calls.append((a, k)), False)[1],
    )

    resolve_target_calls = []
    orig_resolve_target = pr_mod.resolve_target

    def _recording_resolve_target(*args, **kwargs):
        resolve_target_calls.append(kwargs)
        return orig_resolve_target(*args, **kwargs)

    monkeypatch.setattr(pr_mod, "resolve_target", _recording_resolve_target)

    resolve_priority(str(c_path), "C_id", ledger_entries=ledger)

    assert resolve_target_calls, "expected at least one resolve_target call while building the parent map"
    assert all(
        kwargs.get("include_history_tier") is False for kwargs in resolve_target_calls
    ), "_build_parent_map must opt every resolve_target() call out of the git-history tier"

    # Mechanism-level pin, isolated from dag.walk_forward's own (separate,
    # out-of-scope) internal resolve_target() call for the same ref: with
    # include_history_tier=False, dag.resolve_target itself must never reach
    # tier 3 for a path absent from every on-disk candidate — i.e. the flag
    # asserted above actually short-circuits the ever_tracked()/subprocess
    # path, not just a passthrough kwarg nobody reads.
    ever_tracked_calls.clear()
    result = dag.resolve_target(
        "totally-orphaned-ref.md",
        str(d),
        str(d.parent.parent),
        include_history_tier=False,
    )
    assert result is None
    assert not ever_tracked_calls, (
        "tier-3 git-history subprocess path must be unreached when "
        "include_history_tier=False, even for a guaranteed-miss ref"
    )

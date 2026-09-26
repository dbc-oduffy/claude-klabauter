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

Spec backlink: DoE-claude DoE-claude:pln-priority-ledger-durable-pm-pri-817d40 § C5, § C10
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
    d = tmp_path / "state" / "handoffs"
    d.mkdir(parents=True)
    return d


def test_cache_builds_parent_map_once_across_many_resolve_priority_calls(
    chain_repo: Path, monkeypatch
):
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

    assert len(build_calls) == 1

    for result in results:
        assert result == {"effective_priority": "high", "origin": "inherited", "source_id": "A_id"}


def test_cache_and_no_cache_paths_agree(chain_repo: Path):
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

    assert len(build_calls) == 1
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
    assert no_cache_result == {
        "effective_priority": None,
        "origin": "none",
        "source_id": None,
    }


def test_build_parent_map_git_history_only_ref_matches_include_history_tier_true(
    node_dir_generic: Path, monkeypatch
):
    d = node_dir_generic
    repo_root = str(d.parent.parent)
    orphaned_ref = "genuinely-relocated-ref.md"
    c_path = _write_node(d, "C.md", handoff_id="C_id", predecessor=orphaned_ref)

    monkeypatch.setattr(
        "coordinator_core.dag._git_path_ever_tracked",
        lambda *a, **k: True,
    )

    with_history = dag.resolve_target(
        orphaned_ref, str(d), repo_root, include_history_tier=True
    )
    assert with_history == "git-history"

    without_history = dag.resolve_target(
        orphaned_ref, str(d), repo_root, include_history_tier=False
    )
    assert without_history is None

    assert not (with_history and with_history != "git-history")
    assert not (without_history and without_history != "git-history")

    ledger = _ledger()
    result = resolve_priority(str(c_path), "C_id", ledger_entries=ledger)
    assert result == {"effective_priority": None, "origin": "none", "source_id": None}


def test_build_parent_map_skips_git_history_tier(node_dir_generic: Path, monkeypatch):
    d = node_dir_generic
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

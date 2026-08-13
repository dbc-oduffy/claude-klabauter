"""
coordinator_core.ops.tests.test_spec_backlink_resolve — tests for the C1 resolver.

Spec backlink: docs/plans/2026-08-13-spec-backlinks-cite-a-stable-deliverable-id.md § C1

Uses the shared `spec_backlink_corpus` fixture (conftest.py, `build_spec_backlink_corpus`)
rather than hand-rolling a second corpus — see the plan's own enrich-once stub.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.ops.spec_backlink_resolve import (
    _resolve_handler,
    build_index,
    resolve,
    resolve_id,
)


def _worktree_root(corpus: dict) -> Path:
    return corpus["root"]


# ---------------------------------------------------------------------------
# Local hits — live plan, archived spec, pln-, dlv-
# ---------------------------------------------------------------------------


def test_live_plan_pln_hit(spec_backlink_corpus):
    root = _worktree_root(spec_backlink_corpus)
    index = build_index(root)
    outcome = resolve_id(index, "pln-fixture-plan-full-aaaaaa")
    assert outcome["outcome"] == "hit"
    assert outcome["path"] == str(spec_backlink_corpus["plan_full"])


def test_live_plan_dlv_hit(spec_backlink_corpus):
    root = _worktree_root(spec_backlink_corpus)
    index = build_index(root)
    outcome = resolve_id(index, "dlv-fixture-plan-full-bbbbbb")
    assert outcome["outcome"] == "hit"
    assert outcome["path"] == str(spec_backlink_corpus["plan_full"])


def test_archived_hit(spec_backlink_corpus):
    root = _worktree_root(spec_backlink_corpus)
    index = build_index(root)
    outcome = resolve_id(index, "pln-fixture-archived-full-222222")
    assert outcome["outcome"] == "hit"
    assert outcome["path"] == str(spec_backlink_corpus["archived_full"])

    outcome2 = resolve_id(index, "dlv-fixture-archived-full-333333")
    assert outcome2["outcome"] == "hit"
    assert outcome2["path"] == str(spec_backlink_corpus["archived_full"])


def test_dlv_only_and_pln_only_hits(spec_backlink_corpus):
    root = _worktree_root(spec_backlink_corpus)
    index = build_index(root)

    dlv_only = resolve_id(index, "dlv-fixture-plan-dlv-only-cccccc")
    assert dlv_only["outcome"] == "hit"
    assert dlv_only["path"] == str(spec_backlink_corpus["plan_dlv_only"])

    pln_only = resolve_id(index, "pln-fixture-plan-pln-only-dddddd")
    assert pln_only["outcome"] == "hit"
    assert pln_only["path"] == str(spec_backlink_corpus["plan_pln_only"])


# ---------------------------------------------------------------------------
# Miss cases
# ---------------------------------------------------------------------------


def test_unknown_id_is_typed_miss(spec_backlink_corpus):
    root = _worktree_root(spec_backlink_corpus)
    index = build_index(root)
    outcome = resolve_id(index, "dlv-does-not-exist-000000")
    assert outcome["outcome"] == "miss"
    assert outcome["path"] is None


def test_null_and_no_id_records_are_not_indexed(spec_backlink_corpus):
    """Records with literal `null` or an absent key never produce a hit for
    any id derived from their own filename/title — nothing to look up, so
    this asserts they don't pollute the index by probing a plausible-but-never-
    mintable id and getting a miss (the corpus never gives them a real id at all)."""
    root = _worktree_root(spec_backlink_corpus)
    index = build_index(root)
    # plan_null_ids / plan_no_ids / archived_no_ids carry no real id by construction;
    # confirm the index has no entry mapping to those paths at all.
    all_paths = set(index.plan_id_to_path.values())
    for paths in index.deliverable_id_to_paths.values():
        all_paths.update(paths)
    assert str(spec_backlink_corpus["plan_null_ids"]) not in all_paths
    assert str(spec_backlink_corpus["plan_no_ids"]) not in all_paths
    assert str(spec_backlink_corpus["archived_no_ids"]) not in all_paths


# ---------------------------------------------------------------------------
# Ambiguity — duplicate-id conflict
# ---------------------------------------------------------------------------


def test_duplicate_dlv_id_resolves_via_pln_instead(spec_backlink_corpus):
    """Per the PM ruling / stub: a `dlv-` id shared by two plans is itself an
    AMBIGUITY when queried directly, but each plan also carries a distinct
    `pln-` id that resolves unambiguously — the caller "resolves via pln-
    instead" by querying that id, not by the resolver silently picking one."""
    root = _worktree_root(spec_backlink_corpus)
    index = build_index(root)

    ambiguous = resolve_id(index, "dlv-fixture-shared-eeeeee")
    assert ambiguous["outcome"] == "ambiguity"
    assert ambiguous["path"] is None
    assert set(ambiguous["candidates"]) == {
        str(spec_backlink_corpus["plan_ambiguous_a"]),
        str(spec_backlink_corpus["plan_ambiguous_b"]),
    }

    hit_a = resolve_id(index, "pln-fixture-plan-ambiguous-a-ffffff")
    assert hit_a["outcome"] == "hit"
    assert hit_a["path"] == str(spec_backlink_corpus["plan_ambiguous_a"])

    hit_b = resolve_id(index, "pln-fixture-plan-ambiguous-b-111111")
    assert hit_b["outcome"] == "hit"
    assert hit_b["path"] == str(spec_backlink_corpus["plan_ambiguous_b"])


def test_ambiguity_outcome_is_distinct_from_hit_and_miss(spec_backlink_corpus):
    """AC1/AC4: AMBIGUITY is a third, distinct outcome — not a hit, not a miss."""
    root = _worktree_root(spec_backlink_corpus)
    index = build_index(root)
    outcome = resolve_id(index, "dlv-fixture-shared-eeeeee")
    assert outcome["outcome"] not in {"hit", "miss"}
    assert outcome["outcome"] == "ambiguity"
    assert len(outcome["candidates"]) > 1


# ---------------------------------------------------------------------------
# Unreadable dir — fail closed
# ---------------------------------------------------------------------------


def test_unreadable_docs_plans_dir_fails_closed(spec_backlink_corpus, monkeypatch):
    """An unreadable docs/plans dir must not silently look like an empty/miss
    corpus that happens to also contain valid hits from elsewhere — it should
    simply not index anything under that root, and lookups against ids that
    only live there return a typed miss rather than raising."""
    root = _worktree_root(spec_backlink_corpus)
    docs_plans = root / "docs" / "plans"

    real_iterdir = Path.iterdir

    def _blocked_iterdir(self):
        if self == docs_plans:
            raise PermissionError("blocked for test")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", _blocked_iterdir, raising=True)

    index = build_index(root)
    # docs/plans/ is unreadable -> nothing from it is indexed; archive/specs/
    # still resolves fine (independent scan root).
    outcome = resolve_id(index, "pln-fixture-plan-full-aaaaaa")
    assert outcome["outcome"] == "miss"

    archived_outcome = resolve_id(index, "pln-fixture-archived-full-222222")
    assert archived_outcome["outcome"] == "hit"


# ---------------------------------------------------------------------------
# N>1 dlv- with no covering pln- — asserts typed AMBIGUITY, not hit/miss
# ---------------------------------------------------------------------------


def test_ambiguous_dlv_with_no_covering_pln_is_ambiguity_not_hit_or_miss(tmp_path):
    """A dlv- id shared by two records, NEITHER of which carries a plan_id —
    i.e. there is no pln- escape hatch at all. The resolver must still return
    a typed AMBIGUITY (never guess one of the two, never report a miss)."""
    docs_plans = tmp_path / "docs" / "plans"
    docs_plans.mkdir(parents=True)

    shared_dlv = "dlv-uncovered-shared-abcdef"

    def _write(name: str) -> Path:
        p = docs_plans / name
        p.write_text(
            "\n".join(
                [
                    "---",
                    f'title: "{name}"',
                    "created: 2026-08-13",
                    f'deliverable_id: "{shared_dlv}"',
                    "scope_mode: feature",
                    "---",
                    "",
                    f"# {name}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return p

    path_a = _write("2026-08-13-uncovered-a.md")
    path_b = _write("2026-08-13-uncovered-b.md")

    index = build_index(tmp_path)
    outcome = resolve_id(index, shared_dlv)
    assert outcome["outcome"] == "ambiguity"
    assert outcome["outcome"] != "hit"
    assert outcome["outcome"] != "miss"
    assert set(outcome["candidates"]) == {str(path_a), str(path_b)}


# ---------------------------------------------------------------------------
# Peer-qualified hit — lazy peer index, resolved via doe_root()
# ---------------------------------------------------------------------------


def test_peer_qualified_hit_uses_doe_root(tmp_path, monkeypatch):
    """A `<repo>:pln-...` query must trigger a lazy peer-repo index build
    rooted at whatever coordinator_registry.doe_root() resolves to — never
    the local worktree_root — and never build the peer index for a local-only
    query."""
    peer_root = tmp_path / "peer-repo"
    peer_docs_plans = peer_root / "docs" / "plans"
    peer_docs_plans.mkdir(parents=True)
    peer_plan = peer_docs_plans / "2026-08-13-peer-fixture-plan.md"
    peer_plan.write_text(
        "\n".join(
            [
                "---",
                'title: "peer fixture plan"',
                "created: 2026-08-13",
                'plan_id: "pln-peer-fixture-plan-999999"',
                "scope_mode: feature",
                "---",
                "",
                "# peer fixture plan",
                "",
            ]
        ),
        encoding="utf-8",
    )

    local_root = tmp_path / "local-repo"
    (local_root / "docs" / "plans").mkdir(parents=True)

    import coordinator_core.ops.spec_backlink_resolve as sbr

    monkeypatch.setattr(sbr, "_doe_root_path", lambda: peer_root)

    build_calls = []
    real_build_index = sbr.build_index

    def _tracking_build_index(root):
        build_calls.append(root)
        return real_build_index(root)

    monkeypatch.setattr(sbr, "build_index", _tracking_build_index)

    outcome = sbr.resolve(local_root, "doeclaude:pln-peer-fixture-plan-999999")
    assert outcome["outcome"] == "hit"
    assert outcome["path"] == str(peer_plan)
    assert outcome["queried_id"] == "doeclaude:pln-peer-fixture-plan-999999"
    # Only the peer root was indexed for this query — the local root was
    # never scanned (lazy peer resolution, local index untouched).
    assert peer_root in build_calls
    assert local_root not in build_calls


def test_peer_qualified_miss_when_doe_root_unresolvable(tmp_path, monkeypatch):
    import coordinator_core.ops.spec_backlink_resolve as sbr

    monkeypatch.setattr(sbr, "_doe_root_path", lambda: None)
    local_root = tmp_path / "local-repo"
    (local_root / "docs" / "plans").mkdir(parents=True)

    outcome = sbr.resolve(local_root, "doeclaude:pln-whatever-000000")
    assert outcome["outcome"] == "miss"


# ---------------------------------------------------------------------------
# Handler-level smoke test (repo_root=None safe-empty path)
# ---------------------------------------------------------------------------


def test_handler_returns_typed_miss_when_repo_root_none():
    outcome = _resolve_handler({"id": "pln-whatever-000000"}, repo_root=None)
    assert outcome["outcome"] == "miss"


def test_handler_hits_against_real_worktree(spec_backlink_corpus):
    root = _worktree_root(spec_backlink_corpus)
    # repo_root is normally the .git common dir; main_worktree_root derives
    # the worktree from it. For this handler-level smoke test we bypass that
    # derivation by calling resolve() directly against the already-known
    # worktree root (main_worktree_root's own contract is exercised by
    # deliverable_rollup's existing tests, not re-tested here).
    outcome = resolve(root, "pln-fixture-plan-full-aaaaaa")
    assert outcome["outcome"] == "hit"
    assert outcome["path"] == str(spec_backlink_corpus["plan_full"])

"""
coordinator_core.ops.tests.test_spec_backlink_resolve — tests for the C1 resolver.

Spec backlink: pln-spec-backlinks-cite-a-stable-d-451b3e § C1

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
    resolve_path_with_index,
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
    all_paths: set = set()
    for paths in index.plan_id_to_paths.values():
        all_paths.update(paths)
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


def test_duplicate_plan_id_is_ambiguity_not_last_write_wins(tmp_path):
    """Review: code-reviewer P3 — a `plan_id` collision (two records
    carrying the same `plan_id`, a genuine duplicate/copy-paste on this
    supposedly per-file-identity field) must be a typed AMBIGUITY, exactly
    like a `deliverable_id` collision — never last-write-wins on whichever
    path an unordered directory traversal happens to visit last."""
    docs_plans = tmp_path / "docs" / "plans"
    docs_plans.mkdir(parents=True)

    shared_pln = "pln-uncovered-shared-abcdef"

    def _write(name: str) -> Path:
        p = docs_plans / name
        p.write_text(
            "\n".join(
                [
                    "---",
                    f'title: "{name}"',
                    "created: 2026-08-13",
                    f'plan_id: "{shared_pln}"',
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

    path_a = _write("2026-08-13-duplicate-plan-a.md")
    path_b = _write("2026-08-13-duplicate-plan-b.md")

    index = build_index(tmp_path)
    outcome = resolve_id(index, shared_pln)
    assert outcome["outcome"] == "ambiguity"
    assert outcome["outcome"] != "hit"
    assert outcome["outcome"] != "miss"
    assert set(outcome["candidates"]) == {str(path_a), str(path_b)}


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
    """A `<repo>:pln-...` query, using the one recognized qualifier
    (`DoE-claude:`, matching `rewrite_spec_backlinks._PEER_REPO_NAME`'s fixed
    emit literal), must trigger a lazy peer-repo index build rooted at
    whatever coordinator_registry.doe_root() resolves to — never the local
    worktree_root — and never build the peer index for a local-only query."""
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

    outcome = sbr.resolve(local_root, "DoE-claude:pln-peer-fixture-plan-999999")
    assert outcome["outcome"] == "hit"
    assert outcome["path"] == str(peer_plan)
    assert outcome["queried_id"] == "DoE-claude:pln-peer-fixture-plan-999999"
    # Only the peer root was indexed for this query — the local root was
    # never scanned (lazy peer resolution, local index untouched).
    assert peer_root in build_calls
    assert local_root not in build_calls


def test_peer_qualified_miss_when_doe_root_unresolvable(tmp_path, monkeypatch):
    import coordinator_core.ops.spec_backlink_resolve as sbr

    monkeypatch.setattr(sbr, "_doe_root_path", lambda: None)
    local_root = tmp_path / "local-repo"
    (local_root / "docs" / "plans").mkdir(parents=True)

    outcome = sbr.resolve(local_root, "DoE-claude:pln-whatever-000000")
    assert outcome["outcome"] == "miss"


def test_unrecognized_repo_qualifier_is_typed_miss_not_silent_peer_hit(tmp_path, monkeypatch):
    """Review: code-reviewer P2 — a queried_id carrying any qualifier OTHER
    than the one recognized peer name (`DoE-claude`) must be a typed miss
    naming the unrecognized-repo condition, never silently routed to the
    DoE-claude peer index. This is the case `test_peer_qualified_hit_uses_doe_root`
    used to (accidentally) certify as a HIT before it was corrected to use
    the real qualifier."""
    import coordinator_core.ops.spec_backlink_resolve as sbr

    peer_root = tmp_path / "peer-repo"
    peer_docs_plans = peer_root / "docs" / "plans"
    peer_docs_plans.mkdir(parents=True)
    (peer_docs_plans / "2026-08-13-peer-fixture-plan.md").write_text(
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

    monkeypatch.setattr(sbr, "_doe_root_path", lambda: peer_root)

    build_calls = []
    real_build_index = sbr.build_index

    def _tracking_build_index(root):
        build_calls.append(root)
        return real_build_index(root)

    monkeypatch.setattr(sbr, "build_index", _tracking_build_index)

    outcome = sbr.resolve(local_root, "doeclaude:pln-peer-fixture-plan-999999")
    assert outcome["outcome"] == "miss"
    assert outcome["path"] is None
    assert outcome["reason"] == "unrecognized_repo_qualifier"
    # The unrecognized qualifier must be refused before any index build --
    # never build the peer index just to discover it wasn't the right repo.
    assert build_calls == []


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


# ---------------------------------------------------------------------------
# C1-archived — path->ids resolution across a docs/plans/ <-> archive/specs/
# git-mv, via the basename join key (reused from
# assert_no_dangling_plan_backlinks.py's _build_moved_plan_map, not a looser
# stem/slug match).
# ---------------------------------------------------------------------------


def test_cited_docs_plans_path_resolves_via_archived_twin(spec_backlink_corpus):
    """A citation to the OLD docs/plans/ path of a record that
    fleet.archive_completed_plans has since git-mv'd to archive/specs/YYYY-MM/
    must still resolve, emitting the archived record's ids — this is the
    plan's central "healable today" population."""
    root = _worktree_root(spec_backlink_corpus)
    index = build_index(root)
    # archived_full lives ONLY at archive/specs/2026-08/2026-08-01-fixture-archived-full.md
    cited_path = "docs/plans/2026-08-01-fixture-archived-full.md"
    outcome = resolve_path_with_index(index, root, cited_path)
    assert outcome["outcome"] == "hit"
    assert outcome["cited_path"] == cited_path
    assert outcome["plan_id"] == "pln-fixture-archived-full-222222"
    assert outcome["deliverable_id"] == "dlv-fixture-archived-full-333333"


def test_cited_archive_path_resolves_via_live_twin(spec_backlink_corpus):
    """Converse of the above: a citation to an archive/specs/ path whose
    record actually lives (only) under docs/plans/ must also resolve via the
    same basename join."""
    root = _worktree_root(spec_backlink_corpus)
    index = build_index(root)
    cited_path = "archive/specs/2026-08/2026-08-13-fixture-plan-full.md"
    outcome = resolve_path_with_index(index, root, cited_path)
    assert outcome["outcome"] == "hit"
    assert outcome["cited_path"] == cited_path
    assert outcome["plan_id"] == "pln-fixture-plan-full-aaaaaa"
    assert outcome["deliverable_id"] == "dlv-fixture-plan-full-bbbbbb"


def test_stem_present_under_both_roots_is_ambiguity_not_silent_pick(tmp_path):
    """A cited basename that exists as a real-id record under BOTH
    docs/plans/ AND archive/specs/ (never actually moved, or a stale/duplicate
    situation) must return the typed AMBIGUITY outcome, never silently pick
    one candidate."""
    docs_plans = tmp_path / "docs" / "plans"
    archive_specs_a = tmp_path / "archive" / "specs" / "2026-07"
    archive_specs_b = tmp_path / "archive" / "specs" / "2026-08"
    docs_plans.mkdir(parents=True)
    archive_specs_a.mkdir(parents=True)
    archive_specs_b.mkdir(parents=True)

    stem = "2026-08-13-duplicate-stem.md"

    def _write(dest: Path, plan_id: str) -> None:
        dest.write_text(
            "\n".join(
                [
                    "---",
                    'title: "duplicate stem"',
                    "created: 2026-08-13",
                    f'plan_id: "{plan_id}"',
                    "scope_mode: feature",
                    "---",
                    "",
                    "# duplicate stem",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    # The cited docs/plans/ path does NOT literally exist (exact-path lookup
    # misses) -- the basename `stem` instead resolves two DIFFERENT real
    # records via the fallback (two archive/specs/YYYY-MM/ twins), which is
    # what must trip AMBIGUITY rather than a silent pick.
    _write(archive_specs_a / stem, "pln-duplicate-archived-a-000001")
    _write(archive_specs_b / stem, "pln-duplicate-archived-b-000002")

    index = build_index(tmp_path)
    cited_path = f"docs/plans/{stem}"
    outcome = resolve_path_with_index(index, tmp_path, cited_path)
    assert outcome["outcome"] == "ambiguity"
    assert outcome["outcome"] not in {"hit", "miss"}
    assert set(outcome["candidates"]) == {
        str(archive_specs_a / stem),
        str(archive_specs_b / stem),
    }


def test_cited_path_absent_from_both_roots_stays_typed_miss(spec_backlink_corpus):
    """A cited path with no record under either root (the "absent from this
    repo" population, C7/C8) must stay a typed MISS — the archived-twin
    fallback must not start resolving these."""
    root = _worktree_root(spec_backlink_corpus)
    index = build_index(root)
    outcome = resolve_path_with_index(
        index, root, "docs/plans/2026-08-13-does-not-exist-anywhere.md"
    )
    assert outcome["outcome"] == "miss"
    assert outcome["plan_id"] is None
    assert outcome["deliverable_id"] is None

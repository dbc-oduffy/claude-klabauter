"""coordinator_core.tests.test_c6_pointer_normalization — C6 pointer-normalization seam.

Coverage:
  (a) Unit coverage of each of the six observed value encodings (bare filename,
      state/-relative path, archive/-relative path, quoted variants of either, the
      literal string "none", YAML null) normalizing to the same resolved pointer via
      dag.resolve_target — with and without the id_index parameter, confirming the
      new parameter is a pure addition (path/filename-shaped refs are byte-for-byte
      unaffected whether or not an id_index is supplied).
  (b) Unit coverage of the id-suffixed field aliases (predecessor_id, origin_handoff_id)
      — a handoff naming its parent ONLY via the _id field (no plain field present) is
      now resolved by dag.referenced_by, where before this seam it was silently dropped
      (the _id field was in no edge-kind set at all).
  (c) Differential-oracle agreement: coordinator_core.dag's pointer resolution (engine)
      vs. _baton_dag_oracle's independent from-scratch normalization (oracle) agree on
      "who points at this baton" for every live baton in the DoE-claude corpus (~255
      files) and the claude-klabauter corpus (~95 files), checked separately for the
      predecessor-family ({'predecessor', 'predecessor_id'}) and origin_handoff-family
      ({'origin_handoff', 'origin_handoff_id'}) pointer sets. Comparison is on POINTER
      RESOLUTION ONLY — no edge-kind-set is added, removed, or unified by this test;
      origin_handoff stays a deliberate explicit-opt-in edge kind exactly as before.

Spec backlink: DoE-claude:pln-push-side-write-discipline-for-05c30d chunk C6.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Set

import pytest

from coordinator_core import dag
from coordinator_core.doe_root_pointer import read_doe_root_pointer

from . import _baton_dag_oracle as oracle


# Fixture: clear dag._FRONTMATTER_CACHE between tests (mirrors test_dag_edge_kinds.py

@pytest.fixture(autouse=True)
def clear_frontmatter_cache():
    dag._FRONTMATTER_CACHE.clear()
    yield
    dag._FRONTMATTER_CACHE.clear()


def _write_handoff(path: Path, *, slug: str, status: str = "active", **extra_fields) -> None:
    extra_lines = "".join(f"{k}: {v}\n" for k, v in extra_fields.items())
    path.write_text(
        f"---\n"
        f"slug: {slug}\n"
        f"status: {status}\n"
        f"{extra_lines}"
        f"---\n"
        f"# Handoff body\n"
    )


class TestSixValueEncodings:
    @pytest.fixture
    def repo(self, tmp_path: Path):
        state_dir = tmp_path / "state" / "handoffs"
        state_dir.mkdir(parents=True)
        parent = state_dir / "2026-07-01_000000_parent.md"
        _write_handoff(parent, slug="parent", handoff_id="hnd-parent-000001")
        return tmp_path, state_dir, parent

    @pytest.mark.parametrize(
        "encode",
        [
            pytest.param(lambda basename, rel: basename, id="bare-filename"),
            pytest.param(lambda basename, rel: f"state/handoffs/{basename}", id="state-relative"),
            pytest.param(lambda basename, rel: f'"{basename}"', id="quoted-bare-filename"),
            pytest.param(lambda basename, rel: f'"state/handoffs/{basename}"', id="quoted-state-relative"),
        ],
    )
    def test_resolves_to_parent_path(self, repo, encode):
        tmp_path, state_dir, parent = repo
        basename = parent.name
        raw = encode(basename, None)
        target = raw.strip('"').strip("'")
        resolved = dag.resolve_target(target, str(state_dir), str(tmp_path))
        assert resolved == str(parent.resolve())

    def test_archive_relative_resolves(self, tmp_path: Path):
        archive_dir = tmp_path / "archive" / "handoffs"
        archive_dir.mkdir(parents=True)
        state_dir = tmp_path / "state" / "handoffs"
        state_dir.mkdir(parents=True)
        parent = archive_dir / "2026-07-01_000000_parent.md"
        _write_handoff(parent, slug="parent")
        resolved = dag.resolve_target(
            f"archive/handoffs/{parent.name}", str(state_dir), str(tmp_path)
        )
        assert resolved == str(parent.resolve())

    def test_literal_none_resolves_to_none(self, repo):
        tmp_path, state_dir, _parent = repo
        assert dag.resolve_target("none", str(state_dir), str(tmp_path)) is None

    def test_null_value_resolves_to_none(self, repo):
        tmp_path, state_dir, _parent = repo
        assert dag.resolve_target(None, str(state_dir), str(tmp_path)) is None

    def test_id_index_is_a_pure_addition_for_path_shaped_refs(self, repo):
        tmp_path, state_dir, parent = repo
        basename = parent.name
        no_index = dag.resolve_target(basename, str(state_dir), str(tmp_path))
        with_index = dag.resolve_target(
            basename, str(state_dir), str(tmp_path), id_index={"hnd-unrelated": "/nowhere"}
        )
        assert no_index == with_index == str(parent.resolve())


class TestIdSuffixedFieldAliases:
    def test_predecessor_id_only_is_now_resolved(self, tmp_path: Path):
        state_dir = tmp_path / "state" / "handoffs"
        state_dir.mkdir(parents=True)
        parent = state_dir / "2026-07-01_000000_parent.md"
        _write_handoff(parent, slug="parent", handoff_id="hnd-parent-abc123")
        child = state_dir / "2026-07-02_000000_child.md"
        _write_handoff(child, slug="child", predecessor_id='"hnd-parent-abc123"')

        result = dag.referenced_by(
            str(parent), [str(parent), str(child)], edge_kinds={"predecessor"}
        )
        assert result["referenced"] is True
        assert str(child.resolve()) in result["referencedBy"]

    def test_origin_handoff_id_only_is_resolved_on_explicit_opt_in(self, tmp_path: Path):
        state_dir = tmp_path / "state" / "handoffs"
        state_dir.mkdir(parents=True)
        source = state_dir / "2026-07-01_000000_source.md"
        _write_handoff(source, slug="source", handoff_id="hnd-source-abc123")
        spinoff = state_dir / "2026-07-02_000000_spinoff.md"
        _write_handoff(spinoff, slug="spinoff", origin_handoff_id='"hnd-source-abc123"')

        default_result = dag.referenced_by(str(source), [str(source), str(spinoff)])
        assert str(spinoff.resolve()) not in default_result["referencedBy"]

        explicit_result = dag.referenced_by(
            str(source), [str(source), str(spinoff)], edge_kinds={"origin_handoff"}
        )
        assert str(spinoff.resolve()) in explicit_result["referencedBy"]

    def test_stale_id_does_not_false_match(self, tmp_path: Path):
        state_dir = tmp_path / "state" / "handoffs"
        state_dir.mkdir(parents=True)
        decoy = state_dir / "2026-07-01_000000_decoy.md"
        _write_handoff(decoy, slug="decoy")
        child = state_dir / "2026-07-02_000000_child.md"
        _write_handoff(child, slug="child", predecessor_id="hnd-does-not-exist-000000")

        result = dag.referenced_by(
            str(decoy), [str(decoy), str(child)], edge_kinds={"predecessor"}
        )
        assert result["referenced"] is False


def _corpus_agreement(root: str, fields, edge_kinds: Set[str]) -> None:
    live_paths, oracle_children = oracle.build_children_index(root, fields=fields)
    assert live_paths, f"expected a non-empty live handoff set under {root}"

    all_corpus_paths = oracle.collect_corpus_paths(root)
    handoff_dir = os.path.dirname(all_corpus_paths[0])

    # equivalence argument: the per-node work is target-INDEPENDENT, so it
    # `_FRONTMATTER_CACHE` does not rescue the old shape and its absence is
    reverse_index = dag.build_reverse_edge_index(
        all_corpus_paths, handoff_dir=handoff_dir, edge_kinds=edge_kinds
    )

    mismatches = []
    for baton_path in live_paths:
        baton_basename = os.path.basename(baton_path)
        oracle_set = oracle_children.get(baton_basename, set())

        engine_result = dag.referenced_by_indexed(
            baton_path, reverse_index, edge_kinds=edge_kinds
        )
        engine_set = {os.path.basename(p) for p in engine_result["referencedBy"]}

        if oracle_set != engine_set:
            mismatches.append((baton_basename, sorted(oracle_set), sorted(engine_set)))

    assert not mismatches, (
        f"pointer-resolution disagreement between dag engine and independent oracle "
        f"for {len(mismatches)} baton(s) under {root} "
        f"(edge_kinds={sorted(edge_kinds)}): {mismatches[:10]}"
    )


class TestForeignFamilyPointerIsNotRehomed:

    @pytest.fixture
    def repo(self, tmp_path: Path):
        state_dir = tmp_path / "state" / "handoffs"
        state_dir.mkdir(parents=True)
        (tmp_path / "cross-repo" / "archive").mkdir(parents=True)
        baton = state_dir / "2026-08-17_000000_memo-topic.md"
        _write_handoff(
            baton,
            slug="memo-topic",
            predecessor="cross-repo/inbox/2026-08-17_000000_memo-topic.md",
        )
        return tmp_path, state_dir, baton

    def test_resolve_target_does_not_rehome_onto_same_basename_baton(self, repo):
        tmp_path, state_dir, baton = repo
        resolved = dag.resolve_target(
            "cross-repo/inbox/2026-08-17_000000_memo-topic.md",
            str(state_dir),
            str(tmp_path),
            include_history_tier=False,
        )
        assert resolved is None, (
            "a ref naming cross-repo/inbox/ must not resolve to a same-basename "
            f"handoff; got {resolved!r}"
        )

    def test_referenced_by_reports_no_self_edge(self, repo):
        tmp_path, state_dir, baton = repo
        result = dag.referenced_by(
            str(baton),
            [str(baton)],
            edge_kinds={"predecessor"},
            handoff_dir=str(state_dir),
        )
        assert result["referencedBy"] == []
        assert result["referenced"] is False

    def test_baton_family_ref_still_basename_recovers(self, repo):
        tmp_path, state_dir, baton = repo
        parent = state_dir / "2026-07-01_000000_parent.md"
        _write_handoff(parent, slug="parent")
        resolved = dag.resolve_target(
            "archive/handoffs/2026-07/2026-07-01_000000_parent.md",
            str(state_dir),
            str(tmp_path),
            include_history_tier=False,
        )
        assert resolved is not None
        assert os.path.abspath(resolved) == os.path.abspath(str(parent))


class TestReverseEdgeIndexCoverage:
    """An index answers only for the kinds it was built over, and says so.

    `referenced_by_indexed` filters a prebuilt index in memory and cannot
    consult disk. Asked for a kind the index never carried it used to return
    `{'referenced': False, 'referencedBy': []}` -- a well-formed answer,
    indistinguishable from a genuine no-referencer result, for a question the
    index structurally could not answer.

    That was reachable by following `build_reverse_edge_index`'s own "safe to
    swap in" equivalence argument: `origin_handoff` is not in
    ARCHIVAL_EDGE_KINDS, so swapping `referenced_by` for the indexed form on
    that kind silently changed the answer to empty. Found 2026-09-01 when this
    file's own oracle-agreement tests were hoisted onto the indexed seam and
    the `origin_handoff` families went red while `predecessor` passed.
    """

    def _corpus(self):
        root = str(Path(__file__).resolve().parents[2])
        return root, oracle.collect_corpus_paths(root)

    def test_index_records_the_kinds_it_covers(self):
        root, paths = self._corpus()
        index = dag.build_reverse_edge_index(
            paths, handoff_dir=os.path.dirname(paths[0])
        )
        assert index["edge_kinds"] == frozenset(dag.ARCHIVAL_EDGE_KINDS)

        widened = dag.build_reverse_edge_index(
            paths,
            handoff_dir=os.path.dirname(paths[0]),
            edge_kinds={"origin_handoff"},
        )
        assert widened["edge_kinds"] == frozenset({"origin_handoff"})

    def test_uncovered_kind_raises_instead_of_answering_empty(self):
        root, paths = self._corpus()
        index = dag.build_reverse_edge_index(
            paths, handoff_dir=os.path.dirname(paths[0])
        )
        with pytest.raises(ValueError) as exc:
            dag.referenced_by_indexed(
                paths[0], index, edge_kinds={"origin_handoff"}
            )
        assert "origin_handoff" in str(exc.value)
        assert "build_reverse_edge_index" in str(exc.value)

    def test_a_covered_kind_still_answers(self):
        root, paths = self._corpus()
        index = dag.build_reverse_edge_index(
            paths,
            handoff_dir=os.path.dirname(paths[0]),
            edge_kinds={"origin_handoff"},
        )
        result = dag.referenced_by_indexed(
            paths[0], index, edge_kinds={"origin_handoff"}
        )
        assert set(result) == {"referenced", "referencedBy"}

    def test_a_legacy_index_without_coverage_is_read_as_archival(self):
        """An index built before coverage was recorded carries exactly
        ARCHIVAL_EDGE_KINDS by construction, so absence is not unknown."""
        root, paths = self._corpus()
        index = dag.build_reverse_edge_index(
            paths, handoff_dir=os.path.dirname(paths[0])
        )
        del index["edge_kinds"]
        dag.referenced_by_indexed(paths[0], index, edge_kinds={"predecessor"})
        with pytest.raises(ValueError):
            dag.referenced_by_indexed(
                paths[0], index, edge_kinds={"origin_handoff"}
            )


class TestDifferentialOracleAgreement:
    def test_claude_klabauter_predecessor_family(self):
        root = str(Path(__file__).resolve().parents[2])
        assert os.path.isdir(os.path.join(root, "state", "handoffs"))
        _corpus_agreement(root, oracle.PREDECESSOR_LINK_FIELDS, {"predecessor"})

    def test_claude_klabauter_origin_handoff_family(self):
        root = str(Path(__file__).resolve().parents[2])
        _corpus_agreement(root, oracle.ORIGIN_HANDOFF_LINK_FIELDS, {"origin_handoff"})

    @pytest.mark.real_home
    def test_doe_claude_predecessor_family(self):
        doe_root = read_doe_root_pointer()
        if not doe_root or not os.path.isdir(os.path.join(doe_root, "state", "handoffs")):
            pytest.skip(
                "DoE-claude root not resolvable via read_doe_root_pointer() on this "
                "machine — this cross-repo differential check requires a DoE-claude "
                "sibling checkout and is not part of the portable pytest surface."
            )
        _corpus_agreement(doe_root, oracle.PREDECESSOR_LINK_FIELDS, {"predecessor"})

    @pytest.mark.real_home
    def test_doe_claude_origin_handoff_family(self):
        doe_root = read_doe_root_pointer()
        if not doe_root or not os.path.isdir(os.path.join(doe_root, "state", "handoffs")):
            pytest.skip(
                "DoE-claude root not resolvable via read_doe_root_pointer() on this "
                "machine — this cross-repo differential check requires a DoE-claude "
                "sibling checkout and is not part of the portable pytest surface."
            )
        _corpus_agreement(doe_root, oracle.ORIGIN_HANDOFF_LINK_FIELDS, {"origin_handoff"})

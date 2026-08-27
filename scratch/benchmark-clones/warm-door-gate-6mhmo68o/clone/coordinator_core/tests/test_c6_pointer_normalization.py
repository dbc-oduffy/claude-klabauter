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


# ---------------------------------------------------------------------------
# Fixture: clear dag._FRONTMATTER_CACHE between tests (mirrors test_dag_edge_kinds.py
# convention).
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# (a) Six value encodings, via resolve_target
# ---------------------------------------------------------------------------

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
        # Mirror how dag._parse_frontmatter would have already stripped quotes —
        # resolve_target is exercised directly here, so strip them the same way
        # a real frontmatter read would have (quote-stripping is _parse_scalar's
        # job, not resolve_target's; see six-encodings coverage via referenced_by
        # below for the full parse-then-resolve path).
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
        """A path/filename-shaped ref resolves identically whether or not an
        (irrelevant) id_index is supplied — id_index only intercepts refs that
        do not end in '.md'."""
        tmp_path, state_dir, parent = repo
        basename = parent.name
        no_index = dag.resolve_target(basename, str(state_dir), str(tmp_path))
        with_index = dag.resolve_target(
            basename, str(state_dir), str(tmp_path), id_index={"hnd-unrelated": "/nowhere"}
        )
        assert no_index == with_index == str(parent.resolve())


# ---------------------------------------------------------------------------
# (b) Id-suffixed field aliases — predecessor_id / origin_handoff_id
# ---------------------------------------------------------------------------

class TestIdSuffixedFieldAliases:
    def test_predecessor_id_only_is_now_resolved(self, tmp_path: Path):
        """A child naming its parent ONLY via predecessor_id (no plain `predecessor`
        field at all) is found by referenced_by — this is the C6 seam's core gap-fix.
        """
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

        # Default edge_kinds does NOT include origin_handoff — ratified exclusion,
        # untouched by this seam.
        default_result = dag.referenced_by(str(source), [str(source), str(spinoff)])
        assert str(spinoff.resolve()) not in default_result["referencedBy"]

        explicit_result = dag.referenced_by(
            str(source), [str(source), str(spinoff)], edge_kinds={"origin_handoff"}
        )
        assert str(spinoff.resolve()) in explicit_result["referencedBy"]

    def test_stale_id_does_not_false_match(self, tmp_path: Path):
        """An id ref that resolves to nothing (handoff_id not in the corpus) must
        never fall back to matching some unrelated live node — fail-closed, not
        fail-open, on an unresolvable id-shaped ref."""
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


# ---------------------------------------------------------------------------
# (c) Differential-oracle agreement over the real DoE-claude + claude-klabauter corpora
# ---------------------------------------------------------------------------

def _corpus_agreement(root: str, fields, edge_kinds: Set[str]) -> None:
    live_paths, oracle_children = oracle.build_children_index(root, fields=fields)
    assert live_paths, f"expected a non-empty live handoff set under {root}"

    all_corpus_paths = oracle.collect_corpus_paths(root)
    handoff_dir = os.path.dirname(all_corpus_paths[0])

    mismatches = []
    for baton_path in live_paths:
        baton_basename = os.path.basename(baton_path)
        oracle_set = oracle_children.get(baton_basename, set())

        engine_result = dag.referenced_by(
            baton_path, all_corpus_paths, edge_kinds=edge_kinds, handoff_dir=handoff_dir
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
    """A pointer naming a non-baton family must not basename-recover onto a baton.

    Regression: `state/handoffs/<name>.md` carrying
    `predecessor: cross-repo/inbox/<name>.md` (the memo-pickup convention, where
    the handoff inherits the memo's slug) resolved onto ITSELF once the memo was
    archived out of `cross-repo/inbox/`, because resolve_target's stale-path
    recovery tier probes `state/handoffs/<basename>` regardless of the directory
    the ref names. `referenced_by` then reported the baton as its own referencer,
    which blocks its archival forever. Fixture-backed rather than corpus-backed:
    the differential-oracle tests below can only catch this while the offending
    record happens to be in the live corpus.
    """

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
        """Negative control — the stale-path recovery this fix narrows still works."""
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

"""Arms over `coordinator_core.frontmatter.schema_corpus`.

The defect these pin: a cloud container resolves `data_root("schemas")` to the
published coordinator-claude mirror, whose `schemas/` dir EXISTS, parses, and
yields a registry built from 2 loadable schemas where an authoring checkout
yields 68. Every consumer saw a valid-looking root, so the shortfall could not
be told from a broken one and nothing announced it.

Every fixture here is a REAL directory holding REAL schema files copied out of
`coordinator_core/frontmatter/schemas/`, with the REAL markers placed around it
— not a hand-written classification dict. The resolution being tested IS the
walk from a schemas dir up to its markers, so a fixture that skips the disk
skips the test.

The module answers one bit, so these arms pin the bit and the EVIDENCE ORDER
that produces it, not a taxonomy. The load-bearing claims: the answer comes off
disk markers and never off a file count, and the authoring sentinel is probed
BEFORE the published marker.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from coordinator_core._content_root_primitive import FLAT_CONTENT_ROOT_MARKER
from coordinator_core.frontmatter.schema_corpus import (
    DEV_REPO_SENTINEL,
    published_subset_reason,
)

_REAL_SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"


def _real_schema_files(limit: int) -> list[Path]:
    files = sorted(
        p for p in _REAL_SCHEMAS.iterdir()
        if p.is_file() and (p.name.endswith(".yaml") or p.name.endswith(".schema.json"))
    )
    assert len(files) >= limit, f"vendored corpus too small to build fixtures: {len(files)}"
    return files[:limit]


def _populate(schemas_dir: Path, count: int) -> None:
    schemas_dir.mkdir(parents=True, exist_ok=True)
    for src in _real_schema_files(count):
        shutil.copy2(src, schemas_dir / src.name)


def _mark_published(content_root: Path) -> None:
    marker = content_root.joinpath(*FLAT_CONTENT_ROOT_MARKER)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"name": "coordinator-claude"}', encoding="utf-8")


class TestAnswersByMarkersNotByCount:
    def test_published_mirror_with_a_small_corpus_is_a_subset_not_a_fault(self, tmp_path):
        root = tmp_path / "coordinator-claude"
        _populate(root / "schemas", 2)
        _mark_published(root)

        reason = published_subset_reason(root / "schemas")
        assert reason is not None
        assert "2 schema file(s)" in reason

    def test_authoring_checkout_private_layout_is_the_whole_set(self, tmp_path):
        repo = tmp_path / "DoE-claude"
        _populate(repo / "coordinator" / "schemas", 5)
        (repo / DEV_REPO_SENTINEL).write_text("", encoding="utf-8")
        _mark_published(repo / "coordinator")

        assert published_subset_reason(repo / "coordinator" / "schemas") is None

    def test_authoring_sentinel_beats_the_published_marker(self, tmp_path):
        """An authoring checkout carries BOTH markers — it is the tree the
        mirror is published FROM — so marker order is load-bearing, not
        incidental. Probed the other way round, this root would be reported as
        a published subset and the gate it feeds would stand down on the one
        tree it exists to check."""
        root = tmp_path / "flat-authoring"
        _populate(root / "schemas", 3)
        _mark_published(root)
        (root / DEV_REPO_SENTINEL).write_text("", encoding="utf-8")

        assert published_subset_reason(root / "schemas") is None

    def test_a_large_corpus_under_no_marker_makes_no_claim(self, tmp_path):
        """The count is reported, never consulted: a big unmarked dir earns no
        published-subset claim, the same way a small marked one earns no
        fault. No count threshold exists to tune.

        Review: overengineering-reviewer (Kira, pass 2, finding N3) -- the
        companion tiny-corpus arm was removed. After the F1 collapse the count
        is computed only INSIDE the already-answered published branch, so no
        code path can branch on it: the no-threshold rule holds by construction
        and this arm is the pin for the unmarked direction."""
        root = tmp_path / "somewhere"
        _populate(root / "schemas", 10)

        assert published_subset_reason(root / "schemas") is None

    def test_absent_directory_makes_no_claim_and_does_not_raise(self, tmp_path):
        assert published_subset_reason(tmp_path / "nope" / "schemas") is None

    def test_reason_names_the_evidence_that_answered(self, tmp_path):
        root = tmp_path / "coordinator-claude"
        _populate(root / "schemas", 2)
        _mark_published(root)
        reason = published_subset_reason(root / "schemas")
        assert str(root) in reason
        assert DEV_REPO_SENTINEL in reason


class TestLiveResolvedCorpusIsAnswered:
    def test_the_resolved_data_root_corpus_answers_without_raising(self):
        """Whatever this machine resolves — authoring tree, published mirror, or
        neither — the answer is a reason string or None, never an exception and
        never a silent pass-through. This is the arm that would have caught the
        container case without knowing which shape the container is."""
        from coordinator_core.data_root import data_root

        try:
            resolved = data_root("schemas")
        except RuntimeError:
            return  # no coordinator content root on this box — nothing to answer

        reason = published_subset_reason(resolved)
        assert reason is None or (isinstance(reason, str) and reason)

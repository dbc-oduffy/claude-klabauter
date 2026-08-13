"""
Tests for coordinator_core.ops.rewrite_spec_backlinks.

Uses the shared spec_backlink_corpus fixture (conftest.py) rather than
rebuilding fixture files. C1's real resolver is not assumed importable in
this wave -- a local stub resolver stands in for the C1 seam, matching the
typed HIT/MISS/AMBIGUITY contract pinned in the plan's enriched C3 stub.

Spec backlink: docs/plans/2026-08-13-spec-backlinks-cite-a-stable-deliverable-id.md § C3
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.rewrite_spec_backlinks import (
    rewrite_file,
    rewrite_spec_backlinks,
)
from coordinator_core.ops.tests.conftest import build_spec_backlink_corpus


def _stub_resolver(corpus: dict[str, Path]):
    """A local stand-in for C1's spec_backlink_resolve.resolve, keyed on the
    fixture's own path->record mapping. Returns the typed outcome dict
    C3 must consume: {"outcome": "HIT"/"MISS"/"AMBIGUITY", ...}."""

    mapping = {
        "docs/plans/2026-08-13-fixture-plan-full.md": {
            "outcome": "HIT",
            "plan_id": "fixture-plan-full-aaaaaa",
            "deliverable_id": "fixture-plan-full-bbbbbb",
        },
        "docs/plans/2026-08-13-fixture-plan-dlv-only.md": {
            "outcome": "HIT",
            "plan_id": None,
            "deliverable_id": "fixture-plan-dlv-only-cccccc",
        },
        "docs/plans/2026-08-13-fixture-plan-pln-only.md": {
            "outcome": "HIT",
            "plan_id": "fixture-plan-pln-only-dddddd",
            "deliverable_id": None,
        },
        "docs/plans/2026-08-13-fixture-plan-null-ids.md": {
            "outcome": "HIT",
            "plan_id": None,
            "deliverable_id": None,
        },
        "docs/plans/2026-08-13-fixture-plan-ambiguous-a.md": {
            "outcome": "AMBIGUITY",
        },
        "docs/plans/2026-08-13-fixture-plan-ambiguous-b.md": {
            "outcome": "AMBIGUITY",
        },
        "docs/plans/2026-08-13-does-not-exist.md": {
            "outcome": "MISS",
        },
    }

    def _resolve(cited_path: str) -> dict:
        return mapping.get(cited_path, {"outcome": "MISS"})

    return _resolve


def test_rewrite_preserves_anchor_and_surrounding_text(tmp_path: Path) -> None:
    corpus = build_spec_backlink_corpus(tmp_path)
    resolver = _stub_resolver(corpus)
    citing_file = corpus["citing_file"]

    before = citing_file.read_text(encoding="utf-8")
    assert "# Spec backlink: docs/plans/2026-08-13-fixture-plan-full.md § AC1" in before

    report = rewrite_file(citing_file, resolver=resolver)

    after = citing_file.read_text(encoding="utf-8")
    assert "# Spec backlink: pln-fixture-plan-full-aaaaaa § AC1" in after
    assert report["rewritten"] == ["docs/plans/2026-08-13-fixture-plan-full.md"]
    assert report["unresolvable"] == []

    # everything else on the line and file is byte-for-byte preserved
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    assert len(before_lines) == len(after_lines)
    for b, a in zip(before_lines, after_lines):
        if "Spec backlink:" in b:
            continue
        assert b == a


def test_decoy_prose_mention_untouched(tmp_path: Path) -> None:
    corpus = build_spec_backlink_corpus(tmp_path)
    resolver = _stub_resolver(corpus)
    citing_file = corpus["citing_file"]

    rewrite_file(citing_file, resolver=resolver)

    after = citing_file.read_text(encoding="utf-8")
    assert (
        '"""See docs/plans/2026-08-13-fixture-plan-full.md for background'
        in after
    )


def test_unresolvable_untouched_and_reported(tmp_path: Path) -> None:
    corpus = build_spec_backlink_corpus(tmp_path)
    resolver = _stub_resolver(corpus)

    miss_file = corpus["root"] / "miss_citer.py"
    miss_file.write_text(
        "# Spec backlink: docs/plans/2026-08-13-does-not-exist.md § AC2\n",
        encoding="utf-8",
    )

    before = miss_file.read_text(encoding="utf-8")
    report = rewrite_file(miss_file, resolver=resolver)
    after = miss_file.read_text(encoding="utf-8")

    assert after == before
    assert report["rewritten"] == []
    assert report["unresolvable"] == ["docs/plans/2026-08-13-does-not-exist.md"]


def test_ambiguity_refuses_to_emit(tmp_path: Path) -> None:
    corpus = build_spec_backlink_corpus(tmp_path)
    resolver = _stub_resolver(corpus)

    ambiguous_file = corpus["root"] / "ambiguous_citer.py"
    ambiguous_file.write_text(
        "# Spec backlink: docs/plans/2026-08-13-fixture-plan-ambiguous-a.md § AC3\n",
        encoding="utf-8",
    )

    before = ambiguous_file.read_text(encoding="utf-8")
    report = rewrite_file(ambiguous_file, resolver=resolver)
    after = ambiguous_file.read_text(encoding="utf-8")

    assert after == before
    assert report["rewritten"] == []
    assert report["unresolvable"] == [
        "docs/plans/2026-08-13-fixture-plan-ambiguous-a.md"
    ]


def test_hit_with_neither_id_real_is_unresolvable(tmp_path: Path) -> None:
    corpus = build_spec_backlink_corpus(tmp_path)
    resolver = _stub_resolver(corpus)

    null_ids_file = corpus["root"] / "null_ids_citer.py"
    null_ids_file.write_text(
        "# Spec backlink: docs/plans/2026-08-13-fixture-plan-null-ids.md § AC4\n",
        encoding="utf-8",
    )

    before = null_ids_file.read_text(encoding="utf-8")
    report = rewrite_file(null_ids_file, resolver=resolver)
    after = null_ids_file.read_text(encoding="utf-8")

    assert after == before
    assert report["unresolvable"] == [
        "docs/plans/2026-08-13-fixture-plan-null-ids.md"
    ]


def test_idempotent_on_second_pass(tmp_path: Path) -> None:
    corpus = build_spec_backlink_corpus(tmp_path)
    resolver = _stub_resolver(corpus)
    citing_file = corpus["citing_file"]

    rewrite_file(citing_file, resolver=resolver)
    after_first = citing_file.read_text(encoding="utf-8")

    report_second = rewrite_file(citing_file, resolver=resolver)
    after_second = citing_file.read_text(encoding="utf-8")

    assert after_first == after_second
    assert report_second["rewritten"] == []
    assert report_second["unresolvable"] == []


def test_both_keys_present_rewrites_to_pln_form(tmp_path: Path) -> None:
    corpus = build_spec_backlink_corpus(tmp_path)
    resolver = _stub_resolver(corpus)

    both_keys_file = corpus["root"] / "both_keys_citer.py"
    both_keys_file.write_text(
        "# Spec backlink: docs/plans/2026-08-13-fixture-plan-full.md § AC5\n",
        encoding="utf-8",
    )

    report = rewrite_file(both_keys_file, resolver=resolver)
    after = both_keys_file.read_text(encoding="utf-8")

    assert "pln-fixture-plan-full-aaaaaa" in after
    assert "dlv-fixture-plan-full-bbbbbb" not in after
    assert report["rewritten"] == ["docs/plans/2026-08-13-fixture-plan-full.md"]


def test_batch_entry_point_aggregates_reported_set(tmp_path: Path) -> None:
    corpus = build_spec_backlink_corpus(tmp_path)
    resolver = _stub_resolver(corpus)

    miss_file = corpus["root"] / "batch_miss_citer.py"
    miss_file.write_text(
        "# Spec backlink: docs/plans/2026-08-13-does-not-exist.md § AC6\n",
        encoding="utf-8",
    )

    report = rewrite_spec_backlinks(
        [corpus["citing_file"], miss_file], resolver=resolver
    )

    assert str(corpus["citing_file"]) in report["rewritten"]
    assert report["rewritten"][str(corpus["citing_file"])] == [
        "docs/plans/2026-08-13-fixture-plan-full.md"
    ]
    assert str(miss_file) in report["unresolvable"]
    assert report["unresolvable"][str(miss_file)] == [
        "docs/plans/2026-08-13-does-not-exist.md"
    ]

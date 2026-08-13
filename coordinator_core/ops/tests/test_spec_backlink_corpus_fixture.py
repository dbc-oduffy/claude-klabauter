"""
coordinator_core.ops.tests.test_spec_backlink_corpus_fixture

Certification self-test for the `spec_backlink_corpus` fixture
(coordinator_core/ops/tests/conftest.py), authored for the C1/C2/C3 chunk
trio of docs/plans/2026-08-13-spec-backlinks-cite-a-stable-deliverable-id.md.

Asserts the fixture actually builds the corpus shape those three chunks'
stub bodies depend on, so an unverified fixture is never propagated to three
executors.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.tests.conftest import build_spec_backlink_corpus


def test_fixture_builds_docs_plans_variants(tmp_path: Path) -> None:
    paths = build_spec_backlink_corpus(tmp_path)

    full_text = paths["plan_full"].read_text(encoding="utf-8")
    assert 'plan_id: "pln-fixture-plan-full-aaaaaa"' in full_text
    assert 'deliverable_id: "dlv-fixture-plan-full-bbbbbb"' in full_text

    dlv_only_text = paths["plan_dlv_only"].read_text(encoding="utf-8")
    assert "plan_id:" not in dlv_only_text
    assert 'deliverable_id: "dlv-fixture-plan-dlv-only-cccccc"' in dlv_only_text

    pln_only_text = paths["plan_pln_only"].read_text(encoding="utf-8")
    assert 'plan_id: "pln-fixture-plan-pln-only-dddddd"' in pln_only_text
    assert "deliverable_id:" not in pln_only_text

    null_text = paths["plan_null_ids"].read_text(encoding="utf-8")
    assert "plan_id: null" in null_text
    assert "deliverable_id: null" in null_text

    no_ids_text = paths["plan_no_ids"].read_text(encoding="utf-8")
    assert "plan_id:" not in no_ids_text
    assert "deliverable_id:" not in no_ids_text


def test_fixture_builds_ambiguous_dlv_pair(tmp_path: Path) -> None:
    paths = build_spec_backlink_corpus(tmp_path)

    text_a = paths["plan_ambiguous_a"].read_text(encoding="utf-8")
    text_b = paths["plan_ambiguous_b"].read_text(encoding="utf-8")

    assert "dlv-fixture-shared-eeeeee" in text_a
    assert "dlv-fixture-shared-eeeeee" in text_b
    assert 'plan_id: "pln-fixture-plan-ambiguous-a-ffffff"' in text_a
    assert 'plan_id: "pln-fixture-plan-ambiguous-b-111111"' in text_b
    assert "pln-fixture-plan-ambiguous-a-ffffff" != "pln-fixture-plan-ambiguous-b-111111"


def test_fixture_builds_archive_specs_variants(tmp_path: Path) -> None:
    paths = build_spec_backlink_corpus(tmp_path)

    assert paths["archived_full"].parent == paths["archive_specs_dir"]
    assert paths["archive_specs_dir"].name == "2026-08"

    full_text = paths["archived_full"].read_text(encoding="utf-8")
    assert "plan_id:" in full_text
    assert "deliverable_id:" in full_text

    no_ids_text = paths["archived_no_ids"].read_text(encoding="utf-8")
    assert "plan_id:" not in no_ids_text
    assert "deliverable_id:" not in no_ids_text


def test_fixture_builds_sizings_as_plain_yaml(tmp_path: Path) -> None:
    paths = build_spec_backlink_corpus(tmp_path)

    with_dlv_text = paths["sizing_with_dlv"].read_text(encoding="utf-8")
    assert not with_dlv_text.startswith("---")
    assert "schema: sizing-object" in with_dlv_text
    assert 'deliverable_id: "dlv-fixture-sizing-with-dlv-444444"' in with_dlv_text

    without_dlv_text = paths["sizing_without_dlv"].read_text(encoding="utf-8")
    assert not without_dlv_text.startswith("---")
    assert "deliverable_id:" not in without_dlv_text


def test_fixture_builds_citing_file_with_backlink_and_decoy(tmp_path: Path) -> None:
    paths = build_spec_backlink_corpus(tmp_path)

    text = paths["citing_file"].read_text(encoding="utf-8")
    lines = text.splitlines()

    backlink_lines = [ln for ln in lines if "Spec backlink:" in ln]
    assert len(backlink_lines) == 1
    assert "docs/plans/2026-08-13-fixture-plan-full.md" in backlink_lines[0]
    assert "§ AC1" in backlink_lines[0]

    decoy_lines = [
        ln
        for ln in lines
        if "docs/plans/2026-08-13-fixture-plan-full.md" in ln and "Spec backlink:" not in ln
    ]
    assert len(decoy_lines) == 1


def test_pytest_fixture_wrapper_works(spec_backlink_corpus: dict[str, Path]) -> None:
    assert spec_backlink_corpus["docs_plans"].is_dir()
    assert spec_backlink_corpus["plan_full"].exists()
    assert spec_backlink_corpus["sizing_with_dlv"].exists()

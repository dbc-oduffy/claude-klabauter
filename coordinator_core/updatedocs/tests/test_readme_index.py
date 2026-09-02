"""
Tests for coordinator_core.updatedocs.readme_index.

Spec backlink: pln-bucket-2-extraction-four-deter-e121fa (chunk C1)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.updatedocs._common import UpdatedocsTargetMissing
from coordinator_core.updatedocs.readme_index import compute_readme_index_drift


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_raises_when_docs_dir_absent(tmp_path):
    with pytest.raises(UpdatedocsTargetMissing) as excinfo:
        compute_readme_index_drift(tmp_path)
    assert excinfo.value.missing_path == tmp_path / "docs"


def test_raises_when_readme_absent(tmp_path):
    (tmp_path / "docs").mkdir()
    with pytest.raises(UpdatedocsTargetMissing) as excinfo:
        compute_readme_index_drift(tmp_path)
    assert excinfo.value.missing_path == tmp_path / "docs" / "README.md"


def _make_fixture(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    _write(docs / "wiki" / "linked-one.md", "# Linked One\n")
    _write(docs / "wiki" / "missing-one.md", "# Missing One\n")
    _write(docs / "wiki" / "dead-target.md", "# Dead Target\n")
    _write(docs / "plans" / "a-plan.md", "# A Plan\n")

    readme = docs / "README.md"
    _write(
        readme,
        "\n".join(
            [
                "# Index",
                "",
                "## Start here",
                "",
                "1. [`../README.md`](../README.md) — repo root, not docs/.",
                "",
                "## Wikis and Guides",
                "",
                "| Wiki entry | Topic |",
                "|---|---|",
                "| [`linked-one.md`](wiki/linked-one.md) | one |",
                "| [`no-longer-there.md`](wiki/no-longer-there.md) | dead |",
                "",
                "## Plans",
                "",
                "No plans linked here yet.",
                "",
            ]
        ),
    )
    return tmp_path


def test_missing_and_dead_computed_per_section(tmp_path):
    root = _make_fixture(tmp_path)
    drift = compute_readme_index_drift(root)
    by_section = {s.section: s for s in drift.sections}

    wiki = by_section["Wikis and Guides"]
    assert wiki.linked == 2
    assert wiki.on_disk == 3
    assert wiki.missing == ["dead-target.md", "missing-one.md"]
    assert wiki.dead == ["no-longer-there.md"]

    plans = by_section["Plans"]
    assert plans.linked == 0
    assert plans.on_disk == 1
    assert plans.missing == ["a-plan.md"]
    assert plans.dead == []


def test_repo_root_readme_link_is_not_counted_as_docs_target(tmp_path):
    root = _make_fixture(tmp_path)
    drift = compute_readme_index_drift(root)
    for section in drift.sections:
        assert "README.md" not in section.dead


def test_all_configured_sections_present(tmp_path):
    root = _make_fixture(tmp_path)
    drift = compute_readme_index_drift(root)
    names = {s.section for s in drift.sections}
    assert names == {
        "Wikis and Guides",
        "Plans",
        "Research",
        "Problems",
        "Decisions",
        "Reference Documentation",
    }


def test_reference_documentation_uses_top_level_docs_only(tmp_path):
    docs = tmp_path / "docs"
    _write(docs / "exec-summary.md", "# Exec Summary\n")
    _write(docs / "wiki" / "not-top-level.md", "# Not Top Level\n")
    _write(
        docs / "README.md",
        "\n".join(
            [
                "## Reference Documentation",
                "",
                "| Doc | Purpose |",
                "|-----|---------|",
                "| [exec-summary.md](exec-summary.md) | summary |",
                "",
            ]
        ),
    )
    drift = compute_readme_index_drift(tmp_path)
    ref = next(s for s in drift.sections if s.section == "Reference Documentation")
    # README.md itself is a top-level docs/*.md file too, and is not linked
    # from its own Reference Documentation section, so it is `missing`.
    assert "not-top-level.md" not in ref.missing
    assert "exec-summary.md" not in ref.missing
    assert "README.md" in ref.missing


from coordinator_core.updatedocs.readme_index import _extract_title  # noqa: E402


def test_title_extraction_frontmatter(tmp_path):
    path = tmp_path / "with-frontmatter.md"
    _write(path, '---\ntitle: "Custom Title"\n---\n\n# A Heading\n')
    assert _extract_title(path) == "Custom Title"


def test_title_extraction_first_heading(tmp_path):
    path = tmp_path / "with-heading.md"
    _write(path, "Some intro text.\n\n# The Real Heading\n\nBody.\n")
    assert _extract_title(path) == "The Real Heading"


def test_title_extraction_falls_through_to_stem_when_neither_present(tmp_path):
    path = tmp_path / "bare-file-no-title.md"
    _write(path, "Just some prose with no frontmatter and no heading at all.\n")
    title = _extract_title(path)
    assert title == "bare-file-no-title"


def test_title_extraction_reads_a_bounded_head_not_the_whole_file(tmp_path):
    """`_extract_title` now shares `_common.read_head`'s bound (8192 bytes,
    grown once to 65536 if a frontmatter close delimiter hasn't appeared) --
    finding 5's `_common.py` lift replaced the module's own 800-byte reader.
    A heading placed well past the growth ceiling must still fall through to
    the filename-stem fallback, proving the read stays bounded rather than
    reading the whole file."""
    path = tmp_path / "huge.md"
    _write(path, "x" * 70_000 + "\n# Late Heading\n")
    assert _extract_title(path) == "huge"

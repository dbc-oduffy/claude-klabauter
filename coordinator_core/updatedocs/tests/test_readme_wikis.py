from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.updatedocs._common import UpdatedocsTargetMissing
from coordinator_core.updatedocs.readme_index import compute_readme_index_drift
from coordinator_core.updatedocs.readme_wikis import (
    render_wikis_table,
    splice_wikis_table,
    sync_readme_wikis,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_topic_comes_from_frontmatter_title_then_heading_then_stem(tmp_path):
    wiki = tmp_path / "wiki"
    _write(wiki / "a-fm.md", "---\ntitle: \"FM Title\"\n---\n# Ignored\n")
    _write(wiki / "b-head.md", "intro\n# Heading Title\n")
    _write(wiki / "c-stem.md", "no heading at all\n")

    table = render_wikis_table(wiki)

    assert table[:2] == ["| Wiki entry | Topic |", "|---|---|"]
    assert table[2] == "| [`a-fm.md`](wiki/a-fm.md) | FM Title |"
    assert table[3] == "| [`b-head.md`](wiki/b-head.md) | Heading Title |"
    assert table[4] == "| [`c-stem.md`](wiki/c-stem.md) | c-stem |"


def test_pipe_in_title_is_escaped(tmp_path):
    _write(tmp_path / "wiki" / "p.md", "# A | B\n")
    assert render_wikis_table(tmp_path / "wiki")[2].endswith("| A \\| B |")


def _make_repo(tmp_path: Path) -> Path:
    _write(tmp_path / "docs" / "wiki" / "new-one.md", "# New One\n")
    _write(tmp_path / "docs" / "wiki" / "kept.md", "# Kept\n")
    _write(
        tmp_path / "docs" / "README.md",
        "# Docs\n\n## Wikis and Guides\n\nIntro prose.\n\n| Wiki entry | Topic |\n|---|---|\n"
        "| [`kept.md`](wiki/kept.md) | hand written |\n"
        "| [`dead.md`](wiki/dead.md) | gone |\n\nTrailing prose.\n\n"
        "## Plans\n\n| [`x.md`](wiki/x.md) | untouched |\n",
    )
    return tmp_path


def test_sync_adds_missing_drops_dead_and_leaves_the_rest(tmp_path):
    repo = _make_repo(tmp_path)

    result = sync_readme_wikis(repo)
    text = (repo / "docs" / "README.md").read_text(encoding="utf-8")

    assert (result.changed, result.rows, result.added, result.dropped) == (True, 2, 1, 1)
    assert "new-one.md`](wiki/new-one.md) | New One |" in text
    assert "kept.md`](wiki/kept.md) | Kept |" in text
    assert "dead.md" not in text
    assert "Intro prose.\n" in text and "\nTrailing prose.\n" in text
    assert text.endswith("## Plans\n\n| [`x.md`](wiki/x.md) | untouched |\n")


def test_sync_is_idempotent_and_clears_the_drift_gate(tmp_path):
    repo = _make_repo(tmp_path)
    sync_readme_wikis(repo)
    first = (repo / "docs" / "README.md").read_bytes()

    result = sync_readme_wikis(repo)

    assert result.changed is False
    assert (repo / "docs" / "README.md").read_bytes() == first
    wikis = next(s for s in compute_readme_index_drift(repo).sections if s.section == "Wikis and Guides")
    assert wikis.missing == [] and wikis.dead == []


def test_section_without_a_table_gets_one_after_the_heading():
    out = splice_wikis_table("## Wikis and Guides\n\nprose\n\n## Plans\n", ["| h |", "|---|"])
    assert out == "## Wikis and Guides\n\n| h |\n|---|\n\nprose\n\n## Plans\n"


def test_missing_heading_returns_none_and_sync_refuses_without_writing(tmp_path):
    assert splice_wikis_table("## Plans\n", ["| h |"]) is None
    _write(tmp_path / "docs" / "wiki" / "a.md", "# A\n")
    _write(tmp_path / "docs" / "README.md", "## Plans\n")
    with pytest.raises(UpdatedocsTargetMissing):
        sync_readme_wikis(tmp_path)
    assert (tmp_path / "docs" / "README.md").read_text(encoding="utf-8") == "## Plans\n"


def test_missing_wiki_dir_refuses(tmp_path):
    _write(tmp_path / "docs" / "README.md", "## Wikis and Guides\n")
    with pytest.raises(UpdatedocsTargetMissing) as excinfo:
        sync_readme_wikis(tmp_path)
    assert excinfo.value.missing_path == tmp_path / "docs" / "wiki"

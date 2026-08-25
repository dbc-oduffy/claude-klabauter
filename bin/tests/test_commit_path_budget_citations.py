"""
bin.tests.test_commit_path_budget_citations — Discriminator coverage for
bin/commit-path-budget-citations.py.

Covers the enumerator's four inclusion/exclusion discriminators (not its output count — the
live corpus moves and a pinned total would be stale within a day):

  1. a file matching a bar figure but NOT mentioning "commit" is excluded
  2. a file matching both, but whose bar figure appears on a line (or table block) with no
     budget vocabulary nearby, is excluded
  3. a terminal `status:` excludes; a non-terminal one includes
  4. an ABSENT `status:` field is included (absent is not terminal, and is a distinct case
     from the non-terminal case above)

Uses a tmp_path fixture with synthetic files only — never the live corpus, per the chunk brief:
a test that reads state/ and docs/ re-fails whenever an unrelated session writes an artifact.

Spec backlink: state/dispatch-briefs/2026-08-25-what-a-commit-path-budget-measures/C3.md
Negative-spec: does not assert an output count or a specific live-corpus path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_SCRIPT = _REPO_ROOT / "bin" / "commit-path-budget-citations.py"


def _load_module():
    key = "commit_path_budget_citations_unit"
    spec = importlib.util.spec_from_file_location(key, _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_MOD = _load_module()


def _make_corpus(tmp_path: Path) -> Path:
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    return tmp_path


def test_figure_without_commit_mention_excluded(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    path = root / "docs" / "plans" / "no-commit.md"
    path.write_text(
        "---\nstatus: draft\n---\n\nThe budget bar is 500ms end-to-end for this op.\n",
        encoding="utf-8",
    )
    results = _MOD.sweep(root)
    assert not any(r[1] == "docs/plans/no-commit.md" for r in results)


def test_figure_and_commit_but_no_nearby_budget_vocab_excluded(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    path = root / "docs" / "plans" / "unrelated-figure.md"
    path.write_text(
        "---\nstatus: draft\n---\n\n"
        "This commit touches many files.\n"
        "\n"
        "\n"
        "\n"
        "The queue depth reached 500ms once under heavy load, nothing to do with commits.\n",
        encoding="utf-8",
    )
    results = _MOD.sweep(root)
    assert not any(r[1] == "docs/plans/unrelated-figure.md" for r in results)


def test_figure_and_commit_with_same_line_budget_vocab_included(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    path = root / "docs" / "plans" / "same-line.md"
    path.write_text(
        "---\nstatus: draft\n---\n\n"
        "This commit-path op must hit the 500ms budget bar end-to-end.\n",
        encoding="utf-8",
    )
    results = _MOD.sweep(root)
    matches = [r for r in results if r[1] == "docs/plans/same-line.md"]
    assert len(matches) == 1
    assert matches[0][2] >= 1


def test_figure_and_commit_with_table_block_budget_vocab_included(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    path = root / "docs" / "plans" / "table-block.md"
    path.write_text(
        "---\nstatus: draft\n---\n\n"
        "This commit path is measured below.\n\n"
        "| term | process time |\n"
        "| --- | --- |\n"
        "| budget figure | 400ms |\n",
        encoding="utf-8",
    )
    results = _MOD.sweep(root)
    assert any(r[1] == "docs/plans/table-block.md" for r in results)


def test_terminal_status_excludes(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    path = root / "docs" / "plans" / "landed.md"
    path.write_text(
        "---\nstatus: landed\n---\n\n"
        "This commit-path op must hit the 500ms budget bar.\n",
        encoding="utf-8",
    )
    results = _MOD.sweep(root)
    assert not any(r[1] == "docs/plans/landed.md" for r in results)


def test_nonterminal_status_includes(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    path = root / "docs" / "plans" / "proposed.md"
    path.write_text(
        "---\nstatus: proposed\n---\n\n"
        "This commit-path op must hit the 500ms budget bar.\n",
        encoding="utf-8",
    )
    results = _MOD.sweep(root)
    matches = [r for r in results if r[1] == "docs/plans/proposed.md"]
    assert len(matches) == 1
    assert matches[0][0] == "proposed"


def test_absent_status_includes_and_is_not_the_nonterminal_case(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    path = root / "state" / "handoffs" / "no-status-field.md"
    path.write_text(
        "This commit-path op must hit the 500ms budget bar.\n",
        encoding="utf-8",
    )
    results = _MOD.sweep(root)
    matches = [r for r in results if r[1] == "state/handoffs/no-status-field.md"]
    assert len(matches) == 1
    assert matches[0][0] == "(absent)"


def test_bare_yaml_terminal_status_excludes(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    path = root / "state" / "handoffs" / "bare-terminal.yaml"
    path.write_text(
        "status: closed\n"
        "This commit-path op must hit the 500ms budget bar.\n",
        encoding="utf-8",
    )
    results = _MOD.sweep(root)
    assert not any(r[1] == "state/handoffs/bare-terminal.yaml" for r in results)


def test_bare_yaml_nonterminal_status_includes(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    path = root / "state" / "handoffs" / "bare-nonterminal.yaml"
    path.write_text(
        "status: open\n"
        "This commit-path op must hit the 500ms budget bar.\n",
        encoding="utf-8",
    )
    results = _MOD.sweep(root)
    matches = [r for r in results if r[1] == "state/handoffs/bare-nonterminal.yaml"]
    assert len(matches) == 1
    assert matches[0][0] == "open"


def test_bare_yaml_nested_status_does_not_shadow_top_level_status(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    path = root / "state" / "handoffs" / "nested-status.yaml"
    path.write_text(
        "history:\n"
        "  - status: closed\n"
        "status: open\n"
        "This commit-path op must hit the 500ms budget bar.\n",
        encoding="utf-8",
    )
    results = _MOD.sweep(root)
    matches = [r for r in results if r[1] == "state/handoffs/nested-status.yaml"]
    assert len(matches) == 1
    assert matches[0][0] == "open"


def test_table_block_with_adjacent_caption_included(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    path = root / "docs" / "plans" / "table-caption.md"
    path.write_text(
        "---\nstatus: draft\n---\n\n"
        "This commit path is measured below.\n\n"
        "| figure |\n"
        "| --- |\n"
        "| 500ms |\n"
        "brightline noted right after\n",
        encoding="utf-8",
    )
    results = _MOD.sweep(root)
    assert any(r[1] == "docs/plans/table-caption.md" for r in results)


def test_window_boundary_two_lines_away_includes(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    path = root / "docs" / "plans" / "window-two.md"
    path.write_text(
        "---\nstatus: draft\n---\n\n"
        "This commit touches the budget.\n"
        "\n"
        "The figure is 500ms here.\n",
        encoding="utf-8",
    )
    results = _MOD.sweep(root)
    matches = [r for r in results if r[1] == "docs/plans/window-two.md"]
    assert len(matches) == 1


def test_window_boundary_three_lines_away_excludes(tmp_path: Path) -> None:
    root = _make_corpus(tmp_path)
    path = root / "docs" / "plans" / "window-three.md"
    path.write_text(
        "---\nstatus: draft\n---\n\n"
        "This commit touches the budget.\n"
        "\n"
        "\n"
        "\n"
        "The figure is 500ms here.\n",
        encoding="utf-8",
    )
    results = _MOD.sweep(root)
    assert not any(r[1] == "docs/plans/window-three.md" for r in results)


def test_extract_status_returns_none_when_field_missing() -> None:
    assert _MOD.extract_status("no frontmatter here at all\n") is None
    assert _MOD.extract_status("---\ntitle: x\n---\nbody\n") is None


def test_is_terminal_status_absent_is_not_terminal() -> None:
    assert _MOD.is_terminal_status(None) is False
    assert _MOD.is_terminal_status("proposed") is False
    assert _MOD.is_terminal_status("landed") is True
    assert _MOD.is_terminal_status("superseded") is True

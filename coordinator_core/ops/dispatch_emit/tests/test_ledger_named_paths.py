"""Pins named_paths and gated_paths of ledger_remaining_paths."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import gated_paths, named_paths


def _repo(tmp_path):
    (tmp_path / "docs/plans").mkdir(parents=True)
    (tmp_path / "docs/plans/x.md").write_text("x")
    (tmp_path / "CLAUDE.md").write_text("x")
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch/a.py").write_text("x")
    return tmp_path


def test_admits_existing_files(tmp_path):
    root = _repo(tmp_path)
    assert named_paths(["edit docs/plans/x.md and CLAUDE.md"], root) == ["docs/plans/x.md", "CLAUDE.md"]


def test_dedupes_across_texts(tmp_path):
    root = _repo(tmp_path)
    assert named_paths(["CLAUDE.md", "CLAUDE.md docs/plans/x.md"], root) == ["CLAUDE.md", "docs/plans/x.md"]


def test_drops_unnameable_tokens(tmp_path):
    root = _repo(tmp_path)
    texts = ["nodir/absent.md scratch/a.py ../x.md /etc/passwd.txt README word"]
    assert named_paths(texts, root) == []


def test_strips_line_suffix(tmp_path):
    root = _repo(tmp_path)
    assert named_paths(["docs/plans/x.md:12"], root) == ["docs/plans/x.md"]


def test_gated_paths_selects_claude_md_class():
    assert gated_paths(["CLAUDE.md", "docs/plans/x.md", "coordinator/CLAUDE.md", "docs/CLAUDE.md.bak"]) == [
        "CLAUDE.md",
        "coordinator/CLAUDE.md",
    ]

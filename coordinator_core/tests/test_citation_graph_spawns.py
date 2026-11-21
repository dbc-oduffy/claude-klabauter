"""coordinator_core/tests/test_citation_graph.py -- tests for
`coordinator_core.citation_graph`.

Ported from coordinator-content-repo `coordinator/tests/test_citation_graph.py`
(`docs/plans/2026-08-30-citation-integrity-tier-1.md`, chunk C1) per
`docs/plans/2026-09-18-doe-holds-no-scripts.md` chunk W2-C3.

Covers extraction (fenced-code skip, frontmatter `spec_backlink:` exemption,
structural-comment skip, directory-convention placeholder skip, markdown-link
label dedup) and the four resolution classes -- bare-basename (live /
ambiguous / cross_surface / rot), pathed (repo-root-relative, NOT joined
against the citing file's directory -- the inherited-bug regression this
module's Anti-scope names explicitly), and markdown-link (resolved relative
to the citing file). Also covers the seeded sampler's determinism and a
smoke test against claude-klabauter's own real `docs/wiki/` corpus, which lives in
`test_citation_graph_claude_klabauter_corpus.py`, so a wiring regression (e.g.
`TRACKED_EXTENSIONS` widened without updating resolution) is caught even
though this module's detailed behaviour is otherwise tested on synthetic
fixtures.
"""

from __future__ import annotations


from coordinator_core import citation_graph as cg

import pytest
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


# --- tombstone resolution proof and bare_cite_scope narrowing ---------------


def _git(root, *args):
    import subprocess

    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def test_load_repo_index_in_git_checkout_skips_ignored_keeps_untracked(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("scratch/\n", encoding="utf-8")
    (tmp_path / "scratch").mkdir()
    (tmp_path / "scratch" / "ignored.md").write_text("x", encoding="utf-8")
    (tmp_path / "tracked.md").write_text("x", encoding="utf-8")
    (tmp_path / "untracked.md").write_text("x", encoding="utf-8")
    _git(tmp_path, "add", "tracked.md", ".gitignore")
    (tmp_path / "gone.md").write_text("x", encoding="utf-8")
    _git(tmp_path, "add", "gone.md")
    (tmp_path / "gone.md").unlink()

    index = cg.load_repo_index(tmp_path)
    assert "tracked.md" in index and "untracked.md" in index
    assert "ignored.md" not in index
    assert "gone.md" not in index

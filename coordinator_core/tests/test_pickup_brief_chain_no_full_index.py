"""pickup_brief.chain_ancestor_count resolves a single baton's chain without
building the corpus-wide handoff_id index."""
import time
from pathlib import Path

import pytest

from coordinator_core import dag, pickup_brief

pytestmark = pytest.mark.complete

_CORPUS = 3000


def _write(root: Path, rel: str, lines: list) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + "\n".join(lines) + "\n---\n\nbody\n", encoding="utf-8")
    return p


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "repo"
    for i in range(_CORPUS):
        _write(root, f"archive/handoffs/2026-01/n{i}.md",
               [f"handoff_id: hnd-n{i}", "predecessor: none"])
    c = _write(root, "state/handoffs/c.md",
               ["handoff_id: hnd-c", "predecessor: none"])
    b = _write(root, "state/handoffs/b.md",
               ["handoff_id: hnd-b", "predecessor: state/handoffs/c.md",
                "predecessor_id: hnd-c"])
    a = _write(root, "state/handoffs/a.md",
               ["handoff_id: hnd-a", "predecessor: state/handoffs/b.md",
                "predecessor_id: hnd-b"])
    return root, a, c


@pytest.fixture
def no_index(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("corpus-wide handoff_id index touched")
    monkeypatch.setattr(dag, "build_handoff_id_index", boom)
    monkeypatch.setattr(dag, "_raw_scan_handoff_id", boom)


def test_chain_count_without_full_index(corpus, no_index):
    root, a, _c = corpus
    t0 = time.process_time()
    n = pickup_brief.chain_ancestor_count(a, root, "handoff")
    assert n == 2
    assert time.process_time() - t0 < 0.5


def test_id_only_link_reads_missing_link_not_index(corpus, no_index):
    root, _a, _c = corpus
    d = _write(root, "state/handoffs/d.md",
               ["handoff_id: hnd-d", "predecessor: none", "predecessor_id: hnd-c"])
    assert pickup_brief.chain_ancestor_count(d, root, "handoff") == 0
    walk = dag.walk_forward(str(d), handoff_dir=str(root / "state/handoffs"),
                            repo_root=str(root), include_history_tier=False,
                            follow_id_aliases=False)
    assert walk["terminatedEarly"] == "missing-link"


def test_default_walk_still_follows_id_alias(corpus):
    root, _a, _c = corpus
    d = _write(root, "state/handoffs/d.md",
               ["handoff_id: hnd-d", "predecessor: none", "predecessor_id: hnd-c"])
    walk = dag.walk_forward(str(d), handoff_dir=str(root / "state/handoffs"),
                            repo_root=str(root), include_history_tier=False)
    assert len(walk["orderedPaths"]) == 2

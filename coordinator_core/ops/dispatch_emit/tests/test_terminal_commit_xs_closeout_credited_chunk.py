"""An xs-arm closeout chunk whose changed declared paths are all evidence/state is regraded as credited."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence, pytest.mark.xs_gate_live]

PLAN = "scratch/warp/r/x.spine.md"
EVIDENCE = "scratch/warp/r/x.spine.evidence.yaml"
BATON = "state/handoffs/2026-10/b.md"
INDEX = "docs/plans/INDEX.evidence.yaml"


def _git(root, *a):
    subprocess.run(["git", *a], cwd=str(root), check=True, capture_output=True, **no_console_creationflags())


@pytest.fixture
def root(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "seed.txt").write_text("s\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    (tmp_path / "rep").mkdir()
    (tmp_path / "rep" / "X1.md").write_text("status: DONE\n<exit-status>DONE</exit-status>\n", encoding="utf-8")
    return tmp_path


def _write(root, rel, text="a: 1\n"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _regrade(root, *paths, report="rep/X1.md"):
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="t", paths=tuple(paths), report=report),), plan_path=PLAN
    )
    return tc._xs_closeout_regrades(root, request, {"X1"}, None, {})


def test_evidence_baton_and_index_writes_are_credited(root):
    for rel in (EVIDENCE, BATON, INDEX):
        _write(root, rel)
    assert set(_regrade(root, EVIDENCE, BATON, INDEX)) == {"X1"}


def test_a_product_path_keeps_the_chunk_stranded(root):
    _write(root, EVIDENCE)
    _write(root, "coordinator_core/mod.py", "x = 1\n")
    assert _regrade(root, EVIDENCE, "coordinator_core/mod.py") == {}


def test_a_state_corpus_product_record_keeps_the_chunk_stranded(root):
    _write(root, "state/bug-backlog/x.yaml")
    assert _regrade(root, "state/bug-backlog/x.yaml") == {}


def test_no_changed_declared_path_credits_nothing(root):
    assert _regrade(root, EVIDENCE) == {}


def test_a_non_delivered_report_is_not_credited(root):
    _write(root, EVIDENCE)
    (root / "rep" / "X1.md").write_text("status: BLOCKED\n<exit-status>BLOCKED</exit-status>\n", encoding="utf-8")
    assert _regrade(root, EVIDENCE) == {}

"""A declared write into a state corpus, untracked on disk at the halt, counts as changed product for the since-halt gate."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import UNCHANGED_SINCE_HALT_REASON
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

MET = {"items": [{"id": "R1", "status": "met", "wired": True}]}
STRANDED = "state/bug-backlog/2026-10-21-stranded.yaml"


def _git(root, *a):
    return subprocess.run(
        ["git", *a], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "seed.txt").write_text("s\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "halt")
    return tmp_path, _git(tmp_path, "rev-parse", "HEAD")


def _manifest(root, *writes):
    path = root / "manifest.json"
    path.write_text(json.dumps({"rows": [{"id": "C1", "writes": list(writes)}]}), encoding="utf-8")
    return path


def _failures(root, halt, manifest):
    resume = {"run_id": "old", "halt_sha": halt, "blocking": ["R1"], "unlanded": ["C1"]}
    return tc._unchanged_since_halt_failures(root, resume, MET, manifest)


def test_untracked_state_corpus_write_declared_by_the_chunk_counts_as_product(repo):
    root, halt = repo
    (root / "state" / "bug-backlog").mkdir(parents=True)
    (root / STRANDED).write_text("a: 1\n", encoding="utf-8")
    assert _failures(root, halt, _manifest(root, STRANDED)) == []


def test_declared_write_absent_from_disk_is_still_refused(repo):
    root, halt = repo
    assert _failures(root, halt, _manifest(root, STRANDED)) == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


def test_undeclared_untracked_state_file_does_not_count(repo):
    root, halt = repo
    (root / "state" / "bug-backlog").mkdir(parents=True)
    (root / STRANDED).write_text("a: 1\n", encoding="utf-8")
    manifest = _manifest(root, "state/bug-backlog/other.yaml")
    assert _failures(root, halt, manifest) == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


@pytest.mark.parametrize("rel", ["state/sizings/x.yaml", "state/warp/r/x.json", "state/loose.yaml"])
def test_run_process_state_still_does_not_count(repo, rel):
    root, halt = repo
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text("a: 1\n", encoding="utf-8")
    assert _failures(root, halt, _manifest(root, rel)) == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]

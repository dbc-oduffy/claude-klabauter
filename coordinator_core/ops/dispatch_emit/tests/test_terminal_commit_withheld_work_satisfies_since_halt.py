"""Product a halt withheld as halt-record-only stays dirty with the halt state's own bytes, so
`git diff <halt_sha>` reads it unchanged; a met refire must still count it as work to land."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, UNCHANGED_SINCE_HALT_REASON
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

MET = {"items": [{"id": "R1", "status": "met", "wired": True}]}
RUN = "ask-20261020T000000-aaaaaa"


def _git(root, *a):
    out = subprocess.run(
        ["git", *a], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    )
    return out.stdout.strip()


def _commit(root, text, msg):
    (root / "prod.py").write_text(text, encoding="utf-8", newline="\n")
    _git(root, "add", "prod.py")
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    """HEAD reverted prod.py after the halt commit; the worktree holds the halt commit's bytes."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    _commit(tmp_path, "x = 1\n", "base")
    halt = _commit(tmp_path, "x = 2\n", "halt")
    _commit(tmp_path, "x = 1\n", "revert")
    (tmp_path / "prod.py").write_text("x = 2\n", encoding="utf-8", newline="\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"rows": [{"id": "C1", "writes": ["prod.py"]}]}), encoding="utf-8")
    return tmp_path, manifest, halt


def _resume(halt, **extra):
    return {"run_id": RUN, "halt_sha": halt, "blocking": ["R1"], "unlanded": ["C1"], **extra}


def _record_halt(root, withheld):
    record_dir = root / RECORD_DIR_ROOT / RUN
    record_dir.mkdir(parents=True)
    halted_run.write_halt_marker(record_dir, halted="ledger-check", sizing_rel=None)
    halted_run.record_withheld(record_dir, withheld)


def test_dirty_path_the_halt_withheld_counts_as_work_to_land(repo):
    root, manifest, halt = repo
    _record_halt(root, {"prod.py": "halt-record-only"})
    assert tc._unchanged_since_halt_failures(root, _resume(halt), MET, manifest) == []


def test_withheld_path_carried_on_the_resume_counts_too(repo):
    root, manifest, halt = repo
    resume = _resume(halt, run_artifacts={"withheld": {"prod.py": "halt-record-only"}})
    assert tc._unchanged_since_halt_failures(root, resume, MET, manifest) == []


def test_same_dirty_path_without_a_withheld_record_still_refuses(repo):
    root, manifest, halt = repo
    got = tc._unchanged_since_halt_failures(root, _resume(halt), MET, manifest)
    assert got == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


def test_withheld_path_that_is_clean_still_refuses(repo):
    root, manifest, halt = repo
    _git(root, "reset", "-q", "--hard", halt)
    _record_halt(root, {"prod.py": "halt-record-only"})
    got = tc._unchanged_since_halt_failures(root, _resume(halt), MET, manifest)
    assert got == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]

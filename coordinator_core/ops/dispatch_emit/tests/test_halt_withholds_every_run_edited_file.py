"""A halt records every file the run edited as withheld, touch record or not, declared or not."""

from __future__ import annotations

import os
import time

import pytest

from coordinator_core.ops import warp_run_deliverable
from coordinator_core.ops.dispatch_emit import halted_record_commit, stage_dirt_snapshot
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    LEDGER,
    PASSING,
    RUN,
    _call,
    _git,
    _write_record,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

TOUCHED = "pkg/__init__.py"  # tracked, in the session touch record
UNRECORDED_TRACKED = "pkg/b.py"  # tracked, no touch record
NEW_FILE = "pkg/new_mod.py"  # untracked-new, no touch record
NEW_IN_NEW_DIR = "newpkg/sub/x.py"  # untracked-new inside a brand-new directory
DECLARED = "pkg/declared.py"  # untracked-new, declared by a done chunk
EDITED = [TOUCHED, UNRECORDED_TRACKED, NEW_FILE, NEW_IN_NEW_DIR, DECLARED]
PRE_STAGE_DIRT = "old.py"
SECOND = 10**9


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def edited_run(repo, monkeypatch):
    """Stage marker laid down, then five files edited after it; the touch record names only one."""
    for rel in (TOUCHED, UNRECORDED_TRACKED, PRE_STAGE_DIRT):
        _write(repo, rel, "v = 0\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "seed pkg"], repo)

    _write(repo, PRE_STAGE_DIRT, "v = 'peer edit before stage'\n")
    old = time.time_ns() - 100 * SECOND
    os.utime(repo / PRE_STAGE_DIRT, ns=(old, old))

    record = _write_record(repo, RUN)
    assert stage_dirt_snapshot.write_snapshot(repo, record) is not None
    marker = stage_dirt_snapshot.read_snapshot(record)
    assert marker is not None and (repo / PRE_STAGE_DIRT).stat().st_mtime_ns < marker

    _write(repo, "reports/C1.md", "DONE: C1\n")
    chunk = ChunkCommit(id="C1", title="C1", paths=(DECLARED,), report="reports/C1.md")
    marker_line = render_marker(CommitRequest(chunks=(chunk,), session_id=None, repo_root=None, plan_path=None))
    _write(repo, "run.mjs", f"{marker_line}\n")

    _write(repo, TOUCHED, "v = 1000\n")
    _write(repo, UNRECORDED_TRACKED, "v = 2000\n")
    _write(repo, NEW_FILE, "def f():\n    pass\n")
    _write(repo, NEW_IN_NEW_DIR, "def g():\n    pass\n")
    _write(repo, DECLARED, "d = 1\n")
    for rel in EDITED:
        assert (repo / rel).stat().st_mtime_ns >= marker

    monkeypatch.setattr(halted_record_commit, "_run_written", lambda *_a, **_k: [TOUCHED])
    return repo


def test_a_halt_withholds_all_five_edited_files_and_not_pre_stage_dirt(edited_run):
    out = _call(edited_run, LEDGER, **PASSING)

    withheld = out["run_artifacts"]["withheld"]
    missing = [p for p in EDITED if p not in withheld]
    assert not missing, f"edited but not withheld: {missing}; withheld={sorted(withheld)}"
    held = warp_run_deliverable.halted_withheld_paths(edited_run, RUN)
    assert set(EDITED) <= held
    assert PRE_STAGE_DIRT not in withheld and PRE_STAGE_DIRT not in held


def test_the_edited_files_stay_uncommitted_in_the_tree(edited_run):
    _call(edited_run, LEDGER, **PASSING)

    status = _git(["status", "--porcelain", "-uall"], edited_run)
    for rel in [*EDITED, PRE_STAGE_DIRT]:
        assert rel in status

"""A withheld row's run-write-set path that is unchanged since the halt commit but differs from HEAD
in the worktree is work to land; one equal to HEAD, or a run with nothing withheld, still refuses."""

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
RUN = "ask-20261026T000000-bbbbbb"
REFUSED = [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


def _git(root, *a):
    out = subprocess.run(
        ["git", *a], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """The manifest declares prod.py (clean); the run's record names other.py, outside the manifest."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "prod.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    (tmp_path / "other.py").write_text("y = 1\n", encoding="utf-8", newline="\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "halt")
    halt = _git(tmp_path, "rev-parse", "HEAD")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"rows": [{"id": "C1", "writes": ["prod.py"]}]}), encoding="utf-8")
    return tmp_path, manifest, halt


def _resume(halt):
    return {"run_id": RUN, "halt_sha": halt, "blocking": ["R1"], "unlanded": ["C1"]}


def _record(root, withheld):
    record_dir = root / RECORD_DIR_ROOT / RUN
    record_dir.mkdir(parents=True)
    (record_dir / "manifest.json").write_text(
        json.dumps({"review_declared_paths": ["other.py"]}), encoding="utf-8"
    )
    halted_run.write_halt_marker(record_dir, halted="ledger-check", sizing_rel=None)
    halted_run.record_withheld(record_dir, withheld)


def test_withheld_write_set_path_differing_from_head_is_not_refused(repo):
    root, manifest, halt = repo
    _record(root, {"other.py": "halt-record-only"})
    (root / "other.py").write_text("y = 2\n", encoding="utf-8", newline="\n")
    assert tc._unchanged_since_halt_failures(root, _resume(halt), MET, manifest) == []


def test_withheld_write_set_path_equal_to_head_still_refuses(repo):
    root, manifest, halt = repo
    _record(root, {"other.py": "halt-record-only"})
    assert tc._unchanged_since_halt_failures(root, _resume(halt), MET, manifest) == REFUSED


def test_dirty_path_with_nothing_withheld_still_refuses_and_spawns_no_head_leg(repo, monkeypatch):
    root, manifest, halt = repo
    (root / "other.py").write_text("y = 2\n", encoding="utf-8", newline="\n")
    seen = []
    real = tc.run_git
    monkeypatch.setattr(tc, "run_git", lambda args, **k: (seen.append(args), real(args, **k))[1])
    assert tc._unchanged_since_halt_failures(root, _resume(halt), MET, manifest) == REFUSED
    assert not any("HEAD" in a for a in seen)

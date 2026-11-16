"""A wave checkpoint names the owner and lines of a new file it withholds: a halted non-co-declaring
holder by run id, an unattributable owner as `unknown`, each with the foreign line range. The own tracked
path still lands. Provenance findings are supplied at the `hunk_provenance.unproduced_hunks` seam."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import checkpoint_hunks
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit import (
    halted_hunk_attribution, hunk_provenance, peer_hunk_gate, run_hunk_claims, run_write_prints,
)
from coordinator_core.ops.dispatch_emit.landing_targeted_tests import Finding
from coordinator_core.ops.dispatch_emit.run_hunk_claims import HunkSplit
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "ckh-self"
PLAN = "state/mise-inventory/run.spine.md"
RUN = "ask-20261114T100000-aaaaaa"
HALTED = "ask-20261114T090000-dddddd"
OWN = "pkg/own.py"
NEW_HALTED = "pkg/new_halted.py"
NEW_UNKNOWN = "pkg/new_unknown.py"
EXPLAINED = "new file holds content no run baseline attributes to this run"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / "pkg").mkdir()
    (root / OWN).write_bytes(b"a = 1\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(checkpoint_hunks, "checkpoint_run_id", lambda r, message, sid: RUN)
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [])
    monkeypatch.setattr(run_write_prints, "read", lambda r, run: {OWN: "recorded"})
    monkeypatch.setattr(
        run_hunk_claims, "own_hunks", lambda r, run, path, hunks, holders: HunkSplit(own=list(hunks)),
    )
    monkeypatch.setattr(halted_hunk_attribution, "_baseline", lambda r, run: {"files": {}})
    monkeypatch.setattr(
        hunk_provenance, "unproduced_hunks",
        lambda r, run, paths, **k: [
            Finding(NEW_HALTED, "foreign-hunk-unproduced", f"lines 1-3 held by halted run {HALTED}"),
            Finding(NEW_UNKNOWN, "foreign-hunk-unproduced", "lines 1-2 owned by unknown"),
        ],
    )
    (root / OWN).write_bytes(b"a = 2\n")
    (root / NEW_HALTED).write_bytes(b"h = 1\nh = 2\nh = 3\n")
    (root / NEW_UNKNOWN).write_bytes(b"u = 1\nu = 2\n")
    return root


def test_checkpoint_names_halted_and_unknown_owners_with_their_lines_and_lands_the_own_path(repo):
    base = _git(["rev-parse", "HEAD"], repo).strip()

    result = commit_v2._handler(
        {
            "paths": [OWN, NEW_HALTED, NEW_UNKNOWN], "skip_missing": True, "gone_tracked_as_deleted": True,
            "message": f"checkpoint(wave 1): 1 rows — r1\n\nCheckpoint-Plan: {PLAN}\nCheckpoint-Base: {base}\n",
        },
        repo_root=repo / ".git",
    )

    assert result["committed"] is True, result
    assert _git(["show", f"HEAD:{OWN}"], repo) == "a = 2\n"
    reasons = result["checkpoint_withheld_reasons"]
    assert reasons[NEW_HALTED] == f"{EXPLAINED}; lines 1-3 held by halted run {HALTED}"
    assert reasons[NEW_UNKNOWN] == f"{EXPLAINED}; lines 1-2 owned by unknown"
    assert sorted(result["checkpoint_withheld_paths"]) == [NEW_HALTED, NEW_UNKNOWN]
    assert (repo / NEW_HALTED).is_file() and (repo / NEW_UNKNOWN).is_file()

"""A wave checkpoint through the default `ceremony.commit_v2` call lands HEAD plus only the checkpointing
run's recorded hunks of a shared file: a hunk a named peer run recorded stays in the worktree, and a path
the run has no record for while a peer does is withheld naming lines and owner. Case 9 (example-game-repo b9c1a5-1
swept c35e97-2's driver.py hunks) is the `driver.py` row."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import checkpoint_hunks
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit import peer_hunk_gate, run_hunk_claims, run_write_prints
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.run_hunk_claims import HunkSplit
from coordinator_core.session import core as session_core
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "ckc-self"
PLAN = "state/mise-inventory/run.spine.md"
RUN_A = "ask-20261114T100000-aaaaaa"
RUN_B = "ask-20261114T100001-bbbbbb"
LINES = 200


def _git(args, cwd: Path, **kw) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **kw, **no_console_creationflags(),
    ).stdout


def _body(**edits: str) -> str:
    rows = [f"x_{i} = {i}" for i in range(1, LINES + 1)]
    for line, text in edits.items():
        rows[int(line[1:]) - 1] = text
    return "\n".join(rows) + "\n"


def _record(root: Path, run_id: str, path: str, text: str) -> None:
    sha = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=str(root), input=text.encode(), capture_output=True,
        check=True, **no_console_creationflags(),
    ).stdout.decode().strip()
    rdir = root / RECORD_DIR_ROOT / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "kept.json").write_text(json.dumps({"chunks": {"C1": {"paths": {path: sha}}}}), encoding="utf-8")


def _call(root: Path) -> dict:
    base = _git(["rev-parse", "HEAD"], root).strip()
    return commit_v2._handler(
        {
            "paths": [_TARGET[0]], "skip_missing": True, "gone_tracked_as_deleted": True,
            "message": f"checkpoint(wave 1): 1 rows — r1\n\nCheckpoint-Plan: {PLAN}\nCheckpoint-Base: {base}\n",
        },
        repo_root=root / ".git",
    )


_TARGET = ["pkg/shared.py"]


@pytest.fixture(params=["pkg/shared.py", "driver.py"])
def repo(request, tmp_path, monkeypatch):
    _TARGET[0] = request.param
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / _TARGET[0]).parent.mkdir(parents=True, exist_ok=True)
    (root / _TARGET[0]).write_bytes(_body().encode())
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(checkpoint_hunks, "checkpoint_run_id", lambda r, message, sid: RUN_A)
    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [])
    monkeypatch.setattr(run_write_prints, "read", lambda r, run: {_TARGET[0]: "recorded"})
    # The write-print layer calls the whole file the run's own: the sweep the record layer must stop.
    monkeypatch.setattr(
        run_hunk_claims, "own_hunks", lambda r, run, path, hunks, holders: HunkSplit(own=list(hunks)),
    )
    return root


def _both_edit(root: Path) -> None:
    (root / _TARGET[0]).write_bytes(_body(l10="x_10 = 'A'", l150="x_150 = 'B'").encode())


def test_checkpoint_commit_carries_only_the_checkpointing_runs_hunk_and_leaves_the_sibling_dirty(repo):
    _record(repo, RUN_A, _TARGET[0], _body(l10="x_10 = 'A'"))
    _record(repo, RUN_B, _TARGET[0], _body(l150="x_150 = 'B'"))
    _both_edit(repo)

    result = _call(repo)

    assert result["committed"] is True, result
    landed = _git(["show", f"HEAD:{_TARGET[0]}"], repo)
    assert landed == _body(l10="x_10 = 'A'")
    assert (repo / _TARGET[0]).read_text() == _body(l10="x_10 = 'A'", l150="x_150 = 'B'")
    assert "checkpoint_withheld_paths" not in result


def test_a_path_the_run_has_no_record_for_while_a_peer_does_is_withheld_naming_lines_and_owner(repo):
    _record(repo, RUN_B, _TARGET[0], _body(l150="x_150 = 'B'"))
    _both_edit(repo)
    head = _git(["rev-parse", "HEAD"], repo).strip()

    result = _call(repo)

    assert result.get("committed") is not True
    assert result["checkpoint_withheld_paths"] == [_TARGET[0]]
    reason = result["checkpoint_withheld_reasons"][_TARGET[0]]
    assert RUN_B in reason and "150" in reason
    assert _git(["rev-parse", "HEAD"], repo).strip() == head
    assert "'B'" in (repo / _TARGET[0]).read_text()


def test_hunks_no_run_recorded_stay_with_the_write_print_attribution(repo):
    _both_edit(repo)

    result = _call(repo)

    assert result["committed"] is True, result
    assert _git(["show", f"HEAD:{_TARGET[0]}"], repo) == _body(l10="x_10 = 'A'", l150="x_150 = 'B'")

"""`terminal_commit_checkpoints.checkpoint_candidates`: a checkpoint blob is HEAD plus only the checkpointing
run's recorded hunks, a sibling's hunk stays in the worktree, and an unattributable path is refused naming
lines and owner."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import stranded_sibling, terminal_commit_checkpoints as tcc
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

X = "pkg/shared.py"
RUN_A = "ask-20261114T100000-aaaaaa"
RUN_B = "ask-20261114T100001-bbbbbb"
LINES = 200


def _git(args, cwd: Path, **kw):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True,
                          **kw, **no_console_creationflags()).stdout


def _body(**edits: str) -> bytes:
    rows = [f"x_{i} = {i}" for i in range(1, LINES + 1)]
    for line, text in edits.items():
        rows[int(line[1:]) - 1] = text
    return ("\n".join(rows) + "\n").encode("utf-8")


def _blob(root: Path, data: bytes) -> str:
    return _git(["hash-object", "-w", "--stdin"], root, input=data).decode().strip()


def _record(root: Path, run_id: str, sha: str) -> None:
    rdir = root / RECORD_DIR_ROOT / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "kept.json").write_text(json.dumps({"chunks": {"C1": {"paths": {X: sha}}}}), encoding="utf-8")


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
    (root / X).write_bytes(_body())
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _two_runs_one_file(root: Path):
    a_only = _body(l10="x_10 = 'A'")
    b_only = _body(l150="x_150 = 'B'")
    _record(root, RUN_A, _blob(root, a_only))
    _record(root, RUN_B, _blob(root, b_only))
    both = _body(l10="x_10 = 'A'", l150="x_150 = 'B'")
    (root / X).write_bytes(both)
    return a_only, both


def test_two_runs_edit_one_file_the_checkpoint_blob_carries_only_the_checkpointing_runs_hunk(repo):
    a_only, both = _two_runs_one_file(repo)

    out = tcc.checkpoint_candidates(repo, RUN_A, [X])[X]

    assert out["bytes"] == a_only
    assert b"'B'" not in out["bytes"]
    assert out["excluded"] == [{"lines": "150", "owner": RUN_B}]
    assert (repo / X).read_bytes() == both


def test_the_sibling_run_gets_its_own_hunk_and_never_the_checkpointing_runs(repo):
    _a_only, _both = _two_runs_one_file(repo)

    out = tcc.checkpoint_candidates(repo, RUN_B, [X])[X]

    assert out["bytes"] == _body(l150="x_150 = 'B'")
    assert out["excluded"] == [{"lines": "10", "owner": RUN_A}]


def test_a_path_no_hunk_of_the_run_is_recorded_for_is_refused_naming_lines_and_owner(repo):
    _record(repo, RUN_B, _blob(repo, _body(l150="x_150 = 'B'")))
    (repo / X).write_bytes(_body(l150="x_150 = 'B'"))

    out = tcc.checkpoint_candidates(repo, RUN_A, [X])[X]

    assert "bytes" not in out
    assert out["refused"] == "no-evidence"
    assert (out["lines"], out["owner"]) == ("150", RUN_B)


def test_a_wholly_own_file_is_the_worktree_bytes_and_an_unreadable_path_is_absent(repo):
    a_only = _body(l10="x_10 = 'A'")
    _record(repo, RUN_A, _blob(repo, a_only))
    (repo / X).write_bytes(a_only)

    out = tcc.checkpoint_candidates(repo, RUN_A, [X, "pkg/missing.py"])

    assert out == {X: {"bytes": a_only, "excluded": []}}


def test_a_run_touched_blob_attributes_a_path_the_kept_record_omits(repo):
    a_only = _body(l10="x_10 = 'A'")
    stranded_sibling.write(repo / RECORD_DIR_ROOT / RUN_A, {X: {"base": None, "run": _blob(repo, a_only)}})
    (repo / X).write_bytes(_body(l10="x_10 = 'A'", l150="x_150 = 'STRAY'"))

    out = tcc.checkpoint_candidates(repo, RUN_A, [X])[X]

    assert out["bytes"] == a_only
    assert out["excluded"] == [{"lines": "150", "owner": "unknown"}]

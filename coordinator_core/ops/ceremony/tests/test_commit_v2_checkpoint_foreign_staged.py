"""A checkpoint `ceremony.commit_v2` ignores a foreign path staged in the shared
index (the block-discharge record) and commits only its declared row files.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.ceremony import commit_v2

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_RECORD = "state/block-discharge/sess-foreign.jsonl"
_ROW_FILE = "src/row_a.txt"
_PLAN = "docs/plans/p.md"


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    ).stdout


def _w(path, text="x\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "work/p")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    _w(r / "seed.txt")
    _w(r / _PLAN, "# plan\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    return r


def test_a_checkpoint_commits_only_its_rows_and_leaves_a_foreign_staged_record(repo):
    base = _git(repo, "rev-parse", "HEAD").strip()
    _w(repo / _RECORD, '{"kind": "fire"}\n')
    _git(repo, "add", _RECORD)
    _w(repo / _ROW_FILE, "row\n")

    message = (
        "checkpoint(wave 1): 1 rows — A\n\n"
        f"Checkpoint-Plan: {_PLAN}\n"
        f"Checkpoint-Base: {base}\n"
    )
    out = commit_v2._handler(
        {
            "paths": [_ROW_FILE],
            "message": message,
            "skip_missing": True,
            "gone_tracked_as_deleted": True,
        },
        repo_root=repo / ".git",
    )

    assert out["committed"] is True, out
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").split() == [_ROW_FILE]
    assert _git(repo, "diff", "--cached", "--name-only").split() == [_RECORD]

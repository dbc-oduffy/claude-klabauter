"""A checkpoint that withholds a declared path lands a `Checkpoint-Withheld:` trailer and its row is not stamped coded."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from coordinator_core.git import checkpoint_hunks
from coordinator_core.ops.ceremony import commit_v2

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_PLAN = "docs/plans/p.md"
_HELD = "src/held.txt"
_LANDED = "src/landed.txt"
_OTHER = "src/other.txt"


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    ).stdout.strip()


def _w(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _plan_text() -> str:
    body = (
        f"- id: C1\n  title: T1\n  disposition: open\n  writes:\n    - {_HELD}\n    - {_LANDED}\n"
        f"- id: C2\n  title: T2\n  disposition: open\n  writes:\n    - {_OTHER}\n"
    )
    return f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n"


def _rows(repo) -> dict:
    text = (repo / _PLAN).read_text(encoding="utf-8")
    block = text.split("```yaml plan-tasks\n", 1)[1].split("```", 1)[0]
    return {r["id"]: r for r in yaml.safe_load(block)}


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "work/p")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    _w(r / "seed.txt", "x\n")
    _w(r / _PLAN, _plan_text())
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    return r


def _checkpoint(repo):
    base = _git(repo, "rev-parse", "HEAD")
    for p in (_HELD, _LANDED, _OTHER):
        _w(repo / p, "row\n")
    message = f"checkpoint(wave 0): 2 rows — C1, C2\n\nCheckpoint-Plan: {_PLAN}\nCheckpoint-Base: {base}\n"
    return commit_v2._handler(
        {"paths": [_HELD, _LANDED, _OTHER], "message": message}, repo_root=repo / ".git"
    )


def _withhold_held(monkeypatch):
    monkeypatch.setattr(checkpoint_hunks, "checkpoint_run_id", lambda root, message, sid: "run1")

    def own_blobs(root, run_id, paths, message, reasons):
        reasons[_HELD] = "no write-time print of this run"
        return {}, [_HELD]

    monkeypatch.setattr(checkpoint_hunks, "checkpoint_own_blobs", own_blobs)


def test_a_withholding_checkpoint_carries_the_trailer_and_leaves_the_row_open(repo, monkeypatch):
    _withhold_held(monkeypatch)

    out = _checkpoint(repo)

    assert out["committed"] is True, out
    assert out["checkpoint_withheld_paths"] == [_HELD]
    assert out["checkpoint_withheld_reasons"][_HELD]
    landed = _git(repo, "show", "--name-only", "--format=", out["sha"]).split()
    assert _HELD not in landed and _LANDED in landed
    assert f"Checkpoint-Withheld: {_HELD}" in _git(repo, "log", "-1", "--format=%B", out["sha"])
    rows = _rows(repo)
    assert rows["C1"]["disposition"] == "open"
    assert rows["C2"]["disposition"] == "coded"


def test_a_checkpoint_that_withholds_nothing_has_no_trailer_and_stamps_every_row(repo):
    out = _checkpoint(repo)

    assert out["committed"] is True, out
    assert "checkpoint_withheld_paths" not in out
    assert "Checkpoint-Withheld" not in _git(repo, "log", "-1", "--format=%B", out["sha"])
    rows = _rows(repo)
    assert rows["C1"]["disposition"] == "coded" and rows["C2"]["disposition"] == "coded"

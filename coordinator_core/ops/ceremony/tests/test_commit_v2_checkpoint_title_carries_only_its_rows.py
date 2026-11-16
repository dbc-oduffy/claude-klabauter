"""A withholding checkpoint's subject names only rows whose files it carries; withheld rows go in the body."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git import checkpoint_hunks
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.dispatch_emit.checkpoint_stamp import parse_checkpoint

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
        f"- id: C1\n  title: T1\n  disposition: open\n  writes:\n    - {_HELD}\n"
        f"- id: C2\n  title: T2\n  disposition: open\n  writes:\n    - {_LANDED}\n    - {_OTHER}\n"
    )
    return f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n"


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


def _withhold(monkeypatch, held):
    monkeypatch.setattr(checkpoint_hunks, "checkpoint_run_id", lambda root, message, sid: "run1")

    def own_blobs(root, run_id, paths, message, reasons):
        for p in held:
            reasons[p] = "no write-time print of this run"
        return {}, list(held)

    monkeypatch.setattr(checkpoint_hunks, "checkpoint_own_blobs", own_blobs)


def _message(repo, sha):
    return _git(repo, "log", "-1", "--format=%B", sha)


def test_the_subject_drops_a_withheld_row_and_the_body_names_it(repo, monkeypatch):
    _withhold(monkeypatch, [_HELD])

    out = _checkpoint(repo)

    assert out["committed"] is True, out
    message = _message(repo, out["sha"])
    assert message.splitlines()[0] == "checkpoint(wave 0): 1 row — C2"
    assert "Checkpoint-Withheld-Rows: C1" in message.splitlines()
    assert f"Checkpoint-Withheld: {_HELD}" in message
    assert parse_checkpoint(message).row_ids == frozenset({"C2"})


def test_a_row_whose_every_declared_path_is_withheld_leaves_the_other_row_named(repo, monkeypatch):
    _withhold(monkeypatch, [_LANDED, _OTHER])

    out = _checkpoint(repo)

    assert out["committed"] is True, out
    message = _message(repo, out["sha"])
    assert message.splitlines()[0] == "checkpoint(wave 0): 1 row — C1"
    assert "Checkpoint-Withheld-Rows: C2" in message.splitlines()


def test_a_row_split_across_commit_and_withheld_commits_its_carried_part_and_leaves_the_row_open(repo, monkeypatch):
    _withhold(monkeypatch, [_OTHER])

    out = _checkpoint(repo)

    assert out["committed"] is True, out
    message = _message(repo, out["sha"])
    assert message.splitlines()[0] == "checkpoint(wave 0): 1 row — C1"
    assert "Checkpoint-Withheld-Rows: C2" in message.splitlines()
    assert f"Checkpoint-Withheld: {_OTHER}" in message
    assert _LANDED in _git(repo, "show", "--name-only", "--format=", out["sha"]).splitlines()


def test_a_checkpoint_that_withholds_nothing_keeps_its_subject_byte_identical(repo):
    out = _checkpoint(repo)

    assert out["committed"] is True, out
    message = _message(repo, out["sha"])
    assert message.splitlines()[0] == "checkpoint(wave 0): 2 rows — C1, C2"
    assert "Checkpoint-Withheld-Rows" not in message


def test_a_withheld_path_no_named_row_declares_leaves_the_subject_alone(repo, monkeypatch):
    _w(repo / "src/stray.txt", "x\n")
    _withhold(monkeypatch, ["src/stray.txt"])

    out = _checkpoint(repo)

    assert out["committed"] is True, out
    message = _message(repo, out["sha"])
    assert message.splitlines()[0] == "checkpoint(wave 0): 2 rows — C1, C2"
    assert "Checkpoint-Withheld-Rows" not in message

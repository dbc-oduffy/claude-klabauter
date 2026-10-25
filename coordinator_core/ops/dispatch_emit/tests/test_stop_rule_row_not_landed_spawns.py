"""A checkpoint commit's stop-rule trailer keeps the stopped row out of the landed state (real git)."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.emit import checkpoint_landed_state, resume_landed_state
from coordinator_core.ops.dispatch_emit.tests.test_stop_rule_row_not_landed import PLAN, _LINE, _SUBJECT
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()

    def git(*a):
        subprocess.run(["git", *a], cwd=str(r), check=True, capture_output=True, **no_console_creationflags())

    git("init", "-q")
    git("config", "user.email", "t@t.example")
    git("config", "user.name", "t")
    return r, git


def _commit(r, git, name, subject, *trailers):
    (r / name).write_text(name, encoding="utf-8")
    git("add", "--", name)
    git("commit", "-q", "-m", subject, "-m", "\n".join(trailers))


def test_checkpoint_landed_state_omits_the_stopped_row(tmp_path):
    r, git = _repo(tmp_path)
    _commit(r, git, "a", _SUBJECT, f"Checkpoint-Plan: {PLAN}", _LINE)
    landed, owed, _ = checkpoint_landed_state(r, PLAN)
    assert landed == {"L6"}
    assert owed == frozenset()


def test_a_newer_bare_checkpoint_lands_the_stopped_row(tmp_path):
    r, git = _repo(tmp_path)
    _commit(r, git, "a", _SUBJECT, f"Checkpoint-Plan: {PLAN}", _LINE)
    _commit(r, git, "b", "checkpoint(wave 0): 1 rows — L7", f"Checkpoint-Plan: {PLAN}")
    assert checkpoint_landed_state(r, PLAN)[0] == {"L6", "L7"}
    assert resume_landed_state(r, PLAN).rows == {"L6", "L7"}


def test_resume_landed_state_omits_the_stopped_row(tmp_path):
    r, git = _repo(tmp_path)
    _commit(r, git, "a", _SUBJECT, f"Checkpoint-Plan: {PLAN}", _LINE)
    state = resume_landed_state(r, PLAN)
    assert state.rows == {"L6"}
    assert "L7" not in state.row_shas


def test_resume_landed_state_stays_one_spawn(tmp_path, monkeypatch):
    import coordinator_core.git.run as git_run

    r, git = _repo(tmp_path)
    _commit(r, git, "a", _SUBJECT, f"Checkpoint-Plan: {PLAN}", _LINE)
    calls = []
    real = git_run.run_git
    monkeypatch.setattr(git_run, "run_git", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    resume_landed_state(r, PLAN)
    assert len(calls) == 1

"""A row whose executor fired a stop rule is carried by its checkpoint, never landed."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.checkpoint_stamp import STOP_RULE_TRAILER
from coordinator_core.ops.dispatch_emit.emit import (
    checkpoint_landed_state,
    landed_rows_from_text,
    resume_landed_state,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose
from coordinator_core.win_portability import no_console_creationflags

PLAN = "docs/plans/a.md"
_GOLDEN = (
    Path(__file__).resolve().parents[3]
    / "benchmarks" / "fixtures" / "emit_full_path_plan.expected.script.mjs"
)
_SUBJECT = "checkpoint(wave 0): 2 rows — L6, L7"
_LINE = f"{STOP_RULE_TRAILER}: L7"


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


def test_plan_route_committer_writes_the_stop_rule_line():
    script = _GOLDEN.read_text(encoding="utf-8")
    assert "const stopped = done.filter((i) => _stoppedBy.includes(i));" in script
    assert f"'\\n{STOP_RULE_TRAILER}: ' + stopped.join(', ')" in script


def test_ask_route_committer_writes_the_stop_rule_line():
    script = _compose()
    assert "const stopped = done.filter((i) => _stoppedBy.includes(i));" in script
    assert f"'\\n{STOP_RULE_TRAILER}: ' + stopped.join(', ')" in script


@pytest.mark.spawns_process
def test_checkpoint_landed_state_omits_the_stopped_row(tmp_path):
    r, git = _repo(tmp_path)
    _commit(r, git, "a", _SUBJECT, f"Checkpoint-Plan: {PLAN}", _LINE)
    landed, owed, _ = checkpoint_landed_state(r, PLAN)
    assert landed == {"L6"}
    assert owed == frozenset()


@pytest.mark.spawns_process
def test_a_newer_bare_checkpoint_lands_the_stopped_row(tmp_path):
    r, git = _repo(tmp_path)
    _commit(r, git, "a", _SUBJECT, f"Checkpoint-Plan: {PLAN}", _LINE)
    _commit(r, git, "b", "checkpoint(wave 0): 1 rows — L7", f"Checkpoint-Plan: {PLAN}")
    assert checkpoint_landed_state(r, PLAN)[0] == {"L6", "L7"}
    assert resume_landed_state(r, PLAN).rows == {"L6", "L7"}


@pytest.mark.spawns_process
def test_resume_landed_state_omits_the_stopped_row(tmp_path):
    r, git = _repo(tmp_path)
    _commit(r, git, "a", _SUBJECT, f"Checkpoint-Plan: {PLAN}", _LINE)
    state = resume_landed_state(r, PLAN)
    assert state.rows == {"L6"}
    assert "L7" not in state.row_shas


@pytest.mark.spawns_process
def test_resume_landed_state_stays_one_spawn(tmp_path, monkeypatch):
    import coordinator_core.git.run as git_run

    r, git = _repo(tmp_path)
    _commit(r, git, "a", _SUBJECT, f"Checkpoint-Plan: {PLAN}", _LINE)
    calls = []
    real = git_run.run_git
    monkeypatch.setattr(git_run, "run_git", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    resume_landed_state(r, PLAN)
    assert len(calls) == 1


@pytest.mark.parametrize("sep", ["\n", "\\n"])
def test_text_reader_omits_the_stopped_row_in_both_newline_shapes(sep):
    text = f"abc {_SUBJECT}{sep}{sep}Checkpoint-Plan: {PLAN}{sep}{_LINE}"
    assert landed_rows_from_text(text) == {"L6"}


def test_text_reader_keeps_a_row_a_later_subject_names_bare():
    text = f"{_SUBJECT}\n\n{_LINE}\ndef checkpoint(wave 1): 1 rows — L7\n"
    assert landed_rows_from_text(text) == {"L6", "L7"}


def test_text_reader_keeps_the_stopped_row_for_review():
    text = f"{_SUBJECT}\n\n{_LINE}"
    assert landed_rows_from_text(text, exclude_stop_rule=False) == {"L6", "L7"}

"""dispatch.record_halted_run_landed commits the records it writes, in one scoped commit naming the run."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261029T000000-abc123"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(repo), check=True,
        capture_output=True, text=True, **no_console_creationflags(),
    )
    return done.stdout.strip()


@pytest.fixture
def halted_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(op, "_launch", lambda root, run_id: True)
    _git(tmp_path, "init", "-q")
    (tmp_path / "one").write_text("one", encoding="utf-8")
    _git(tmp_path, "add", "--", "one")
    _git(tmp_path, "commit", "-q", "-m", "one")
    first = _git(tmp_path, "rev-parse", "HEAD")
    rd = record_dir(tmp_path, RUN)
    rd.mkdir(parents=True)
    halted_run.write_halt_marker(rd, halted="halted", sizing_rel=None)
    _git(tmp_path, "add", "--", rd.relative_to(tmp_path).as_posix())
    _git(tmp_path, "commit", "-q", "-m", "halt record")
    return tmp_path, first, rd


def test_landed_flip_is_committed_in_one_scoped_commit_naming_the_run(halted_repo):
    root, first, rd = halted_repo
    out = op._handler({"run_id": RUN, "sha": first}, root)
    assert out["ok"] and out["written"] is True
    head = _git(root, "rev-parse", "HEAD")
    assert out["records_commit"] == head
    assert RUN in _git(root, "log", "-1", "--format=%B")
    rel = rd.relative_to(root).as_posix()
    changed = set(_git(root, "show", "--name-only", "--format=", "HEAD").split())
    assert changed == {f"{rel}/halted.json", f"{rel}/run-stage.json"}
    assert _git(root, "status", "--porcelain", "--", rel) == ""


def test_second_call_writes_nothing_and_commits_nothing(halted_repo):
    root, first, _ = halted_repo
    op._handler({"run_id": RUN, "sha": first}, root)
    head = _git(root, "rev-parse", "HEAD")
    again = op._handler({"run_id": RUN, "sha": first}, root)
    assert again["ok"] and again["written"] is False and "records_commit" not in again
    assert _git(root, "rev-parse", "HEAD") == head


def test_refused_landing_commits_nothing(halted_repo):
    root, _, _ = halted_repo
    head = _git(root, "rev-parse", "HEAD")
    out = op._handler({"run_id": RUN, "sha": "0" * 40}, root)
    assert out["ok"] is False
    assert _git(root, "rev-parse", "HEAD") == head


def test_commit_refusal_lands_the_flip_and_reports_the_error(halted_repo, monkeypatch):
    from coordinator_core.git import commit as git_commit

    def _refuse(*a, **k):
        raise git_commit.CommitRefused("no")

    monkeypatch.setattr(git_commit, "commit_paths", _refuse)
    root, first, _ = halted_repo
    out = op._handler({"run_id": RUN, "sha": first}, root)
    assert out["ok"] and out["written"] is True
    assert out["records_commit_error"].startswith("CommitRefused") and "records_commit" not in out


def test_record_dir_outside_state_is_not_committed(tmp_path):
    scratch = tmp_path / "scratch" / RUN
    scratch.mkdir(parents=True)
    (scratch / halted_run.HALTED_FILE).write_text("{}", encoding="utf-8")
    assert op._commit_records(tmp_path, RUN, scratch, "a" * 40) == {}

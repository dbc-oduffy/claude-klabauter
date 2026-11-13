"""dispatch.record_halted_run_landed ``met_by`` on a run in a repo other than the engine root: the halt-commit lookup resolves there and reports a failed git read as retryable."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.run import GitResult
from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "warp-2026-11-13-cross-repo-20261113T000000-abcdef"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(repo), check=True,
        capture_output=True, text=True, **no_console_creationflags(),
    )
    return done.stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", "--", name)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def _no_launch(monkeypatch):
    monkeypatch.setattr(op, "_launch", lambda root, run_id: True)


@pytest.fixture
def foreign_repo(tmp_path, monkeypatch):
    """A repo that is not the process cwd or the engine root, holding a halted run whose halt commit precedes two later commits."""
    repo = tmp_path / "target"
    repo.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    _git(repo, "init", "-q")
    _commit(repo, "before")
    marker = repo / "state" / "warp" / RUN / "halted.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps({"status": "halted", "halted": "ledger-check", "sizing_rel": None, "blocking": ["R1"]}),
        encoding="utf-8",
    )
    _git(repo, "add", "--", "state")
    _git(repo, "commit", "-q", "-m", "halt")
    halt = _git(repo, "rev-parse", "HEAD")
    after = _commit(repo, "after")
    return repo, marker, halt, after


def _call(repo, **params):
    return op._handler({"run_id": RUN, **params}, repo)


def test_met_by_after_the_halt_lands_a_run_in_another_repo(foreign_repo):
    repo, marker, halt, after = foreign_repo
    assert Path(os.getcwd()).resolve() != repo.resolve()
    out = _call(repo, sha=after, met_by={"R1": after})
    assert out["ok"], out
    assert out["met_by"] == {"R1": after}
    assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "landed"
    assert op._halt_commit(repo, marker.parent) == (halt, None)


def test_met_by_before_the_halt_still_refuses_in_another_repo(foreign_repo):
    repo, marker, halt, after = foreign_repo
    before = _git(repo, "rev-parse", f"{halt}~1")
    out = _call(repo, sha=after, met_by={"R1": before})
    assert not out["ok"] and "before the halt" in out["error"]
    assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "halted"


def test_halt_lookup_is_the_bounded_newest_add_query(foreign_repo, monkeypatch):
    repo, marker, _, _ = foreign_repo
    seen = []
    from coordinator_core import git_ancestry

    def spy(args, **kw):
        seen.append(list(args))
        return real_run(args, **kw)

    real_run = git_ancestry.run_git
    monkeypatch.setattr(git_ancestry, "run_git", spy)
    op._halt_commit(repo, marker.parent)
    assert seen and seen[0][:2] == ["log", "-1"], seen


@pytest.mark.parametrize("timed_out,returncode", [(True, -1), (False, 128)])
def test_a_failed_halt_commit_read_is_reported_retryable_not_absent(foreign_repo, monkeypatch, timed_out, returncode):
    repo, marker, _, after = foreign_repo
    from coordinator_core import git_ancestry

    real_run = git_ancestry.run_git

    def flaky(args, **kw):
        if args[:1] == ["log"]:
            return GitResult(returncode, "", "", timed_out)
        return real_run(args, **kw)

    monkeypatch.setattr(git_ancestry, "run_git", flaky)
    out = _call(repo, sha=after, met_by={"R1": after})
    assert not out["ok"]
    assert "could not be read" in out["error"] and "retry" in out["error"]
    assert "could not be found" not in out["error"]
    assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "halted"


def test_an_uncommitted_halt_marker_still_reports_not_found(foreign_repo, monkeypatch):
    repo, marker, _, after = foreign_repo
    from coordinator_core import git_ancestry

    real_run = git_ancestry.run_git

    def empty(args, **kw):
        if args[:1] == ["log"]:
            return GitResult(0, "", "", False)
        return real_run(args, **kw)

    monkeypatch.setattr(git_ancestry, "run_git", empty)
    out = _call(repo, sha=after, met_by={"R1": after})
    assert not out["ok"] and "could not be found" in out["error"]


def test_a_re_halted_run_places_met_by_after_its_newest_halt(foreign_repo):
    repo, marker, first_halt, after = foreign_repo
    body = marker.read_text(encoding="utf-8")
    marker.unlink()
    _git(repo, "add", "-A", "--", "state")
    _git(repo, "commit", "-q", "-m", "unhalt")
    marker.write_text(body, encoding="utf-8")
    _git(repo, "add", "--", "state")
    _git(repo, "commit", "-q", "-m", "re-halt")
    second_halt = _git(repo, "rev-parse", "HEAD")
    later = _commit(repo, "later")
    assert op._halt_commit(repo, marker.parent) == (second_halt, None)
    out = _call(repo, sha=later, met_by={"R1": after})
    assert not out["ok"] and "before the halt" in out["error"] and second_halt in out["error"]
    assert json.loads(marker.read_text(encoding="utf-8"))["status"] == "halted"
    out = _call(repo, sha=later, met_by={"R1": later})
    assert out["ok"], out

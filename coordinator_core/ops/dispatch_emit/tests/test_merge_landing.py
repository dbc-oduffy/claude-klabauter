"""merge_landing.landing_plan: parity with the old terminal_commit plan, plus the owner gate."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git import merge_owner
from coordinator_core.ops.dispatch_emit import merge_landing
from coordinator_core.ops.dispatch_emit.merge_landing import FOREIGN_KEY, landing_plan

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check, **_NOWIN
    )


def _w(repo, name, text):
    (repo / name).write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture()
def merging(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "base")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    for n in ("shared.txt", "auto.txt", "kept.txt"):
        _w(r, n, "base\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "seed")
    _git(r, "checkout", "-q", "-b", "other")
    _w(r, "shared.txt", "other\n")
    _w(r, "auto.txt", "other auto\n")
    _git(r, "commit", "-qam", "other side")
    _git(r, "checkout", "-q", "base")
    _w(r, "shared.txt", "mine\n")
    _w(r, "kept.txt", "mine kept\n")
    _git(r, "commit", "-qam", "my side")
    _git(r, "merge", "other", check=False)
    assert (r / ".git" / "MERGE_HEAD").exists()
    return r


def _own(repo, run_id="run-1"):
    mh = _git(repo, "rev-parse", "MERGE_HEAD").stdout.strip()
    h = _git(repo, "rev-parse", "HEAD").stdout.strip()
    merge_owner.record(repo, run_id, mh, h)


def _resolve(repo):
    _w(repo, "shared.txt", "resolved\n")
    _git(repo, "add", "shared.txt")


def test_no_owner_plans_the_merge_paths_when_resolved(merging):
    _resolve(merging)
    got = landing_plan(merging, ["shared.txt", "extra.txt"], [], product={"shared.txt"})
    assert "shared.txt" in got["paths"] and "extra.txt" not in got["paths"]
    assert got["deleted"] == []
    assert got["artifacts"] == {"paths": ["extra.txt"], "deleted": []}


def test_no_owner_refuses_with_markers(merging):
    got = landing_plan(merging, ["shared.txt"], [], product={"shared.txt"})
    assert got["refusal"].startswith("merge not finished, HEAD unmoved:")
    assert "shared.txt" in got["extra"]["merge_paths"]


def test_no_owner_refuses_foreign_declared_path(merging):
    _resolve(merging)
    _w(merging, "elsewhere.txt", "x\n")
    got = landing_plan(
        merging, ["shared.txt", "elsewhere.txt"], [], product={"shared.txt", "elsewhere.txt"}
    )
    assert got["refusal"].startswith("merge not finished, HEAD unmoved:")


def test_recorded_owner_gets_paths_and_artifacts(merging):
    _resolve(merging)
    _own(merging)
    got = landing_plan(
        merging, ["shared.txt", "note.md"], [], product={"shared.txt"}, owner_run_id="run-1"
    )
    assert "paths" in got and FOREIGN_KEY not in got
    assert "shared.txt" in got["paths"]
    assert got["artifacts"]["paths"] == ["note.md"]


def test_other_owner_is_foreign(merging):
    _resolve(merging)
    _own(merging, "run-1")
    mh = _git(merging, "rev-parse", "MERGE_HEAD").stdout.strip()
    got = landing_plan(merging, [], [], product=set(), owner_run_id="run-2")
    assert got == {FOREIGN_KEY: True, "merge_head": mh}


def test_no_record_is_foreign(merging):
    got = landing_plan(merging, [], [], product=set(), owner_run_id="run-1")
    assert got[FOREIGN_KEY] is True and got["merge_head"]


def test_no_merge_open_is_none(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    assert landing_plan(r, [], [], product=set()) is None
    assert landing_plan(r, [], [], product=set(), owner_run_id="run-1") is None
    assert merge_landing.FOREIGN_KEY == "foreign"

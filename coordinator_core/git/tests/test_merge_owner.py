"""merge_owner: the run-started-merge record is bound to the live MERGE_HEAD and HEAD."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.git import merge_owner
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check,
        **no_console_creationflags(),
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
    _w(r, "shared.txt", "base\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "seed")
    _git(r, "checkout", "-q", "-b", "other")
    _w(r, "shared.txt", "other\n")
    _git(r, "commit", "-qam", "other side")
    _git(r, "checkout", "-q", "base")
    _w(r, "shared.txt", "mine\n")
    _git(r, "commit", "-qam", "my side")
    _git(r, "merge", "other", check=False)
    assert (r / ".git" / "MERGE_HEAD").exists()
    return r


def _shas(r):
    return (
        _git(r, "rev-parse", "MERGE_HEAD").stdout.strip(),
        _git(r, "rev-parse", "HEAD").stdout.strip(),
    )


def test_round_trip(merging):
    mh, h = _shas(merging)
    path = merge_owner.record(merging, "run-1", mh, h)
    assert path == merging / ".git" / "coordinator-merge" / "owner.json"
    assert merge_owner.owner(merging) == "run-1"


def test_no_file_is_none(merging):
    assert merge_owner.owner(merging) is None


def test_malformed_json_is_none(merging):
    p = merging / ".git" / "coordinator-merge" / "owner.json"
    p.parent.mkdir()
    p.write_text("{not json", encoding="utf-8")
    assert merge_owner.owner(merging) is None


def _tamper(repo, **over):
    mh, h = _shas(repo)
    p = merge_owner.record(repo, "run-1", mh, h)
    data = json.loads(p.read_text(encoding="utf-8"))
    data.update(over)
    p.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize(
    "over",
    [{"schema": 2}, {"merge_head": "0" * 40}, {"head": "1" * 40}, {"run_id": ""}],
)
def test_mismatched_record_is_none(merging, over):
    _tamper(merging, **over)
    assert merge_owner.owner(merging) is None


def test_no_merge_head_is_none(merging):
    mh, h = _shas(merging)
    merge_owner.record(merging, "run-1", mh, h)
    (merging / ".git" / "MERGE_HEAD").unlink()
    assert merge_owner.owner(merging) is None


def test_octopus_merge_head_is_none(merging):
    mh, h = _shas(merging)
    merge_owner.record(merging, "run-1", mh, h)
    (merging / ".git" / "MERGE_HEAD").write_text(f"{mh}\n{h}\n", encoding="utf-8")
    assert merge_owner.owner(merging) is None


def test_record_rejects_empty_fields(merging):
    for args in (("", "a", "b"), ("r", "", "b"), ("r", "a", "")):
        with pytest.raises(ValueError):
            merge_owner.record(merging, *args)


def test_clear_removes_file_and_tolerates_absence(merging):
    mh, h = _shas(merging)
    p = merge_owner.record(merging, "run-1", mh, h)
    merge_owner.clear(merging)
    assert not p.exists()
    merge_owner.clear(merging)


def test_owner_spawns_nothing(merging, monkeypatch):
    mh, h = _shas(merging)
    merge_owner.record(merging, "run-1", mh, h)
    calls = []
    real_run, real_popen = subprocess.run, subprocess.Popen

    def run_spy(*a, **k):
        calls.append(a)
        return real_run(*a, **k)

    class PopenSpy(real_popen):
        def __init__(self, *a, **k):
            calls.append(a)
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "run", run_spy)
    monkeypatch.setattr(subprocess, "Popen", PopenSpy)
    assert merge_owner.owner(merging) == "run-1"
    assert calls == []

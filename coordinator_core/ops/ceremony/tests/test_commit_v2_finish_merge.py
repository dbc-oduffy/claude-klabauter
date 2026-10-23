"""`ceremony.commit_v2` `finish_merge` lands a two-parent commit on a real mid-merge repo, refuses a
non-boolean before any write, and leaves a call without the key refused as before."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SID = "11111111-2222-4333-8444-555555555555"
_PATHS = ["auto.txt", "shared.txt"]


def _git(repo: Path, *args: str, check: bool = True) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check,
        **no_console_creationflags(),
    ).stdout.strip()


def _w(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8", newline="\n")


def _call(repo: Path, params: dict) -> dict:
    return commit_v2._handler(params, repo_root=repo / ".git")


def _params(**extra) -> dict:
    return {"paths": list(_PATHS), "message": "merge other\n", "session_id": _SID, **extra}


@pytest.fixture
def merging(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "base")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "core.autocrlf", "false")
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


def _parents(repo: Path) -> list:
    raw = _git(repo, "cat-file", "-p", "HEAD")
    return [ln.split()[1] for ln in raw.splitlines() if ln.startswith("parent ")]


def test_finish_merge_lands_two_parents_and_reports_merge_parent(merging):
    old_head = _git(merging, "rev-parse", "HEAD")
    merge_head = _git(merging, "rev-parse", "MERGE_HEAD")
    _w(merging, "shared.txt", "resolved\n")

    reply = _call(merging, _params(finish_merge=True))

    assert reply["committed"] is True, reply
    assert reply["merge_parent"] == merge_head
    assert _parents(merging) == [old_head, merge_head]
    assert _git(merging, "rev-parse", "HEAD") == reply["sha"]
    assert not (merging / ".git" / "MERGE_HEAD").exists()


def test_finish_merge_still_refuses_a_conflict_marker(merging):
    before = _git(merging, "rev-parse", "HEAD")

    reply = _call(merging, _params(finish_merge=True))

    assert reply["committed"] is False
    assert _git(merging, "rev-parse", "HEAD") == before
    assert (merging / ".git" / "MERGE_HEAD").exists()


@pytest.mark.parametrize("bad", ["yes", 1, None, []])
def test_non_boolean_finish_merge_is_refused_before_any_write(merging, bad):
    before = _git(merging, "rev-parse", "HEAD")
    _w(merging, "shared.txt", "resolved\n")

    reply = _call(merging, _params(finish_merge=bad))

    assert reply["committed"] is False
    assert "finish_merge must be a boolean" in reply["error"]
    assert _git(merging, "rev-parse", "HEAD") == before
    assert (merging / ".git" / "MERGE_HEAD").exists()


def test_absent_key_keeps_the_mid_merge_refusal(merging):
    before = _git(merging, "rev-parse", "HEAD")
    _w(merging, "shared.txt", "resolved\n")

    reply = _call(merging, _params())

    assert reply["committed"] is False
    assert "MERGE_HEAD" in reply["error"]
    assert _git(merging, "rev-parse", "HEAD") == before
    assert "merge_parent" not in reply


def test_ordinary_commit_carries_no_merge_parent(tmp_path):
    r = tmp_path / "plain"
    r.mkdir()
    _git(r, "init", "-q", "-b", "base")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    _w(r, "a.txt", "a\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "seed")
    _w(r, "a.txt", "b\n")

    reply = _call(r, {"paths": ["a.txt"], "message": "edit\n", "session_id": _SID})

    assert reply["committed"] is True, reply
    assert "merge_parent" not in reply


_MIXED = ["state/x.yaml", "coordinator_core/y.py"]


@pytest.fixture
def mixed_merging(tmp_path):
    r = tmp_path / "m"
    r.mkdir()
    _git(r, "init", "-q", "-b", "base")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "core.autocrlf", "false")
    for n in _MIXED:
        (r / n).parent.mkdir(parents=True, exist_ok=True)
        _w(r, n, "base\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "seed")
    _git(r, "checkout", "-q", "-b", "other")
    _w(r, "state/x.yaml", "other\n")
    _w(r, "coordinator_core/y.py", "other\n")
    _git(r, "commit", "-qam", "other side")
    _git(r, "checkout", "-q", "base")
    _w(r, "coordinator_core/y.py", "mine\n")
    _git(r, "commit", "-qam", "my side")
    _git(r, "merge", "other", check=False)
    assert (r / ".git" / "MERGE_HEAD").exists()
    return r


def _mixed_params(**extra) -> dict:
    return {"paths": list(_MIXED), "message": "merge other\n", "session_id": _SID, **extra}


def test_mixed_finish_merge_lands_one_two_parent_commit(mixed_merging):
    r = mixed_merging
    old_head = _git(r, "rev-parse", "HEAD")
    merge_head = _git(r, "rev-parse", "MERGE_HEAD")
    _w(r, "coordinator_core/y.py", "resolved\n")

    reply = _call(r, _mixed_params(finish_merge=True))

    assert reply["committed"] is True, reply
    assert _parents(r) == [old_head, merge_head]
    assert _git(r, "rev-list", "--count", f"{old_head}..HEAD") == "2"
    assert "(process files)" not in _git(r, "log", "-1", "--format=%s")


def test_mixed_retry_without_merge_head_still_splits(mixed_merging):
    r = mixed_merging
    _git(r, "merge", "--abort")
    _w(r, "state/x.yaml", "edit\n")
    _w(r, "coordinator_core/y.py", "edit\n")

    reply = _call(r, _mixed_params(finish_merge=True))

    assert reply["committed"] is True, reply
    assert len(_parents(r)) == 1
    assert "(process files)" in _git(r, "log", "-1", "--format=%s")


def test_mixed_without_finish_merge_keeps_the_mid_merge_refusal(mixed_merging):
    r = mixed_merging
    before = _git(r, "rev-parse", "HEAD")
    _w(r, "coordinator_core/y.py", "resolved\n")

    reply = _call(r, _mixed_params())

    assert reply["committed"] is False
    assert _git(r, "rev-parse", "HEAD") == before
    assert (r / ".git" / "MERGE_HEAD").exists()

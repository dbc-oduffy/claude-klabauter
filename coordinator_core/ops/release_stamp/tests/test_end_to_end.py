"""Each outcome of the stamp-at-tag-cut exit criterion, through the registered op on a real git repo."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops import release_stamp as rs
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_DATES = {
    "v0.1.0": "2026-01-05T10:00:00",
    "v0.2.0": "2026-02-06T10:00:00",
    "v0.3.0": "2026-03-07T10:00:00",
    "head": "2026-04-08T10:00:00",
}
_ENTRY = "---\ntitle: {n}\nstatus: {status}\n---\nbody {n}\n"
_PENDING = "pending-release"
_ROOT = "archive/completed"


def _git(repo: Path, *args: str, date: str = _DATES["head"]) -> str:
    env = {
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    out = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **env},
        **no_console_creationflags(),
    )
    return out.stdout.strip()


def _put(repo: Path, rel: str, name: str, status: str = _PENDING, body: str | None = None) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body if body is not None else _ENTRY.format(n=name, status=status), encoding="utf-8", newline="")


def _commit(repo: Path, msg: str, date: str) -> None:
    _git(repo, "add", "-A", date=date)
    _git(repo, "commit", "-q", "-m", msg, date=date)


def _snapshot(repo: Path) -> dict[str, bytes]:
    root = repo / _ROOT
    return {
        p.relative_to(repo).as_posix(): p.read_bytes() for p in sorted(root.rglob("*.md"))
    }


def _call(repo: Path, **params):
    handler = _REGISTRY["completion.stamp_releases"]
    return asyncio.run(handler(params, repo))


def _status(repo: Path, rel: str) -> str:
    for line in (repo / rel).read_text(encoding="utf-8").splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError(rel)


A = f"{_ROOT}/2026-01/a.md"  # first held by v0.1.0
B = f"{_ROOT}/2026-01/b.md"  # added in v0.2.0, still in v0.3.0
MOVED_OLD = f"{_ROOT}/2026-01/moved.md"  # in v0.1.0 under 2026-01
MOVED = f"{_ROOT}/2026-02/moved.md"  # HEAD path after the move
RENAMED_OLD = f"{_ROOT}/2026-02/old-name.md"  # in v0.2.0
RENAMED = f"{_ROOT}/2026-02/new-name.md"  # HEAD path, same bytes
C = f"{_ROOT}/2026-03/c.md"  # added in v0.3.0
OTHER = f"{_ROOT}/2026-01/other.md"  # released under v0.1.0 already
LATE = f"{_ROOT}/2026-04/late.md"  # committed after the newest tag
UNTAGGED = f"{_ROOT}/2026-04/untracked.md"  # never committed


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _put(r, A, "a")
    _put(r, MOVED_OLD, "moved")
    _put(r, OTHER, "other", status="released")
    _commit(r, "one", _DATES["v0.1.0"])
    _git(r, "tag", "v0.1.0", date=_DATES["v0.1.0"])
    _put(r, B, "b")
    _put(r, RENAMED_OLD, "renamed")
    _commit(r, "two", _DATES["v0.2.0"])
    _git(r, "tag", "-a", "v0.2.0", "-m", "rel", date=_DATES["v0.2.0"])
    _git(r, "tag", "backup-2026", date=_DATES["v0.2.0"])
    _put(r, C, "c")
    _commit(r, "three", _DATES["v0.3.0"])
    _git(r, "tag", "v0.3.0", date=_DATES["v0.3.0"])
    (r / MOVED).parent.mkdir(parents=True, exist_ok=True)
    (r / MOVED_OLD).rename(r / MOVED)
    (r / RENAMED_OLD).rename(r / RENAMED)
    _put(r, LATE, "late")
    _commit(r, "head", _DATES["head"])
    _put(r, UNTAGGED, "untracked")
    return r


def _tag_facts(repo: Path, tag: str) -> tuple[str, str]:
    sha = _git(repo, "log", "-1", "--format=%H", tag)
    date = _git(repo, "log", "-1", "--format=%as", tag)
    return sha, date


def _fields(repo: Path, rel: str) -> dict[str, str]:
    out = {}
    for line in (repo / rel).read_text(encoding="utf-8").split("---")[1].strip().splitlines():
        k, _, v = line.partition(":")
        out[k] = v.strip()
    return out


def test_run_stamps_every_tagged_pending_entry_with_the_four_fields(repo: Path) -> None:
    res = _call(repo)

    stamped = {s["path"]: s for s in res["stamped"]}
    assert set(stamped) == {A, B, MOVED, RENAMED, C}
    for rel, s in stamped.items():
        sha, date = _tag_facts(repo, s["released_in"])
        assert (s["released_sha"], s["released_at"]) == (sha, date)
        f = _fields(repo, rel)
        assert f["status"] == "released"
        assert (f["released_in"], f["released_at"], f["released_sha"]) == (
            s["released_in"],
            date,
            sha,
        )
    assert res["tags_considered"] == 3 and res["skipped"] == [] and res["report"] is False


def test_earliest_tag_wins_and_moved_and_renamed_entries_follow_first_holder(repo: Path) -> None:
    stamped = {s["path"]: s["released_in"] for s in _call(repo)["stamped"]}

    assert stamped[A] == "v0.1.0"
    assert stamped[B] == "v0.2.0"  # also in v0.3.0
    assert stamped[MOVED] == "v0.1.0"  # held under another month in v0.1.0
    assert stamped[RENAMED] == "v0.2.0"  # same blob as old-name.md in v0.2.0
    assert stamped[C] == "v0.3.0"


def test_second_run_stamps_nothing_and_changes_no_bytes(repo: Path) -> None:
    other_before = (repo / OTHER).read_bytes()
    _call(repo)
    after_first = _snapshot(repo)

    second = _call(repo)

    assert second["stamped"] == []
    assert _snapshot(repo) == after_first
    assert (repo / OTHER).read_bytes() == other_before


def test_entries_in_no_tag_tree_stay_pending(repo: Path) -> None:
    before = {p: (repo / p).read_bytes() for p in (LATE, UNTAGGED)}

    res = _call(repo)

    assert res["unreleased"] == {"count": 2, "paths": sorted([LATE, UNTAGGED])}
    for rel, data in before.items():
        assert (repo / rel).read_bytes() == data and _status(repo, rel) == _PENDING


def test_report_mode_matches_write_mode_and_writes_nothing(repo: Path) -> None:
    before = _snapshot(repo)

    dry = _call(repo, report=True)

    assert dry["report"] is True and _snapshot(repo) == before
    wet = _call(repo)
    assert dry["stamped"] == wet["stamped"] and dry["unreleased"] == wet["unreleased"]


def test_repo_without_version_tag_reports_all_unreleased_and_writes_nothing(tmp_path: Path) -> None:
    r = tmp_path / "bare"
    r.mkdir()
    _git(r, "init", "-q")
    _put(r, A, "a")
    _put(r, B, "b")
    _commit(r, "one", _DATES["v0.1.0"])
    _git(r, "tag", "backup-2026")
    before = _snapshot(r)

    res = _call(r)

    assert res["tags_considered"] == 0 and res["stamped"] == []
    assert res["unreleased"] == {"count": 2, "paths": sorted([A, B])}
    assert _snapshot(r) == before


def test_op_is_registered() -> None:
    assert rs._stamp_releases_handler is _REGISTRY["completion.stamp_releases"]

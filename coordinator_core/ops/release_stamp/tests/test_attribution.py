"""attribute() against real tmp git repos."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.release_stamp import attribution
from coordinator_core.ops.release_stamp.model import ReleaseTag, StampError
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

ROOT = "archive/completed"


def _git(repo: Path, *args: str) -> str:
    env = {
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


def _entry(status: str = "pending-release", body: str = "x") -> str:
    return f"---\nstatus: {status}\n---\n{body}\n"


def _put(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="")


def _cut(repo: Path, version: tuple[int, int, int]) -> ReleaseTag:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "c" + ".".join(map(str, version)))
    name = "v" + ".".join(map(str, version))
    sha = _git(repo, "rev-parse", "HEAD")
    return ReleaseTag(name, version, sha, _git(repo, "rev-parse", "HEAD^{tree}"), "2026-01-01")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    return r


def _commit_only(repo: Path, msg: str = "work") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", msg)


def test_first_of_three_tags_by_second_presence(repo: Path) -> None:
    _put(repo, f"{ROOT}/2026-01/a.md", _entry())
    t1 = _cut(repo, (0, 1, 0))
    _put(repo, f"{ROOT}/2026-01/b.md", _entry(body="other"))
    t2 = _cut(repo, (0, 2, 0))
    t3 = _cut(repo, (0, 3, 0))
    res = attribution.attribute(repo, [t3, t1, t2])
    assert res.stamps[f"{ROOT}/2026-01/a.md"] == t1
    assert res.stamps[f"{ROOT}/2026-01/b.md"] == t2
    assert res.unreleased == ()


def test_moved_between_months_takes_first(repo: Path) -> None:
    _put(repo, f"{ROOT}/2026-01/a.md", _entry())
    t1 = _cut(repo, (0, 1, 0))
    (repo / f"{ROOT}/2026-01/a.md").rename(_mkdir(repo, "2026-02") / "a.md")
    t2 = _cut(repo, (0, 2, 0))
    res = attribution.attribute(repo, [t1, t2])
    assert res.stamps[f"{ROOT}/2026-02/a.md"] == t1


def _mkdir(repo: Path, month: str) -> Path:
    d = repo / ROOT / month
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_renamed_unchanged_bytes_takes_first(repo: Path) -> None:
    _put(repo, f"{ROOT}/2026-01/a.md", _entry(body="same"))
    t1 = _cut(repo, (0, 1, 0))
    (repo / f"{ROOT}/2026-01/a.md").rename(repo / f"{ROOT}/2026-01/renamed.md")
    t2 = _cut(repo, (0, 2, 0))
    res = attribution.attribute(repo, [t1, t2])
    assert res.stamps[f"{ROOT}/2026-01/renamed.md"] == t1


def test_renamed_and_edited_takes_new_path_tag(repo: Path) -> None:
    _put(repo, f"{ROOT}/2026-01/a.md", _entry(body="one"))
    t1 = _cut(repo, (0, 1, 0))
    (repo / f"{ROOT}/2026-01/a.md").unlink()
    _put(repo, f"{ROOT}/2026-01/renamed.md", _entry(body="two"))
    t2 = _cut(repo, (0, 2, 0))
    res = attribution.attribute(repo, [t1, t2])
    assert res.stamps[f"{ROOT}/2026-01/renamed.md"] == t2


def test_no_tag_holds_is_unreleased_and_noise_ignored(repo: Path) -> None:
    _put(repo, f"{ROOT}/2026-01/old.md", _entry("released"))
    _put(repo, f"{ROOT}/2026-01/notes.txt", "pending-release")
    t1 = _cut(repo, (0, 1, 0))
    _put(repo, f"{ROOT}/2026-01/new.md", _entry())
    _commit_only(repo)
    res = attribution.attribute(repo, [t1])
    assert res.stamps == {}
    assert res.unreleased == (f"{ROOT}/2026-01/new.md",)


def test_uncommitted_entry_attributed_by_path_and_name(repo: Path) -> None:
    _put(repo, f"{ROOT}/2026-01/a.md", _entry())
    t1 = _cut(repo, (0, 1, 0))
    _put(repo, f"{ROOT}/2026-02/a.md", _entry())
    res = attribution.attribute(repo, [t1])
    assert res.stamps[f"{ROOT}/2026-02/a.md"] == t1


def test_status_inside_tag_is_not_read(repo: Path) -> None:
    _put(repo, f"{ROOT}/2026-01/a.md", _entry("session-closed-workstream-open"))
    t1 = _cut(repo, (0, 1, 0))
    _put(repo, f"{ROOT}/2026-01/a.md", _entry())
    _commit_only(repo)
    res = attribution.attribute(repo, [t1])
    assert res.stamps[f"{ROOT}/2026-01/a.md"] == t1


def test_no_tags_reads_no_tree(repo: Path) -> None:
    _put(repo, f"{ROOT}/2026-01/a.md", _entry())
    _commit_only(repo)
    res = attribution.attribute(repo, [])
    assert res.unreleased == (f"{ROOT}/2026-01/a.md",)


def test_missing_tag_tree_raises(repo: Path) -> None:
    _put(repo, f"{ROOT}/2026-01/a.md", _entry())
    t1 = _cut(repo, (0, 1, 0))
    completed = _git(repo, "rev-parse", f"{t1.commit_sha}:{ROOT}")
    for p in (repo / ".git" / "objects").rglob(completed[2:]):
        p.chmod(0o600)
        p.unlink()
    from coordinator_core.git import git_objects

    git_objects._OBJECT_CACHE.clear()
    with pytest.raises(StampError) as exc:
        attribution.attribute(repo, [t1])
    assert exc.value.reason == "tree-unreadable"
    assert "v0.1.0" in exc.value.detail


def test_unreadable_head_with_tags_raises(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _put(repo, f"{ROOT}/2026-01/a.md", _entry())
    t1 = _cut(repo, (0, 1, 0))
    monkeypatch.setattr(attribution, "read_tree_spine", lambda *a, **k: None)
    with pytest.raises(StampError) as exc:
        attribution.attribute(repo, [t1])
    assert exc.value.reason == "tree-unreadable"
    assert "HEAD" in exc.value.detail


def test_starts_no_child_process(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _put(repo, f"{ROOT}/2026-01/a.md", _entry())
    t1 = _cut(repo, (0, 1, 0))
    started: list[object] = []

    class Counting(subprocess.Popen):
        def __init__(self, *a, **k):
            started.append(a)
            super().__init__(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", Counting)
    attribution.attribute(repo, [t1])
    assert started == []

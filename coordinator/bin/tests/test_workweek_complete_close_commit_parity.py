"""Parity net for `workweek-complete-close.py archive`'s observable result.

Pins what the archive CLI leaves behind on a real git fixture: moved files,
the HEADER rewrite, the committed path set (name-status), the subject, the
Session-Id trailer, one commit added, and the ref pushed to a bare origin.
The commit tail may be re-implemented; these expected values may not change.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent
_SESSION_ID = "11111111-2222-4333-8444-555555555555"
_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_BRANCH = "work/parity/2026-08-24"


def _fwd(p) -> str:
    return str(p).replace("\\", "/")


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "workweek_complete_close_parity", _BIN_DIR / "workweek-complete-close.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
        **_NO_WINDOW,
    )
    return result.stdout


def _install_hook(repo: Path) -> None:
    hook = repo / ".git" / "hooks" / "prepare-commit-msg"
    hook.parent.mkdir(parents=True, exist_ok=True)
    script = _BIN_DIR / "coordinator-prepare-commit-msg.py"
    hook.write_text(
        f'#!/bin/sh\nexec "{_fwd(sys.executable)}" "{_fwd(script)}" "$@"\n',
        encoding="utf-8",
        newline="\n",
    )
    hook.chmod(0o755)


def _seed(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")


_TRACKED = {
    "README.md": "fixture\n",
    "state/week-changelog/HEADER.md": "**Week starting:** 2026-08-17\n",
    "state/week-changelog/HEADER.priorities.abc123.md": "- p1\n",
    "state/week-changelog/2026-08-17.md": "mon\n",
    "state/week-changelog/2026-08-18.md": "tue\n",
    "state/week-changelog/2026-08-18-pending-release.md": "accumulator\n",
    "state/week-changelog/2026-08-26.md": "next week, tracked\n",
    "state/review-trail/2026-08-18-review.json": "{}\n",
    "state/review-trail/.weekly-reviewer-scopes-abc123.json": "{}\n",
    "state/review-trail/.gitkeep": "",
}


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    repo.mkdir()
    empty_cfg = tmp_path / "empty.gitconfig"
    empty_cfg.write_text("", encoding="utf-8")
    for key, value in {
        "GIT_CONFIG_GLOBAL": str(empty_cfg),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": _fwd(repo / ".git" / "hooks"),
        "GIT_AUTHOR_NAME": "Parity",
        "GIT_AUTHOR_EMAIL": "parity@example.invalid",
        "GIT_COMMITTER_NAME": "Parity",
        "GIT_COMMITTER_EMAIL": "parity@example.invalid",
        "COORDINATOR_SESSION_ID": _SESSION_ID,
    }.items():
        monkeypatch.setenv(key, value)
    for var in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)

    _git(tmp_path, "init", "--bare", "-b", _BRANCH, str(origin))
    _git(repo, "init", "-b", _BRANCH)
    _install_hook(repo)
    _seed(repo, _TRACKED)
    _git(repo, "add", "--", ".")
    _git(repo, "commit", "-m", "seed")
    _git(repo, "remote", "add", "origin", _fwd(origin))
    _git(repo, "push", "origin", _BRANCH)
    # A peer's in-flight, uncommitted work in the same tree.
    _seed(
        repo,
        {
            "state/review-trail/2026-08-30-peer.json": "{}\n",
        },
    )
    return repo, origin


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


def _count(repo: Path) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD").strip())


def _name_status(repo: Path) -> list[str]:
    out = _git(repo, "show", "--name-status", "--no-renames", "--format=", "HEAD")
    return sorted(line.replace("\t", " ") for line in out.splitlines() if line)


def _subject(repo: Path) -> str:
    return _git(repo, "log", "-1", "--format=%s").strip()


def _body(repo: Path) -> str:
    return _git(repo, "log", "-1", "--format=%B")


def _origin_ref(origin: Path) -> str:
    return _git(origin, "rev-parse", _BRANCH).strip()


def _status(repo: Path) -> set[str]:
    return set(_git(repo, "status", "--porcelain", "-uall").splitlines())


def _run(repo: Path, *extra: str) -> int:
    return _mod.main(
        [
            "archive",
            "--repo-root",
            str(repo),
            "--version",
            "v9.9.9",
            "--merge-sha",
            "cafe1234",
            "--released-date",
            "2026-08-24",
            *extra,
        ]
    )


def test_default_sweep_commits_and_pushes(fixture):
    repo, origin = fixture
    before = _count(repo)

    assert _run(repo) == 0

    assert _count(repo) == before + 1
    assert _subject(repo) == (
        "chore(workweek-complete): archive week 2026-08-17, reset changelog + review-trail v9.9.9"
    )
    assert f"Session-Id: {_SESSION_ID}" in _body(repo)

    arch = repo / "archive" / "week-changelogs"
    assert (arch / "2026-08-17" / "2026-08-17.md").read_text(encoding="utf-8") == "mon\n"
    assert (arch / "2026-08-17" / "2026-08-18.md").exists()
    assert (arch / "2026-08-17" / "2026-08-18-pending-release.md").exists()
    assert (arch / "2026-08-17" / "HEADER.priorities.abc123.md").exists()
    assert (arch / "2026-08-24" / "2026-08-26.md").exists()
    rt = repo / "archive" / "review-trail"
    assert (rt / "2026-08-17" / "2026-08-18-review.json").exists()
    assert (rt / "2026-08-17" / "2026-08-30-peer.json").exists()
    assert not (repo / "state/review-trail/.weekly-reviewer-scopes-abc123.json").exists()
    assert (repo / "state/review-trail/.gitkeep").exists()

    header = (repo / "state/week-changelog/HEADER.md").read_text(encoding="utf-8")
    assert header.startswith("# Week Changelog\n")
    assert "v9.9.9" in header and "cafe1234" in header and "2026-08-24" in header
    assert "2026-08-17" not in header.split("-->")[-1]

    assert _name_status(repo) == [
        "A archive/review-trail/2026-08-17/2026-08-18-review.json",
        "A archive/review-trail/2026-08-17/2026-08-30-peer.json",
        "A archive/week-changelogs/2026-08-17/2026-08-17.md",
        "A archive/week-changelogs/2026-08-17/2026-08-18-pending-release.md",
        "A archive/week-changelogs/2026-08-17/2026-08-18.md",
        "A archive/week-changelogs/2026-08-17/HEADER.priorities.abc123.md",
        "A archive/week-changelogs/2026-08-24/2026-08-26.md",
        "D state/review-trail/.weekly-reviewer-scopes-abc123.json",
        "D state/review-trail/2026-08-18-review.json",
        "D state/week-changelog/2026-08-17.md",
        "D state/week-changelog/2026-08-18-pending-release.md",
        "D state/week-changelog/2026-08-18.md",
        "D state/week-changelog/2026-08-26.md",
        "D state/week-changelog/HEADER.priorities.abc123.md",
        "M state/week-changelog/HEADER.md",
    ]
    assert _origin_ref(origin) == _head(repo)
    assert _status(repo) == set()


def test_week_only_commits_only_touched_paths(fixture):
    repo, origin = fixture
    before = _count(repo)
    peer = repo / "state" / "week-changelog" / "2026-09-02-peer-later.md"
    peer.write_text("later peer work\n", encoding="utf-8")

    rc = _run(
        repo,
        "--week-only",
        "--move-priorities",
        "--exclude",
        "*-pending-release.md",
        "--week-ending",
        "2026-08-23",
    )

    assert rc == 0
    assert _count(repo) == before + 1
    assert _subject(repo) == (
        "chore(workweek-complete): archive week 2026-08-17, reset changelog + review-trail v9.9.9"
    )
    assert f"Session-Id: {_SESSION_ID}" in _body(repo)

    wc = repo / "state" / "week-changelog"
    arch = repo / "archive" / "week-changelogs" / "2026-08-17"
    assert (arch / "2026-08-17.md").exists() and not (wc / "2026-08-17.md").exists()
    assert (arch / "2026-08-18.md").exists()
    assert (arch / "HEADER.priorities.abc123.md").exists()
    assert (wc / "2026-08-18-pending-release.md").exists()
    assert (wc / "2026-08-26.md").exists()
    assert (repo / "state/review-trail/.weekly-reviewer-scopes-abc123.json").exists()
    assert (repo / "state/review-trail/2026-08-30-peer.json").exists()
    assert "v9.9.9" in (wc / "HEADER.md").read_text(encoding="utf-8")

    assert _name_status(repo) == [
        "A archive/review-trail/2026-08-17/2026-08-18-review.json",
        "A archive/week-changelogs/2026-08-17/2026-08-17.md",
        "A archive/week-changelogs/2026-08-17/2026-08-18.md",
        "A archive/week-changelogs/2026-08-17/HEADER.priorities.abc123.md",
        "D state/review-trail/2026-08-18-review.json",
        "D state/week-changelog/2026-08-17.md",
        "D state/week-changelog/2026-08-18.md",
        "D state/week-changelog/HEADER.priorities.abc123.md",
        "M state/week-changelog/HEADER.md",
    ]
    assert _origin_ref(origin) == _head(repo)
    assert _status(repo) == {
        "?? state/review-trail/2026-08-30-peer.json",
        "?? state/week-changelog/2026-09-02-peer-later.md",
    }


def test_default_sweep_leaves_hand_deleted_tracked_file_out_of_commit(fixture):
    repo, _origin = fixture
    note = "state/week-changelog/PEER-NOTE.md"
    _seed(repo, {note: "peer note, not a daily block\n"})
    _git(repo, "add", "--", note)
    _git(repo, "commit", "-m", "peer note")
    (repo / note).unlink()
    before = _count(repo)

    assert _run(repo, "--no-push") == 0

    assert _count(repo) == before + 1
    assert note not in "".join(_name_status(repo))
    assert f" D {note}" in _status(repo)


def test_no_push_commits_without_moving_origin(fixture):
    repo, origin = fixture
    before_count = _count(repo)
    origin_before = _origin_ref(origin)

    assert _run(repo, "--no-push") == 0

    assert _count(repo) == before_count + 1
    assert f"Session-Id: {_SESSION_ID}" in _body(repo)
    assert _origin_ref(origin) == origin_before
    assert _origin_ref(origin) != _head(repo)
    assert "M state/week-changelog/HEADER.md" in _name_status(repo)

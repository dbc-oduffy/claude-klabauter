"""The quick-wrap brief's git spawn budget: one raw floor..head log, one own-commit numstat log
(only when under the commit and surface brightline) and one status."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core import quick_wrap_assemble as qwa
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

SID = "11111111-2222-3333-4444-555555555555"


def _git(root: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    out = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=root, capture_output=True, text=True, check=True, env=env, **no_console_creationflags(),
    )
    return out.stdout.strip()


def _repo(root: Path, *, seed_floor: bool) -> None:
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    floor = _git(root, "rev-parse", "HEAD")
    sdir = root / ".git" / "coordinator-sessions" / SID
    sdir.mkdir(parents=True)
    (sdir / "started_at").write_text("2026-10-01T10:00:30Z\n", encoding="utf-8")
    if seed_floor:
        (sdir / "head_at_start").write_text(floor + "\n", encoding="utf-8")
    (root / "work.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"work\n\nSession-Id: {SID}\n")


@pytest.fixture
def spawns(monkeypatch):
    calls: list = []
    real = subprocess.Popen

    class Counting(real):
        def __init__(self, args, *a, **k):
            calls.append(list(args) if not isinstance(args, str) else [args])
            super().__init__(args, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", Counting)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SID)
    return calls


def _is_git(argv: list) -> bool:
    return Path(str(argv[0])).stem.lower() == "git"


def test_brief_makes_one_log_and_one_status_with_a_seeded_floor(tmp_path, spawns):
    repo = tmp_path / "repo"
    _repo(repo, seed_floor=True)
    spawns.clear()

    envelope = qwa.brief(repo, commit=False)

    assert [c for c in spawns if not _is_git(c)] == []
    verbs = [next(a for a in c[1:] if a in ("log", "status")) for c in spawns]
    assert sorted(verbs) == ["log", "log", "status"], spawns
    assert len(_numstat_spawns(spawns)) == 1
    assert envelope["gates"]["close_gate"]["diff"]["degraded"] is False


def _more_commits(root: Path, n: int, lines: int = 1) -> None:
    for i in range(n):
        (root / f"mod{i}.py").write_text("x = 1\n" * lines, encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", f"more {i}\n\nSession-Id: {SID}\n")


def _numstat_spawns(spawns: list) -> list:
    return [c for c in spawns if "--numstat" in c]


def test_over_commit_brightline_skips_numstat_and_reports_loc_none(tmp_path, spawns):
    repo = tmp_path / "repo"
    _repo(repo, seed_floor=True)
    _more_commits(repo, 5)
    spawns.clear()

    envelope = qwa.brief(repo, commit=False)

    assert _numstat_spawns(spawns) == []
    value = envelope["gates"]["close_gate"]["diff"]["value"]
    assert "novel_commit_count" in value["breached"]
    assert value["novel_loc"] is None and value["gross_loc"] is None
    assert "already over brightline on novel_commit_count" in value["loc_skipped"]
    c2 = {c["id"]: c for c in envelope["gates"]["entry_test"]["conditions"]}["c2"]
    assert c2["passed"] is False
    assert "loc not computed" in c2["evidence"]


def test_under_brightline_runs_numstat_for_own_commits_only(tmp_path, spawns):
    repo = tmp_path / "repo"
    _repo(repo, seed_floor=True)
    _more_commits(repo, 2, lines=3)
    spawns.clear()

    envelope = qwa.brief(repo, commit=False)

    assert len(_numstat_spawns(spawns)) == 1
    value = envelope["gates"]["close_gate"]["diff"]["value"]
    assert (value["gross_loc"], value["novel_loc"], value["doc_only_loc"]) == (7, 7, 0)
    assert value["breached"] == [] and value["under_brightline"] is True
    assert "loc_skipped" not in value


def test_doc_only_own_commits_are_not_diffed_and_novel_loc_is_zero(tmp_path, spawns):
    repo = tmp_path / "repo"
    _repo(repo, seed_floor=True)
    _git(repo, "rm", "-q", "work.py")
    _git(repo, "commit", "-q", "-m", f"drop\n\nSession-Id: {SID}\n")
    for i in range(3):
        (repo / f"note{i}.md").write_text("doc\n" * 700, encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"doc {i}\n\nSession-Id: {SID}\n")
    spawns.clear()

    envelope = qwa.brief(repo, commit=False)

    value = envelope["gates"]["close_gate"]["diff"]["value"]
    assert value["novel_loc"] == 2 and value["breached"] == []  # work.py added then dropped
    assert value["gross_loc"] is None and "doc-only commits not diffed" in value["doc_loc_skipped"]


def test_over_loc_only_fails_c2_on_loc(tmp_path, spawns):
    repo = tmp_path / "repo"
    _repo(repo, seed_floor=True)
    _more_commits(repo, 1, lines=600)
    spawns.clear()

    envelope = qwa.brief(repo, commit=False)

    assert len(_numstat_spawns(spawns)) == 1
    value = envelope["gates"]["close_gate"]["diff"]["value"]
    assert value["breached"] == ["novel_loc"]
    c2 = {c["id"]: c for c in envelope["gates"]["entry_test"]["conditions"]}["c2"]
    assert c2["passed"] is False


def test_brief_without_a_floor_makes_one_status_and_fails_condition_two_closed(tmp_path, spawns):
    repo = tmp_path / "repo"
    _repo(repo, seed_floor=False)
    spawns.clear()

    envelope = qwa.brief(repo, commit=False)

    assert [c for c in spawns if not _is_git(c)] == []
    verbs = [next(a for a in c[1:] if a in ("log", "status")) for c in spawns]
    assert verbs == ["status"], spawns
    diff = envelope["gates"]["close_gate"]["diff"]
    assert diff["degraded"] is True
    assert "floor-missing" in diff["evidence"]
    conditions = {c["id"]: c for c in envelope["gates"]["entry_test"]["conditions"]}
    assert conditions["c2"]["passed"] is False

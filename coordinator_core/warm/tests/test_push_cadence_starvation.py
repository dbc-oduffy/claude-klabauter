"""Sweep ordering, failure backoff and the commit-time mark (DR-329: the push stays on the cadence)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.warm import push_cadence, push_sweep_order, telemetry

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    push_cadence.reset_cadence_for_test()
    monkeypatch.setattr(push_cadence, "machine_profile", lambda: "author")
    svc = tmp_path / "svc"
    monkeypatch.setattr(telemetry, "svc_dir", lambda engine_root=None: svc)
    yield
    push_cadence.reset_cadence_for_test()


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _repo(tmp_path: Path, name: str, *, ahead: bool, committed_at: float = 0.0) -> Path:
    """A repo on a day branch; `ahead` adds one unpushed commit whose reflog is stamped `committed_at`."""
    bare = tmp_path / f"{name}-bare.git"
    bare.mkdir()
    _git(["init", "-q", "--bare", str(bare)], tmp_path)
    repo = tmp_path / name
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "a.txt").write_text("seed", encoding="utf-8")
    _git(["add", "--", "a.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["branch", "-m", "work/day"], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    _git(["push", "-q", "-u", "origin", "work/day"], repo)
    if ahead:
        (repo / "b.txt").write_text("more", encoding="utf-8")
        _git(["add", "--", "b.txt"], repo)
        _git(["commit", "-q", "-m", "more"], repo)
        log = repo / ".git" / "logs" / "refs" / "heads" / "work" / "day"
        os.utime(log, (committed_at, committed_at))
    return repo


def _record_sweeps(monkeypatch, results=None):
    swept: list = []

    def fake(root):
        swept.append(root.name)
        return (results or {}).get(root.name, "pushed")

    monkeypatch.setattr(push_cadence, "_sweep_one", fake)
    return swept


def _rows(tmp_path) -> list:
    path = tmp_path / "svc" / telemetry.PUSH_SWEEP_FILENAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _stamp_tracking_ref(repo: Path, stamp: float) -> None:
    log = repo / ".git" / "logs" / "refs" / "remotes" / "origin" / "work" / "day"
    os.utime(log, (stamp, stamp))


def test_repos_with_work_sweep_longest_since_pushed_first(tmp_path, monkeypatch):
    clean = _repo(tmp_path, "clean", ahead=False)
    recent = _repo(tmp_path, "recent", ahead=True, committed_at=1_000_000_000)
    stale = _repo(tmp_path, "stale", ahead=True, committed_at=1_700_000_000)
    _stamp_tracking_ref(recent, 1_700_000_000)
    _stamp_tracking_ref(stale, 1_000_000_000)
    monkeypatch.setattr(push_cadence, "_sweep_one", lambda root: "pushed")
    real_submit = push_cadence.concurrent.futures.ThreadPoolExecutor.submit
    calls: list = []

    def recording_submit(self, fn, *args, **kwargs):
        calls.append(args[0].name)
        return real_submit(self, fn, *args, **kwargs)

    monkeypatch.setattr(
        push_cadence.concurrent.futures.ThreadPoolExecutor, "submit", recording_submit
    )
    push_cadence.sweep_repos([clean, recent, stale], clock=lambda: 0.0)
    assert calls == ["stale", "recent"]  # newest commit is `stale`; the key is the remote's last word
    assert _rows(tmp_path)[0]["skipped"] == [str(clean)]


def test_thirteen_ahead_repos_all_push_at_once(tmp_path, monkeypatch):
    import threading

    repos = []
    for i in range(13):
        root = tmp_path / f"r{i}"
        root.mkdir()
        repos.append(root)
    monkeypatch.setattr(push_cadence, "is_ahead", lambda root: True)
    barrier = threading.Barrier(13, timeout=5)

    def fake(root):
        barrier.wait()
        return "pushed"

    monkeypatch.setattr(push_cadence, "_sweep_one", fake)
    ticks = iter([0.0, 7.5])
    push_cadence.sweep_repos(repos, clock=lambda: next(ticks, 7.5))
    row = _rows(tmp_path)[0]
    assert sorted(row["pushed"]) == sorted(str(r) for r in repos)
    assert row["not_reached"] == []
    assert sorted(row["ahead"]) == sorted(str(r) for r in repos)
    assert row["elapsed_secs"] == 7.5
    assert row["elapsed_secs"] <= push_cadence.SWEEP_TOTAL_CEILING_SECS


def test_a_worker_exception_is_a_failed_repo_not_a_raise(tmp_path, monkeypatch):
    good = tmp_path / "good"
    bad = tmp_path / "bad"
    good.mkdir()
    bad.mkdir()
    monkeypatch.setattr(push_cadence, "is_ahead", lambda root: True)

    def fake(root):
        if root.name == "bad":
            raise RuntimeError("boom")
        return "pushed"

    monkeypatch.setattr(push_cadence, "_sweep_one", fake)
    push_cadence.sweep_repos([good, bad], clock=lambda: 0.0)
    row = _rows(tmp_path)[0]
    assert row["pushed"] == [str(good)]
    assert row["failed"] == [str(bad)]
    assert str(bad) in {str(p) for p in push_cadence._backoff}


def test_the_sweep_threads_are_joined_before_it_returns(tmp_path, monkeypatch):
    import threading

    root = tmp_path / "r"
    root.mkdir()
    monkeypatch.setattr(push_cadence, "is_ahead", lambda root: True)
    monkeypatch.setattr(push_cadence, "_sweep_one", lambda root: "pushed")
    push_cadence.sweep_repos([root], clock=lambda: 0.0)
    assert not [t for t in threading.enumerate() if t.name.startswith("push-sweep")]


def test_work_behind_a_wall_of_clean_repos_is_reached_under_a_tight_ceiling(tmp_path, monkeypatch):
    cleans = [_repo(tmp_path, f"c{i}", ahead=False) for i in range(3)]
    busy = _repo(tmp_path, "busy", ahead=True, committed_at=1_700_000_000)
    swept = _record_sweeps(monkeypatch)
    push_cadence.sweep_repos(
        [*cleans, busy], total_ceiling_secs=20.0, per_repo_budget_secs=16.0, clock=lambda: 0.0
    )
    assert swept == ["busy"]
    assert _rows(tmp_path)[0]["not_reached"] == []


def test_consecutive_failures_back_off_with_growing_gaps(tmp_path, monkeypatch):
    bad = _repo(tmp_path, "bad", ahead=True, committed_at=1_700_000_000)
    swept = _record_sweeps(monkeypatch, {"bad": "failed"})
    attempts = []
    for _ in range(8):
        before = len(swept)
        push_cadence.sweep_repos([bad], clock=lambda: 0.0)
        attempts.append(len(swept) > before)
    # fail -> skip 1 -> fail -> skip 2 -> fail -> skip 4
    assert attempts == [True, False, True, False, False, True, False, False]
    rows = _rows(tmp_path)
    assert rows[1]["backoff"] == {str(bad): 1}
    assert str(bad) in rows[1]["skipped"]


def test_backoff_frees_the_ceiling_for_the_repo_behind_it(tmp_path, monkeypatch):
    bad = _repo(tmp_path, "bad", ahead=True, committed_at=1_700_000_100)
    good = _repo(tmp_path, "good", ahead=True, committed_at=1_700_000_000)
    swept = _record_sweeps(monkeypatch, {"bad": "failed"})
    push_cadence.sweep_repos([bad, good], clock=lambda: 0.0)
    assert sorted(swept) == ["bad", "good"]
    swept.clear()
    push_cadence.sweep_repos([bad, good], clock=lambda: 0.0)
    assert swept == ["good"]


def test_a_success_clears_the_backoff(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "r", ahead=True, committed_at=1_700_000_000)
    results = {"r": "failed"}
    swept = _record_sweeps(monkeypatch, results)
    push_cadence.sweep_repos([repo], clock=lambda: 0.0)
    results["r"] = "pushed"
    push_cadence.sweep_repos([repo], clock=lambda: 0.0)  # backed off
    push_cadence.sweep_repos([repo], clock=lambda: 0.0)  # retried, succeeds
    assert swept == ["r", "r"]
    assert push_cadence._backoff == {}


def test_note_commit_mark_is_consumed_by_the_visit(tmp_path, monkeypatch):
    older = _repo(tmp_path, "older", ahead=True, committed_at=1_000_000_000)
    newer = _repo(tmp_path, "newer", ahead=True, committed_at=1_700_000_000)
    swept = _record_sweeps(monkeypatch)
    push_cadence.note_commit(older)
    assert older in push_cadence._due_repos
    push_cadence.sweep_repos([newer, older], clock=lambda: 0.0)
    assert sorted(swept) == ["newer", "older"]
    assert push_cadence._due_repos == set()


def test_note_commit_marks_without_pushing(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "r", ahead=True, committed_at=1_700_000_000)
    swept = _record_sweeps(monkeypatch)
    push_cadence.note_commit(repo)
    assert swept == []
    assert repo in push_cadence._due_repos


def test_note_commit_lifts_a_backoff_for_one_visit(tmp_path, monkeypatch):
    repo = _repo(tmp_path, "r", ahead=True, committed_at=1_700_000_000)
    swept = _record_sweeps(monkeypatch, {"r": "failed"})
    push_cadence.sweep_repos([repo], clock=lambda: 0.0)
    push_cadence.note_commit(repo)
    push_cadence.sweep_repos([repo], clock=lambda: 0.0)
    assert swept == ["r", "r"]


def test_a_binary_file_under_refs_remotes_does_not_raise(tmp_path):
    repo = _repo(tmp_path, "r", ahead=False)
    (repo / ".git" / "refs" / "remotes" / "origin" / "._HEAD").write_bytes(b"\x00\x05\x16\x07\xb0")
    branch, sha = "work/other", "0" * 40
    assert push_sweep_order.no_upstream_and_no_new_commits(repo, branch, sha) is False
    assert isinstance(push_sweep_order.since_pushed_key(repo), float)
    assert push_sweep_order.is_ahead(repo) is False

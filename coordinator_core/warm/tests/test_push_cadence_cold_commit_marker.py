"""A cold-process commit leaves `<common-dir>/push-due`; the warm cadence marks the repo due and clears it."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.git import commit as gcommit
from coordinator_core.git.git_state import head_sha
from coordinator_core.warm import push_cadence as pc

pytestmark = [pytest.mark.spawns_process]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_ENGINE_ROOT = Path(__file__).resolve().parents[3]
_CADENCE = "coordinator_core.warm.push_cadence"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    ).stdout


def _make_repo(base: Path, branch: str, name: str = "r") -> Path:
    repo = base / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", branch)
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _marker(repo: Path) -> Path:
    return repo / ".git" / gcommit.PUSH_DUE_MARKER


@pytest.fixture(autouse=True)
def _state(monkeypatch: pytest.MonkeyPatch):
    pc.reset_cadence_for_test()
    monkeypatch.setattr(pc, "machine_profile", lambda: "author")
    yield
    pc.reset_cadence_for_test()


def _cold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, _CADENCE, raising=False)


@pytest.mark.parametrize(
    "branch,expect",
    [("work/a/b", True), ("main", False), ("feature/x", False), ("t/box/topic", True), ("other/box/topic", False)],
)
def test_cold_commit_paths_marks_only_a_work_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, branch: str, expect: bool
) -> None:
    repo = _make_repo(tmp_path, branch)
    (repo / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")
    _cold(monkeypatch)
    outcome = gcommit.commit_paths(repo_root=repo, paths=["a.txt"], message="hand")
    assert _marker(repo).exists() is expect
    if expect:
        assert _marker(repo).read_text(encoding="utf-8") == outcome.sha == head_sha(repo)
    assert list((repo / ".git").glob("push-due.*.tmp")) == []


def test_cold_commit_scoped_marks_a_work_branch_and_not_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coordinator_core.ops.ceremony import git_native

    work = _make_repo(tmp_path, "work/a/b", "w")
    main = _make_repo(tmp_path, "main", "m")
    _cold(monkeypatch)
    for repo in (work, main):
        (repo / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")
        msg = repo / "msg.txt"
        msg.write_text("hand\n", encoding="utf-8", newline="\n")
        assert git_native.commit_scoped(["a.txt"], msg, repo).ok
    assert _marker(work).read_text(encoding="utf-8") == head_sha(work)
    assert not _marker(main).exists()


def test_a_warm_process_notes_the_cadence_and_leaves_no_marker(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "work/a/b")
    (repo / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")
    assert _CADENCE in sys.modules
    gcommit.commit_paths(repo_root=repo, paths=["a.txt"], message="warm")
    assert not _marker(repo).exists()
    assert repo in pc.take_due_repos(lambda: 0.0)


def test_a_marker_write_failure_never_fails_the_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path, "work/a/b")
    (repo / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")
    _cold(monkeypatch)
    real_replace = os.replace

    def boom(src, dst):
        if Path(dst).name == gcommit.PUSH_DUE_MARKER:
            raise PermissionError("held")
        return real_replace(src, dst)

    monkeypatch.setattr(gcommit.os, "replace", boom)
    outcome = gcommit.commit_paths(repo_root=repo, paths=["a.txt"], message="hand")
    assert outcome.sha == head_sha(repo)
    assert not _marker(repo).exists()
    assert list((repo / ".git").glob("push-due.*.tmp")) == []


_CHILD = """
import sys, json
from pathlib import Path
repo = Path(sys.argv[2])
if sys.argv[1] == "commit_scoped":
    from coordinator_core.ops.ceremony import git_native
    msg = repo / "msg.txt"
    msg.write_text("hand\\n", encoding="utf-8", newline="\\n")
    ok = git_native.commit_scoped(["a.txt"], msg, repo).ok
else:
    from coordinator_core.git import commit as gcommit
    ok = bool(gcommit.commit_paths(repo_root=repo, paths=["a.txt"], message="hand").sha)
print(json.dumps({"ok": ok, "cadence_loaded": "coordinator_core.warm.push_cadence" in sys.modules}))
"""


@pytest.mark.parametrize("route", ["commit_scoped", "commit_paths"])
def test_cold_process_hand_commit_leaves_the_marker_the_warm_tick_sweeps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    repo = _make_repo(tmp_path, "work/a/b")
    (repo / "a.txt").write_text("a\n", encoding="utf-8", newline="\n")
    env = {**os.environ, "PYTHONPATH": str(_ENGINE_ROOT), "PYTHONIOENCODING": "utf-8"}
    run = subprocess.run(
        [sys.executable, "-c", _CHILD, route, str(repo)],
        cwd=str(_ENGINE_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        **_NOWIN,
    )
    assert run.returncode == 0, run.stderr
    report = json.loads(run.stdout.strip().splitlines()[-1])
    assert report == {"ok": True, "cadence_loaded": False}
    assert _marker(repo).read_text(encoding="utf-8") == _git(repo, "rev-parse", "HEAD").strip()

    pc.reset_cadence_for_test()
    monkeypatch.setattr(pc, "registry_sweep_repos", lambda served: [repo])
    calls: list = []
    clock = [0.0]

    def tick() -> bool:
        return pc.on_idle_tick(
            served_repos=lambda: [],
            clock=lambda: clock[0],
            interval_secs=600.0,
            sweep_fn=lambda repos, **kw: calls.append((list(repos), kw)),
        )

    tick()
    assert calls[0][0] == [repo] and calls[0][1]["due_only"] is True


def test_idle_tick_without_a_marker_sweeps_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path, "work/a/b")
    monkeypatch.setattr(pc, "registry_sweep_repos", lambda served: [repo])
    calls: list = []
    pc.on_idle_tick(
        served_repos=lambda: [],
        clock=lambda: 0.0,
        interval_secs=600.0,
        sweep_fn=lambda repos, **kw: calls.append(repos),
    )
    assert calls == []


def _sweep_unadmitted(repo: Path, monkeypatch: pytest.MonkeyPatch, now: float) -> None:
    monkeypatch.setattr(pc, "_backed_off_now", lambda r: False)
    monkeypatch.setattr(pc, "is_ahead", lambda r: False)
    pc.sweep_repos([repo], due_only=True, clock=lambda: now)


def test_marker_on_an_unadmitted_repo_is_removed_and_not_remarked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path, "work/a/b")
    gcommit.write_due_marker(repo / ".git", "a" * 40)
    assert pc.mark_due_markers([repo]) == [repo]
    _sweep_unadmitted(repo, monkeypatch, 100.0)
    assert not _marker(repo).exists()
    assert pc.mark_due_markers([repo]) == []
    assert pc.take_due_repos(lambda: 1000.0) == []


def test_marker_on_an_admitted_repo_is_removed_after_the_visit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path, "work/a/b")
    gcommit.write_due_marker(repo / ".git", "a" * 40)
    monkeypatch.setattr(pc, "_backed_off_now", lambda r: False)
    monkeypatch.setattr(pc, "is_ahead", lambda r: True)
    monkeypatch.setattr(pc, "_sweep_one", lambda r, *a: "pushed")
    pc.sweep_repos([repo], due_only=True, per_repo_budget_secs=1.0, clock=lambda: 100.0)
    assert not _marker(repo).exists()


def test_marker_rewritten_mid_sweep_survives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _make_repo(tmp_path, "work/a/b")
    gcommit.write_due_marker(repo / ".git", "a" * 40)
    monkeypatch.setattr(pc, "_backed_off_now", lambda r: False)
    monkeypatch.setattr(pc, "is_ahead", lambda r: True)

    def landing_commit(r, *a):
        gcommit.write_due_marker(repo / ".git", "b" * 40)
        return "pushed"

    monkeypatch.setattr(pc, "_sweep_one", landing_commit)
    pc.sweep_repos([repo], due_only=True, per_repo_budget_secs=1.0, clock=lambda: 100.0)
    assert _marker(repo).read_text(encoding="utf-8") == "b" * 40


def test_marker_scan_over_fifty_markerless_repos_spawns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repos = []
    for i in range(50):
        repo = tmp_path / f"r{i}"
        (repo / ".git").mkdir(parents=True)
        repos.append(repo)

    def no_spawn(*args, **kwargs):
        raise AssertionError("marker scan spawned a process")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    assert pc.mark_due_markers(repos) == []


_COLD_COMMIT_SCRIPT = """\
import sys
from coordinator_core.git.commit import commit_paths
commit_paths(sys.argv[1], ["a.py"], "hand commit")
print("COLD" if "coordinator_core.warm.push_cadence" not in sys.modules else "WARM")
"""


@pytest.mark.cadence
def test_hand_commit_in_a_cold_process_is_marked_due_by_the_warm_head_scan(tmp_path: Path) -> None:
    repo = tmp_path / "hand"
    repo.mkdir()
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(tmp_path))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env, creationflags=flags)

    git("init", "-q")
    git("config", "user.email", "t@t.example")
    git("config", "user.name", "t")
    git("config", "core.autocrlf", "false")
    (repo / "a.py").write_bytes(b"one\n")
    git("add", "--", "a.py")
    git("commit", "-q", "-m", "seed")

    pc.reset_cadence_for_test()
    assert pc.mark_moved_heads([repo], clock=lambda: 0.0) == []

    script = tmp_path / "cold_commit.py"
    script.write_text(_COLD_COMMIT_SCRIPT, encoding="utf-8")
    (repo / "a.py").write_bytes(b"one\ntwo\n")
    engine_root = str(Path(pc.__file__).resolve().parents[2])
    run = subprocess.run(
        [sys.executable, str(script), str(repo)],
        capture_output=True,
        text=True,
        creationflags=flags,
        env=dict(env, PYTHONPATH=engine_root),
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "COLD"
    assert pc.take_due_repos(lambda: 5.0) == []

    assert pc.mark_moved_heads([repo], clock=lambda: 20.0) == [repo]
    assert pc.take_due_repos(lambda: 20.0) == [repo]
    pc.reset_cadence_for_test()

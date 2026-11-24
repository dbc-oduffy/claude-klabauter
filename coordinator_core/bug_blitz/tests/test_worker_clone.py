"""worker_clone: provision/clone_root/carried_blobs/reap against a real throwaway repo."""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from coordinator_core.bug_blitz import worker_clone
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=cwd, capture_output=True, text=True, check=True, **no_console_creationflags(),
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.txt").write_bytes(b"alpha\r\n")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_bytes(b"beta\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _dead(_root: Path, _run_id: str) -> bool:
    return False


def test_provision_clones_head_and_records_base(repo: Path) -> None:
    clone = worker_clone.provision(repo, "run-1", is_live=_dead)
    assert clone.is_dir() and clone != repo
    assert (clone / "a.txt").read_bytes() == b"alpha\r\n"
    assert (clone / "sub" / "b.txt").read_bytes().replace(b"\r\n", b"\n") == b"beta\n"
    head = _git(repo, "rev-parse", "HEAD")
    assert (clone / ".git" / "blitz-base").read_text().strip() == head
    assert worker_clone.base_sha(clone) == head
    assert worker_clone.clone_root(repo, "run-1") == clone
    assert clone.parent.name == "bug-blitz-clones"


def test_main_checkout_untouched_by_clone_edits(repo: Path) -> None:
    clone = worker_clone.provision(repo, "run-1", is_live=_dead)
    (clone / "a.txt").write_bytes(b"edited\n")
    assert (repo / "a.txt").read_bytes() == b"alpha\r\n"
    assert _git(repo, "status", "--porcelain") == ""


def test_provision_idempotent_keeps_edits(repo: Path) -> None:
    first = worker_clone.provision(repo, "run-1", is_live=_dead)
    (first / "a.txt").write_bytes(b"edited\n")
    second = worker_clone.provision(repo, "run-1", is_live=_dead)
    assert second == first
    assert (second / "a.txt").read_bytes() == b"edited\n"


def test_provision_rebuilds_a_partial_clone(repo: Path) -> None:
    clone = worker_clone.provision(repo, "run-1", is_live=_dead)
    (clone / ".git" / "blitz-base").unlink()
    assert worker_clone.clone_root(repo, "run-1") is None
    again = worker_clone.provision(repo, "run-1", is_live=_dead)
    assert worker_clone.clone_root(repo, "run-1") == again


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\\b", "x y", "../escape", "a:b"])
def test_unsafe_run_id_is_a_typed_refusal_and_creates_nothing(repo: Path, bad: str) -> None:
    with pytest.raises(worker_clone.InvalidRunId) as exc:
        worker_clone.provision(repo, bad, is_live=_dead)
    assert repr(bad) in str(exc.value)
    assert not worker_clone._clones_dir(repo).exists()
    assert worker_clone.clone_root(repo, bad) is None
    with pytest.raises(worker_clone.InvalidRunId):
        worker_clone.reap(repo, bad)


def test_carried_blobs_reads_bytes_and_marks_deletion(repo: Path) -> None:
    clone = worker_clone.provision(repo, "run-1", is_live=_dead)
    (clone / "a.txt").write_bytes(b"new\r\nbytes\x00")
    (clone / "sub" / "b.txt").unlink()
    (clone / "new.txt").write_bytes(b"fresh")
    got = worker_clone.carried_blobs(clone, ["a.txt", "sub/b.txt", "new.txt", "never.txt"])
    assert got == {"a.txt": b"new\r\nbytes\x00", "sub/b.txt": None, "new.txt": b"fresh", "never.txt": None}


# The absolute case resolves inside the test: a per-session temp path in a parametrize id makes xdist
# workers collect different test ids and abort the whole run.
@pytest.mark.parametrize("bad", ["../x", "sub/../../x", "ABS"])
def test_carried_blobs_refuses_paths_outside_the_clone(repo: Path, bad: str) -> None:
    if bad == "ABS":
        bad = str(Path(tempfile.gettempdir()).resolve() / "x")
    clone = worker_clone.provision(repo, "run-1", is_live=_dead)
    with pytest.raises(worker_clone.WorkerCloneError):
        worker_clone.carried_blobs(clone, [bad])


def test_reap_removes_the_clone_and_is_idempotent(repo: Path) -> None:
    clone = worker_clone.provision(repo, "run-1", is_live=_dead)
    assert worker_clone.reap(repo, "run-1") is True
    assert not clone.exists()
    assert worker_clone.clone_root(repo, "run-1") is None
    assert worker_clone.reap(repo, "run-1") is False


def _age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_provision_reaps_dead_sibling_clone_but_keeps_live_and_fresh(repo: Path) -> None:
    dead = worker_clone.provision(repo, "run-dead", is_live=_dead)
    live = worker_clone.provision(repo, "run-live", is_live=_dead)
    fresh = worker_clone.provision(repo, "run-fresh", is_live=_dead)
    _age(dead, 7200)
    _age(live, 7200)
    own = worker_clone.provision(repo, "run-own", is_live=lambda _r, rid: rid == "run-live")
    assert not dead.exists()
    assert live.is_dir()
    assert fresh.is_dir()
    assert own.is_dir()


def _stage_record(repo: Path, run_id: str, stage: str, session_id: str = "s1") -> None:
    import json

    from coordinator_core.session import record_homes

    rec = Path(record_homes.home_dir(str(repo), "warp")) / run_id
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "run-stage.json").write_text(json.dumps({"stage": stage, "session_id": session_id}), encoding="utf-8")


def test_default_liveness_keeps_an_old_clone_with_no_stage_record_and_reaps_ended_or_orphaned(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from coordinator_core.warm import live_runs

    monkeypatch.setattr(live_runs, "_owner_live", lambda sid, memo=None: sid == "alive")
    clones = {rid: worker_clone.provision(repo, rid, is_live=_dead) for rid in ("no-rec", "ended", "orphan", "running")}
    _stage_record(repo, "ended", "halted", "alive")
    _stage_record(repo, "orphan", "staged", "gone")
    _stage_record(repo, "running", "staged", "alive")
    for c in clones.values():
        _age(c, 7200)
    worker_clone.provision(repo, "run-own")
    assert clones["no-rec"].is_dir()
    assert clones["running"].is_dir()
    assert not clones["ended"].exists()
    assert not clones["orphan"].exists()


def test_sibling_sweep_spawns_no_git(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dead = worker_clone.provision(repo, "run-dead", is_live=_dead)
    _age(dead, 7200)
    calls: list = []
    real = worker_clone.run_git
    monkeypatch.setattr(worker_clone, "run_git", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    swept = worker_clone._reap_dead_siblings(repo, "run-own", _dead, worker_clone.FRESH_S)
    assert swept == ["run-dead"] and calls == []


def test_provision_spawn_count_and_process_time(repo: Path, monkeypatch: pytest.MonkeyPatch, record_property) -> None:
    calls: list = []
    real = worker_clone.run_git
    monkeypatch.setattr(worker_clone, "run_git", lambda *a, **k: (calls.append(a[0][0]), real(*a, **k))[1])
    t0 = time.process_time()
    clone = worker_clone.provision(repo, "run-1", is_live=_dead)
    record_property("provision_parent_process_s", round(time.process_time() - t0, 4))
    record_property("provision_git_spawns", len(calls))
    assert (clone / "a.txt").is_file()
    assert calls == ["rev-parse", "clone", "checkout"]


def _write_cone(repo: Path, run_id: str, text: str) -> None:
    d = worker_clone.run_dir(repo, run_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / worker_clone.CONE_FILE).write_text(text, encoding="utf-8")


def test_read_cone_absent_is_empty_and_entries_are_normalised(tmp_path: Path) -> None:
    assert worker_clone.read_cone(tmp_path) == ()
    (tmp_path / worker_clone.CONE_FILE).write_text("\nsub/\r\n pkg\\inner \nsub\n\n", encoding="utf-8")
    assert worker_clone.read_cone(tmp_path) == ("sub", "pkg/inner")


@pytest.mark.parametrize("bad", ["../x", "a/../b", "-flag", "/abs", "a:b"])
def test_read_cone_refuses_an_entry_outside_the_repo(tmp_path: Path, bad: str) -> None:
    (tmp_path / worker_clone.CONE_FILE).write_text(f"sub\n{bad}\n", encoding="utf-8")
    with pytest.raises(worker_clone.WorkerCloneError, match="repo-relative"):
        worker_clone.read_cone(tmp_path)


def test_provision_with_a_cone_checks_out_only_the_cone_and_root_files(repo: Path) -> None:
    (repo / "other").mkdir()
    (repo / "other" / "c.txt").write_bytes(b"gamma\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "more")
    _write_cone(repo, "run-1", "sub\n")
    clone = worker_clone.provision(repo, "run-1", is_live=_dead)
    assert (clone / "sub" / "b.txt").is_file()
    assert (clone / "a.txt").is_file()  # root files are always in a cone
    assert not (clone / "other").exists()
    assert worker_clone.base_sha(clone) == _git(repo, "rev-parse", "HEAD")


def test_provision_with_an_empty_cone_file_checks_out_everything(repo: Path) -> None:
    _write_cone(repo, "run-1", "\n")
    clone = worker_clone.provision(repo, "run-1", is_live=_dead)
    assert (clone / "sub" / "b.txt").is_file() and (clone / "a.txt").is_file()


def test_provision_with_a_cone_adds_one_sparse_checkout_spawn_and_no_timeout(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_cone(repo, "run-1", "sub\n")
    calls: list = []
    real = worker_clone.run_git
    monkeypatch.setattr(worker_clone, "run_git", lambda *a, **k: (calls.append((a[0][0], sorted(k))), real(*a, **k))[1])
    worker_clone.provision(repo, "run-1", is_live=_dead)
    assert [c[0] for c in calls] == ["rev-parse", "clone", "sparse-checkout", "checkout"]
    assert all("timeout" not in c[1] for c in calls)


def test_a_failed_sparse_checkout_leaves_no_clone(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_cone(repo, "run-1", "sub\n")
    real = worker_clone.run_git

    def fail(argv, **kw):
        if argv[0] == "sparse-checkout":
            return type("R", (), {"returncode": 1, "timed_out": False, "stderr": "boom", "stdout": ""})()
        return real(argv, **kw)

    monkeypatch.setattr(worker_clone, "run_git", fail)
    with pytest.raises(worker_clone.WorkerCloneError, match="sparse-checkout of sub failed: boom"):
        worker_clone.provision(repo, "run-1", is_live=_dead)
    assert not (worker_clone._clones_dir(repo) / "run-1").exists()

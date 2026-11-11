"""Background engine convergence: a clean behind clone is fast-forwarded and its
stamp changes; a dirty clone is refused with the reason recorded; scheduling
returns without running git."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from coordinator_core.warm import engine_converge, push_cadence
from coordinator_core.warm.skew import ENGINE_STAMP_FILENAME, compute_client_token

_REPO_LIB = Path(__file__).resolve().parents[3] / "coordinator" / "lib"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.stdout.strip()


def _stamp(root: Path) -> Path:
    return root / "coordinator_core" / ENGINE_STAMP_FILENAME


def _commit_stamp(root: Path, sha: str) -> None:
    _stamp(root).parent.mkdir(parents=True, exist_ok=True)
    _stamp(root).write_text(f"sha:{sha}\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"stamp {sha}")


def test_a_convergence_pass_signals_no_process(tmp_path, monkeypatch):
    from coordinator_core.warm import pair_stop

    def _forbidden(*a, **k):
        raise AssertionError("convergence must not stop or reap any process")

    for name in ("stop_warm_pair", "reap_pairs_outside", "_terminate_pid", "_terminate_with_reason"):
        monkeypatch.setattr(pair_stop, name, _forbidden)
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)

    class _Result:
        ok = True
        fast_forwarded = True

    payload = engine_converge.run_convergence_pass(
        clone=clone, fast_forward=lambda c: _Result(), record_to=tmp_path / "rec.json"
    )
    assert payload["outcome"] == "fast_forwarded"


@pytest.fixture
def clone_behind(tmp_path):
    """(clone, peer): clone sits one publish behind origin."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "candidate", str(origin))
    peer = tmp_path / "peer"
    _git(tmp_path, "clone", "-q", str(origin), str(peer))
    _git(peer, "checkout", "-q", "-b", "candidate")
    (peer / "seed.txt").write_text("seed", encoding="utf-8")
    _commit_stamp(peer, "aaaaaaa")
    _git(peer, "push", "-q", "-u", "origin", "candidate")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "-b", "candidate", str(origin), str(clone))
    _commit_stamp(peer, "bbbbbbb")
    _git(peer, "push", "-q", "origin", "candidate")
    return clone, peer


def _ff(clone: Path):
    if str(_REPO_LIB) not in sys.path:
        sys.path.insert(0, str(_REPO_LIB))
    from percolate.dest_refresh import fast_forward_consumer

    return fast_forward_consumer(clone)


def test_behind_clean_clone_is_fast_forwarded_and_stamp_changes(clone_behind, tmp_path):
    clone, peer = clone_behind
    before = compute_client_token(clone)
    record = tmp_path / "rec" / "engine-converge.json"

    payload = engine_converge.run_convergence_pass(
        clone=clone, fast_forward=_ff, record_to=record
    )

    assert payload["outcome"] == "fast_forwarded"
    assert _git(clone, "rev-list", "--count", "HEAD..origin/candidate") == "0"
    assert _git(clone, "rev-parse", "HEAD") == _git(peer, "rev-parse", "HEAD")
    assert compute_client_token(clone) != before
    on_disk = json.loads(record.read_text(encoding="utf-8"))
    assert on_disk["outcome"] == "fast_forwarded" and on_disk["behind"] == 0


def test_dirty_clone_is_refused_and_head_does_not_move(clone_behind, tmp_path):
    clone, _peer = clone_behind
    head = _git(clone, "rev-parse", "HEAD")
    _stamp(clone).write_text("sha:handedit\n", encoding="utf-8")
    record = tmp_path / "engine-converge.json"

    payload = engine_converge.run_convergence_pass(
        clone=clone, fast_forward=_ff, record_to=record
    )

    assert payload["outcome"] == "refused_dirty"
    assert payload["ok"] is False and payload["dirty"] is True
    assert "modified tracked" in payload["reason"]
    assert _git(clone, "rev-parse", "HEAD") == head
    assert _stamp(clone).read_text(encoding="utf-8") == "sha:handedit\n"
    assert json.loads(record.read_text(encoding="utf-8"))["outcome"] == "refused_dirty"


def _default_ff(clone: Path):
    if str(_REPO_LIB) not in sys.path:
        sys.path.insert(0, str(_REPO_LIB))
    return engine_converge._default_fast_forward(clone)


def test_a_dirty_engine_stamp_alone_does_not_block_the_default_convergence(clone_behind, tmp_path):
    clone, peer = clone_behind
    _stamp(clone).write_text("sha:handedit\n", encoding="utf-8")

    payload = engine_converge.run_convergence_pass(
        clone=clone, fast_forward=_default_ff, record_to=tmp_path / "r.json"
    )

    assert payload["outcome"] == "fast_forwarded"
    assert _git(clone, "rev-parse", "HEAD") == _git(peer, "rev-parse", "HEAD")
    assert _stamp(clone).read_text(encoding="utf-8") == "sha:bbbbbbb\n"


def test_other_dirt_beside_the_stamp_still_refuses_the_default_convergence(clone_behind, tmp_path):
    clone, _peer = clone_behind
    head = _git(clone, "rev-parse", "HEAD")
    _stamp(clone).write_text("sha:handedit\n", encoding="utf-8")
    (clone / "seed.txt").write_text("edited", encoding="utf-8")

    payload = engine_converge.run_convergence_pass(
        clone=clone, fast_forward=_default_ff, record_to=tmp_path / "r.json"
    )

    assert payload["outcome"] == "refused_dirty"
    assert _stamp(clone).read_text(encoding="utf-8") == "sha:handedit\n"
    assert _git(clone, "rev-parse", "HEAD") == head


def test_a_held_publish_marker_leaves_the_dirty_stamp_untouched(clone_behind, tmp_path):
    from coordinator_core.warm import breadcrumb

    clone, _peer = clone_behind
    head = _git(clone, "rev-parse", "HEAD")
    _stamp(clone).write_text("sha:live-token\n", encoding="utf-8")
    live = _stamp(clone).read_bytes()

    with breadcrumb.hold_publish_marker(clone, holder="t"):
        payload = engine_converge.run_convergence_pass(
            clone=clone, fast_forward=_default_ff, record_to=tmp_path / "r.json"
        )

    assert payload["ok"] is False and payload["fast_forwarded"] is False
    assert _stamp(clone).read_bytes() == live
    assert _git(clone, "rev-parse", "HEAD") == head


def test_diverged_clone_is_refused(clone_behind, tmp_path):
    clone, _peer = clone_behind
    (clone / "local.txt").write_text("x", encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "local")
    head = _git(clone, "rev-parse", "HEAD")

    payload = engine_converge.run_convergence_pass(
        clone=clone, fast_forward=_ff, record_to=tmp_path / "r.json"
    )

    assert payload["outcome"] == "refused_diverged"
    assert _git(clone, "rev-parse", "HEAD") == head


def test_non_git_clone_is_recorded_not_raised(tmp_path):
    payload = engine_converge.run_convergence_pass(
        clone=tmp_path / "nogit", fast_forward=lambda c: pytest.fail("no git"),
        record_to=tmp_path / "r.json",
    )
    assert payload["outcome"] == "not_a_git_clone"


def test_default_clone_resolves_through_skew(monkeypatch, tmp_path):
    from coordinator_core.warm import skew

    monkeypatch.setattr(skew, "_default_engine_clone", lambda: tmp_path / "resolved")
    payload = engine_converge.run_convergence_pass(record_to=tmp_path / "r.json")
    assert payload["clone"] == str(tmp_path / "resolved")


@pytest.fixture(autouse=True)
def _reset():
    engine_converge.reset_converge_for_test()
    push_cadence.reset_cadence_for_test()
    yield
    engine_converge.reset_converge_for_test()


def test_scheduling_returns_without_running_the_pass():
    gate = threading.Event()
    ran = threading.Event()

    def blocked_pass():
        ran.set()
        gate.wait(5)

    started: list = []
    try:
        ok = engine_converge.schedule_convergence(
            pass_fn=blocked_pass, start=lambda t: (started.append(t), t.start())
        )
        assert ok is True
        assert ran.wait(2)  # the pass is running on its own thread...
        # ...and a second schedule neither blocks on it nor starts another.
        assert engine_converge.schedule_convergence(pass_fn=blocked_pass) is False
    finally:
        gate.set()
        started[0].join(2)


def test_schedule_is_interval_gated_and_first_call_is_due():
    clock = [0.0]
    calls: list = []

    def run_inline(t):
        t.start()
        t.join(2)

    kwargs = dict(clock=lambda: clock[0], interval_secs=600.0, pass_fn=lambda: calls.append(1), start=run_inline)
    assert engine_converge.schedule_convergence(**kwargs) is True
    clock[0] = 599.0
    assert engine_converge.schedule_convergence(**kwargs) is False
    clock[0] = 601.0
    assert engine_converge.schedule_convergence(**kwargs) is True
    assert len(calls) == 2


def test_default_schedule_is_inert_under_pytest():
    assert engine_converge.schedule_convergence() is False


def test_idle_tick_schedules_convergence(monkeypatch):
    seen: list = []
    monkeypatch.setattr(engine_converge, "schedule_convergence", lambda: seen.append(1) or True)
    push_cadence.on_idle_tick(served_repos=lambda: [], sweep_fn=lambda *a, **k: None)
    assert seen == [1]


def test_idle_tick_survives_a_scheduler_failure(monkeypatch):
    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(engine_converge, "schedule_convergence", boom)
    assert push_cadence.on_idle_tick(served_repos=lambda: [], sweep_fn=lambda *a, **k: None) is False

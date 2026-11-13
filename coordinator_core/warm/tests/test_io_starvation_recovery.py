"""IO-starvation recovery, end to end through the default entries.

Each scenario freezes the filesystem leg the way `test_wedge_fs_freeze_hooks.py` does (the stubbed
call holds on an Event, nothing sleeps), shows the engine's response while it is frozen, then
releases it and shows recovery:

1. a hung engine is restarted with a growing backoff (`hang_restart.note_stall` / `maybe_restart`,
   and the client's `try_warm_dispatch` unanswered-connect path), never in a storm;
2. a finished run whose landing hits the commit-leg deadline is queued by `land_run`, re-queued by
   the idle tick while IO is still starved, and landed by the idle tick once IO recovers;
3. the ask-spool drain mints no entry without a run, and mints it once the run exists.

All paths are under `tmp_path`; no live engine is touched.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.ask_spool import drain, record
from coordinator_core.ops.dispatch_emit import land_deadline, land_resume, land_run
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import (  # noqa: F401
    RUN,
    _author,
    _declare,
    _git,
    _land,
    _record_dir,
    _tree,
    repo,
)
from coordinator_core.warm import breadcrumb, client, hang_restart, idle, lifecycle, server
from coordinator_core.warm.engine_root import current_engine_clone

HOLD_CEILING_SECS = 30
_PID = 424242
_TOKEN = "faketoken"
_STAMP = "%Y%m%dT%H%M%SZ"
_LINE_STAMP = "%Y-%m-%dT%H:%M:%SZ"


class _FsFreeze:
    """A frozen filesystem call: while `frozen`, `hold` blocks on `release`; `thaw` is recovery."""

    def __init__(self) -> None:
        self.frozen = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def freeze(self) -> None:
        self.frozen = True
        self.entered.clear()
        self.release.clear()

    def thaw(self) -> None:
        self.frozen = False
        self.release.set()

    def hold(self) -> None:
        if self.frozen:
            self.entered.set()
            self.release.wait(HOLD_CEILING_SECS)


# ---- scenario 1: a hung engine restarts with backoff ------------------------------------------


@pytest.fixture
def engine(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(tmp_path))
    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: _TOKEN)
    monkeypatch.setattr(client.election, "pipe_name", lambda token: r"\\.\pipe\fake")
    monkeypatch.setattr(client, "_spawned_this_process", False)
    monkeypatch.setattr(client, "_live_tree_cold", False)
    monkeypatch.setattr(client, "_cold_reason", None)
    monkeypatch.setattr(client, "READ_DEADLINE_SECS", 0.02)
    monkeypatch.setattr(client, "_mutation_deadline_for", lambda method: 0.04)
    monkeypatch.setattr(breadcrumb, "stable_pid_alive", lambda *a, **k: True)
    svc = breadcrumb.svc_dir(current_engine_clone())
    svc.mkdir(parents=True)
    (svc / breadcrumb.BREADCRUMB_FILENAME).write_text(
        json.dumps({"pipe": "p", "pid": _PID, "stable_pid_start_epoch": 1}), encoding="utf-8"
    )
    (svc / "stalls").mkdir()
    killed: list = []
    monkeypatch.setattr(hang_restart, "exit_server", killed.append)
    with hang_restart._stall_marks_lock:
        hang_restart._stall_marks.clear()
    return svc, killed


def _log_lines(svc: Path) -> list:
    path = svc / hang_restart.HANG_RESTART_LOG_FILENAME
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _restart_backoffs(svc: Path) -> list:
    return [
        float(line.split(" backoff_s=", 1)[1].split(" ", 1)[0])
        for line in _log_lines(svc)
        if " action=restart " in line + " "
    ]


def _last_restart_epoch(svc: Path) -> int:
    epoch, _line = hang_restart._last_line(_log_lines(svc), restart=True)
    assert epoch is not None
    return epoch


def _age_log(svc: Path, secs: int) -> None:
    """Move every log line `secs` into the past: the wall time `secs` ago the restarts happened."""
    path = svc / hang_restart.HANG_RESTART_LOG_FILENAME
    aged = []
    for line in _log_lines(svc):
        _stamp_text, rest = line.split(" ", 1)
        epoch = hang_restart._line_epoch(line)
        aged.append(time.strftime(_LINE_STAMP, time.gmtime(epoch - secs)) + " " + rest)
    path.write_text("\n".join(aged) + "\n", encoding="utf-8")


def _burst(stall_dir: Path, **kwargs):
    """The server's stall detector firing `HANG_DUMP_THRESHOLD` times; the last call's verdict."""
    verdict = None
    for _ in range(hang_restart.HANG_DUMP_THRESHOLD):
        verdict = hang_restart.note_stall(stall_dir, server_pid=_PID, **kwargs)
    return verdict


def _dump_attempt(stall_dir: Path, now: float, serial: int, killed: list):
    """`maybe_restart` over a fresh set of hung-stall records stamped just before `now`."""
    stamp = time.strftime(_STAMP, time.gmtime(int(now) - 5))
    for i in range(hang_restart.HANG_DUMP_THRESHOLD):
        (stall_dir / f"{stamp}-{serial}-{i}.txt").write_text("stall\n", encoding="utf-8")
    return hang_restart.maybe_restart(stall_dir, server_pid=_PID, now=now, exit_fn=killed.append)


def _unanswered_calls(monkeypatch, count: int) -> None:
    def _raise(name):
        raise TimeoutError

    monkeypatch.setattr(client, "_open_pipe", _raise)
    for _ in range(count):
        client.try_warm_dispatch({"jsonrpc": "2.0", "id": 1, "method": "review.prep", "params": {}})


def test_a_hung_engine_restarts_with_a_growing_backoff_and_a_quiet_window_resets_it(engine, monkeypatch):
    svc, killed = engine
    stall_dir = svc / "stalls"

    # IO starved: the stall watcher's own log read is frozen, so the decision leg never finishes.
    # The stalled disk restarts the engine anyway (it cannot prove a hold or a backoff).
    freeze = _FsFreeze()
    real_tail = hang_restart._tail_lines

    def frozen_tail(log):
        freeze.hold()
        return real_tail(log)

    monkeypatch.setattr(hang_restart, "_tail_lines", frozen_tail)
    freeze.freeze()
    try:
        assert _burst(stall_dir, deadline=0.05) == "restart"
        assert freeze.entered.wait(HOLD_CEILING_SECS)
        assert killed == [_PID]
    finally:
        freeze.thaw()
    for thread in threading.enumerate():
        if thread.name == "hang-restart-decision":
            thread.join(HOLD_CEILING_SECS)
    assert _restart_backoffs(svc) == [0.0]

    # IO recovered, the replacement stalls again at once: a second restart inside the backoff is refused.
    assert _burst(stall_dir) == "suppressed"
    assert killed == [_PID]
    assert "reason=backoff" in _log_lines(svc)[-1] and "backoff_s=90" in _log_lines(svc)[-1]

    # The callers' side: K unanswered connects to the live pid ask for a restart and get the same refusal.
    _unanswered_calls(monkeypatch, client.UNANSWERED_RESTART_K)
    assert killed == [_PID]
    assert "source=client-unanswering" in _log_lines(svc)[-1] and "reason=backoff" in _log_lines(svc)[-1]

    # The backoff elapses: the next unanswered call restarts the engine through the client.
    _age_log(svc, int(hang_restart.RESTART_MIN_INTERVAL_SECS) + 10)
    _unanswered_calls(monkeypatch, 1)
    assert killed == [_PID, _PID]
    assert _restart_backoffs(svc) == [0.0, 90.0]

    # Each consecutive restart owes twice the last, up to the cap; one second short is refused.
    base = hang_restart.RESTART_MIN_INTERVAL_SECS
    serial = 0
    for owed in (180.0, 360.0, 720.0, 900.0, 900.0):
        serial += 1
        last = _last_restart_epoch(svc)
        before = len(killed)
        assert _dump_attempt(stall_dir, last + owed - 1, serial, killed) == "suppressed"
        assert len(killed) == before
        serial += 1
        assert _dump_attempt(stall_dir, last + owed + 1, serial, killed) == "restart"
        assert len(killed) == before + 1
    assert _restart_backoffs(svc) == [0.0, base, 180.0, 360.0, 720.0, 900.0, 900.0]

    # A quiet window longer than the cap ends the chain: the next restart is a first one again.
    serial += 1
    quiet = _last_restart_epoch(svc) + hang_restart.RESTART_QUIET_WINDOW_SECS + 1
    assert _dump_attempt(stall_dir, quiet, serial, killed) == "restart"
    serial += 1
    assert _restart_backoffs(svc)[-1] == 0.0
    assert _dump_attempt(stall_dir, quiet + base - 1, serial, killed) == "suppressed"


# ---- scenario 2: a finished run queued at commit lands after IO recovers ----------------------


class _FakeVersionState:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


@pytest.fixture
def ctx(tmp_path, repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()
    monkeypatch.setattr(idle, "should_demote", lambda **kwargs: False)
    monkeypatch.setattr(idle, "demote_if_idle", lambda **kwargs: None)
    monkeypatch.setattr(breadcrumb, "svc_dir", lambda root=None: tmp_path / "svc")
    monkeypatch.setattr(server._ServerContext, "served_repos", lambda self: [repo])
    yield server._ServerContext(
        name="pipe-starved", sid="sid-starved", version_state=_FakeVersionState(), engine_root=tmp_path
    )
    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()


def _starve_commit_leg(monkeypatch, freeze: _FsFreeze) -> None:
    """While frozen, the commit leg's first fs call blocks and burns the whole land budget, so the
    real `land_deadline.check` ends the landing there; thawed, the leg runs normally."""
    skew = [0.0]
    real_now, real_check = land_deadline._now, land_deadline.check
    monkeypatch.setattr(land_deadline, "_now", lambda: real_now() + skew[0])

    def check(name):
        if name == "land_run.commit" and freeze.frozen:
            freeze.entered.set()
            skew[0] += land_deadline.LAND_BUDGET_SECS + land_deadline.RESERVE_SECS + 1.0
        real_check(name)

    monkeypatch.setattr(land_deadline, "check", check)


def _drain_land_thread(ctx) -> None:
    thread = ctx.__dict__.get("_land_resume_thread")
    if thread is not None:
        thread.join(HOLD_CEILING_SECS)
        assert not thread.is_alive()


def _quiet_unrelated_idle_legs(monkeypatch) -> None:
    """The tick's other legs (autofire, atlas catch-up) are not under test and walk the served repo."""
    import importlib

    for name in ("coordinator_core.distill.autofire", "coordinator_core.cartography.atlas_catchup"):
        monkeypatch.setattr(importlib.import_module(name), "on_idle_tick", lambda repos: False)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_a_run_queued_at_the_commit_leg_lands_on_the_idle_tick_after_io_recovers(ctx, repo, monkeypatch):
    _quiet_unrelated_idle_legs(monkeypatch)
    _author(repo, "src/a.py", "a = 1\n")
    _declare(repo, "src/a.py")
    record_paths = _record_dir(repo)
    head = _git(["rev-parse", "HEAD"], repo).strip()
    freeze = _FsFreeze()
    _starve_commit_leg(monkeypatch, freeze)
    dispatched: list = []

    def pool_dispatch(msg, **kwargs):
        dispatched.append(msg["method"])
        return {"jsonrpc": "2.0", "id": msg["id"], "result": land_run._handler(msg["params"], repo_root=repo / ".git")}

    monkeypatch.setattr(ctx, "_pool_dispatch", pool_dispatch)

    # The caller's landing hits the deadline on a starved commit leg: queued, nothing committed.
    freeze.freeze()
    halted = _land(repo)
    assert freeze.entered.is_set()
    assert halted["refused"] == land_deadline.REFUSED and halted["leg"] == "land_run.commit"
    assert halted["committed"] is False and halted["queued_for_resume"] is True
    assert land_resume.load(None, RUN)["attempts"] == 1
    assert _git(["rev-parse", "HEAD"], repo).strip() == head

    # IO still starved: the idle tick tries, hits the deadline again, and the entry stays queued.
    ctx._idle_tick()
    _drain_land_thread(ctx)
    assert dispatched == ["dispatch.land_run"]
    entry = land_resume.load(None, RUN)
    assert entry is not None and entry["attempts"] == 2
    assert (land_resume.queue_dir(None) / (RUN + land_resume.PENDING_SUFFIX)).exists()
    assert _git(["rev-parse", "HEAD"], repo).strip() == head

    # IO recovers: the next idle tick lands the run exactly once and closes the entry.
    freeze.thaw()
    ctx._idle_tick()
    _drain_land_thread(ctx)
    assert dispatched == ["dispatch.land_run"] * 2
    done = json.loads((land_resume.queue_dir(None) / (RUN + land_resume.DONE_SUFFIX)).read_text(encoding="utf-8"))
    assert done["outcome"] == "landed"
    assert _git(["rev-list", "--count", f"{head}..HEAD"], repo).strip() == "1"
    assert _tree(repo) == sorted(["src/a.py", *record_paths])
    assert land_resume.load(None, RUN) is None

    ctx._idle_tick()
    _drain_land_thread(ctx)
    assert dispatched == ["dispatch.land_run"] * 2


# ---- scenario 3: no spool entry is minted without a run ---------------------------------------


@pytest.fixture
def spool_root(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    return tmp_path


def _spooled(root: Path) -> str:
    payload = json.dumps(
        {"tool_name": "Workflow", "session_id": "sess1", "tool_use_id": "tu1", "tool_input": {"name": "ask"}}
    ).encode()
    spool_id, created = record.spool_ask(
        str(root), payload, session_id="sess1", tool_use_id="tu1", reason="engine_unreachable"
    )
    assert created
    return spool_id


def _allow(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "permissionDecision": "allow",
            "updatedInput": {"scriptPath": "x"},
            "permissionDecisionReason": reason,
        }
    }


def test_a_spooled_ask_is_minted_only_once_a_run_exists(spool_root):
    sid = _spooled(spool_root)
    starved = drain.run(str(spool_root), evaluate=lambda payload: _allow("Submitted to the engine."))
    assert starved["minted"] == 0 and starved["failed"] == 1
    assert not Path(record.entry_path(str(spool_root), sid, record.MINTED_SUFFIX)).exists()
    assert record.list_pending(str(spool_root)) == [sid]

    refused = drain.run(
        str(spool_root),
        evaluate=lambda payload: {
            "hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "HandoverPending"}
        },
    )
    assert refused["minted"] == 0 and refused["failed"] == 1
    assert record.list_pending(str(spool_root)) == [sid]

    run_id = "ask-20261113T180000-abcdef"

    named_only = drain.run(
        str(spool_root), evaluate=lambda payload: _allow(f"Submitted to the engine as run {run_id}.")
    )
    assert named_only["minted"] == 0 and named_only["failed"] == 1
    assert not Path(record.entry_path(str(spool_root), sid, record.MINTED_SUFFIX)).exists()
    assert record.list_pending(str(spool_root)) == [sid]
    header, _ = record.read_entry(record.entry_path(str(spool_root), sid))
    assert "no run dir" in header["last_error"]

    def recovered(payload: dict) -> dict:
        run_dir = spool_root / "scratch" / "warp" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "submission.json").write_text(
            json.dumps({"run_id": run_id, "session_id": "sess1", "tool_use_id": "tu1"}), encoding="utf-8"
        )
        return _allow(f"Submitted to the engine as run {run_id}.")

    minted = drain.run(str(spool_root), evaluate=recovered)
    assert minted["minted"] == 1 and minted["failed"] == 0
    assert Path(record.entry_path(str(spool_root), sid, record.MINTED_SUFFIX)).exists()
    assert record.list_pending(str(spool_root)) == []

"""Hook-lane guard round trips through a real warm pair never fail open with an indeterminate error under a capped IO load.

Boots an isolated warm pair from the live tree (real server, real pool worker processes), confirms
pair_up, then starts the rate-capped `io_stall_reader` and drives concurrent hook-lane guard ops via
`benchmarks/pool_leg_io_stall_canary.py :: run`. Any reply that is not a result and whose error code
`door_core.c :: is_provably_undispatched` does not list is a door fail-open ("server returned an
error that does not prove the op was never dispatched").

Trap: thread stand-ins in one interpreter do not reproduce the stall (the live stall is a pool worker
process blocked ~4s); only the real pair does.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks import pool_leg_io_stall_canary as canary
from coordinator_core.warm import breadcrumb, client, hang_restart, server
from coordinator_core.warm.engine_root import current_engine_clone
from coordinator_core.warm.tests.io_stall_reader import CHUNK_BYTES, io_stall_reader

_DOOR_DIR = Path(server.__file__).resolve().parent / "door"
_LOAD_SECS = 25.0
_THREADS = 12
_CAP_MB_PER_SEC = 450.0


def _code_values() -> dict:
    header = (_DOOR_DIR / "door_core.h").read_text(encoding="utf-8")
    return {name: int(value) for name, value in re.findall(r"#define\s+(JSONRPC_\w+)\s+\((-?\d+)\)", header)}


def provably_undispatched_codes() -> set:
    """The code set `door_core.c :: is_provably_undispatched` accepts, read from source at call time."""
    source = (_DOOR_DIR / "door_core.c").read_text(encoding="utf-8")
    match = re.search(r"int is_provably_undispatched\(long code\)\s*\{(.*?)\n\}", source, re.DOTALL)
    assert match, "is_provably_undispatched not found in door_core.c -- renamed or moved"
    values = _code_values()
    names = re.findall(r"code == (JSONRPC_\w+)", match.group(1))
    assert names and all(name in values for name in names), names
    return {values[name] for name in names}


def test_the_classifier_set_is_read_from_the_door_source() -> None:
    codes = provably_undispatched_codes()
    assert -32008 in codes and -32004 not in codes


def test_the_reader_refuses_to_run_uncapped(tmp_path: Path) -> None:
    for cap in (0, -1, None):
        with pytest.raises(ValueError):
            with io_stall_reader(tmp_path / "r", mb_per_sec=cap):  # type: ignore[arg-type]
                pass  # pragma: no cover
    assert not (tmp_path / "r" / "io_stall_reader.bin").exists()


@pytest.mark.deliberate_wall_clock(reason="the reader's rate cap is bytes per elapsed second")
def test_the_reader_holds_its_rate_cap_and_removes_its_file(tmp_path: Path) -> None:
    cap = 80.0
    with io_stall_reader(tmp_path / "r", threads=4, size_mb=32, seconds=1.5, mb_per_sec=cap) as stats:
        started = time.monotonic()
        time.sleep(1.5)
        elapsed = time.monotonic() - started
    allowed = cap * (elapsed + 1.0) + 4 * CHUNK_BYTES / (1024 * 1024)
    assert stats.bytes_read > 0
    assert stats.bytes_read / (1024 * 1024) <= allowed, (stats.bytes_read, allowed)
    assert not (tmp_path / "r" / "io_stall_reader.bin").exists()


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.warm_tier
@pytest.mark.deliberate_wall_clock(reason="the hook lane budget deadline under an IO load is the behaviour under test")
def test_hook_lane_guards_never_fail_open_with_an_indeterminate_error_under_a_capped_io_load(tmp_path: Path) -> None:
    allowed = provably_undispatched_codes()
    result = canary.run(
        canary.WORKTREE,
        tmp_path / "canary",
        _LOAD_SECS,
        _THREADS,
        io_reader=True,
        reader_mb_per_sec=_CAP_MB_PER_SEC,
    )
    assert result.get("pair_up"), result
    assert result["reader_mb_per_sec"] > 0, "the reader never read: the fixture did not load the volume"
    assert result["guard_ok"] + result["guard_bad"] > 0, result
    failed_open = {code: n for code, n in result["bad_codes"].items() if not code.lstrip("-").isdigit() or int(code) not in allowed}
    assert not failed_open, (failed_open, result["bad_samples"])


_PID = 424242
_TOKEN = "faketoken"
_HOOK = {"jsonrpc": "2.0", "id": 1, "method": "hooks.preuse_bash_dispatch", "params": {}}


class _DelayedPipe:
    """A server connection whose reply is held until `release` is set, then answers."""

    def __init__(self, release: threading.Event) -> None:
        self._release = release

    def write(self, data: bytes) -> None:
        pass

    def flush(self) -> None:
        pass

    def readline(self):
        self._release.wait(30)
        return b'{"jsonrpc": "2.0", "id": 1, "result": {}}\n'

    def close(self) -> None:
        pass


@pytest.mark.deliberate_wall_clock(reason="a reply held past the client read deadline is the behaviour under test")
def test_one_server_delay_costs_each_hook_only_its_own_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A delayed (not dead) engine fails open only the calls it delayed: it is never ended, and the next call is answered.

    Amplifier: `client._note_unanswered` counts every delayed concurrent hook toward
    `UNANSWERED_RESTART_K`, and `_request_restart` then `hang_restart.exit_server`s an engine that is
    still answering, so every following call finds no pipe and fails open in milliseconds.
    """
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(tmp_path))
    monkeypatch.setattr(client, "is_warm_enabled", lambda: True)
    monkeypatch.setattr(client, "engine_token", lambda: _TOKEN)
    monkeypatch.setattr(client.election, "pipe_name", lambda token: "fake-pipe")
    monkeypatch.setattr(client, "_spawned_this_process", False)
    monkeypatch.setattr(client, "_live_tree_cold", False)
    monkeypatch.setattr(client, "_cold_reason", None)
    monkeypatch.setattr(client, "READ_DEADLINE_SECS", 0.05)
    monkeypatch.setattr(client, "_mutation_deadline_for", lambda method: 0.1)
    monkeypatch.setattr(breadcrumb, "stable_pid_alive", lambda *a, **k: True)
    svc = breadcrumb.svc_dir(current_engine_clone())
    svc.mkdir(parents=True)
    (svc / breadcrumb.BREADCRUMB_FILENAME).write_text(
        json.dumps({"pipe": "p", "pid": _PID, "stable_pid_start_epoch": 1}), encoding="utf-8"
    )
    ended: list = []
    monkeypatch.setattr(hang_restart, "exit_server", ended.append)

    release = threading.Event()
    delayed = _DelayedPipe(release)
    monkeypatch.setattr(client, "_open_pipe", lambda name: delayed)
    replies: list = []
    callers = [
        threading.Thread(target=lambda: replies.append(client.try_warm_dispatch(dict(_HOOK), hook_path=True)))
        for _ in range(client.UNANSWERED_RESTART_K)
    ]
    for caller in callers:
        caller.start()
        time.sleep(0.03)
    for caller in callers:
        caller.join(10.0)
    release.set()

    assert len(replies) == client.UNANSWERED_RESTART_K
    assert not any(isinstance(reply, dict) and "result" in reply for reply in replies), "the delay did not outlive the deadline"
    assert ended == [], "a delayed engine was ended by the unanswered-call counter"
    follow_up = client.try_warm_dispatch(dict(_HOOK), hook_path=True)
    assert isinstance(follow_up, dict) and "result" in follow_up, follow_up


def _write_stall_records(stall_dir: Path, count: int, header: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    for i in range(count):
        stamp = time.strftime(hang_restart._STAMP_FMT, time.gmtime(now - i))
        (stall_dir / f"{stamp}-{i}.txt").write_text(f"{header}\n", encoding="utf-8")


def test_a_genuinely_hung_server_is_still_restarted_within_the_hang_bounds(tmp_path: Path) -> None:
    """Non-io-wait stall records at the threshold end the server once; the backoff then holds the next restart.

    Pins real hang recovery so that exempting io-wait stalls from a kill cannot disable it.
    """
    stall_dir = tmp_path / "svc" / "stall-dumps"
    stall_dir.mkdir(parents=True)
    _write_stall_records(stall_dir, hang_restart.HANG_DUMP_THRESHOLD, "stall-class: hung")
    exits: list = []
    assert hang_restart.maybe_restart(stall_dir, server_pid=_PID, exit_fn=exits.append) == "restart"
    assert exits == [_PID]
    log = (stall_dir.parent / hang_restart.HANG_RESTART_LOG_FILENAME).read_text(encoding="utf-8")
    assert " action=restart " in log
    later = time.time() + 10.0
    _write_stall_records(stall_dir, hang_restart.HANG_DUMP_THRESHOLD, "stall-class: hung", now=later)
    assert hang_restart.maybe_restart(stall_dir, server_pid=_PID, now=later + 1, exit_fn=exits.append) == "suppressed"
    assert exits == [_PID]


def test_the_in_memory_stall_trigger_still_ends_a_hung_server_at_the_threshold(tmp_path: Path) -> None:
    stall_dir = tmp_path / "svc" / "stall-dumps"
    stall_dir.mkdir(parents=True)
    hang_restart._stall_marks.pop(str(stall_dir), None)
    exits: list = []
    verdicts = [
        hang_restart.note_stall(stall_dir, server_pid=_PID, exit_fn=exits.append)
        for _ in range(hang_restart.HANG_DUMP_THRESHOLD)
    ]
    assert verdicts[:-1] == [None] * (hang_restart.HANG_DUMP_THRESHOLD - 1)
    assert verdicts[-1] == "restart" and exits == [_PID]


def test_a_hook_lane_task_binds_the_chain_deadline_inside_the_server_budget_and_others_clear_it() -> None:
    from coordinator_core.hooks import _preuse_phases

    stamp = time.monotonic()
    try:
        server._bind_hook_lane_budget({"method": "hooks.preuse_bash_dispatch"}, stamp)
        end = _preuse_phases._POOL_BUDGET_END
        assert end is not None and end < stamp + server._HOOK_LANE_BUDGET_SECS
        server._bind_hook_lane_budget({"method": "dispatch.emit"}, stamp)
        assert _preuse_phases._POOL_BUDGET_END is None
    finally:
        _preuse_phases.bind_pool_budget(None)

"""One server-side delay on a real warm pair fails open only the call it delayed; every following hook call reaches the server.

Boots an isolated warm pair from the live tree (real server, real pool worker processes), confirms
pair_up, arms ONE delay in the pair's worker body (longer than the server hook-lane budget, so the delayed call
is answered with the budget's error envelope), then issues the delayed hook call and, spaced across
the delay window and just past it, a run of following hook calls. The delayed call may fail open;
each following call must return a result, and the server's own request telemetry must hold a row per
call issued.

Trap: round trips are counted from `request-telemetry.jsonl`, never from client return values, so a
client that short-circuits to None without connecting shows as a missing server row.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks import pool_leg_io_stall_canary as canary
from coordinator_core.warm import server

_HOOK_METHOD = "hooks.preuse_bash_dispatch"
_LANES = 4
_FOLLOWING_CALLS = 6
_FOLLOWING_GAP_SECS = 0.5
_DELAY_SECS = server._HOOK_LANE_BUDGET_SECS + 1.5
_TELEMETRY_POLL_SECS = 60.0

_EPILOGUE = '''

def _single_delay_wrap(fn):
    import functools as _ft
    import os as _os
    import time as _t

    @_ft.wraps(fn)
    def _wrapped(msg, caller, *args, **kwargs):
        if str(msg.get("method", "")).startswith("hooks."):
            try:
                _os.replace({marker!r}, {marker!r} + ".taken")
                _secs = float(open({marker!r} + ".taken").read())
            except (OSError, ValueError):
                _secs = 0.0
            with open({marker!r} + ".log", "a") as _log:
                _log.write("%.3f pid=%d delay=%.1f id=%s\\n" % (_t.time(), _os.getpid(), _secs, msg.get("id")))
            if _secs > 0:
                _t.sleep(_secs)
        return fn(msg, caller, *args, **kwargs)

    return _wrapped


_pool_dispatch_worker_body = _single_delay_wrap(_pool_dispatch_worker_body)
'''

_DRIVER = r"""
import json, sys, threading, time
from coordinator_core.warm import client
dest, marker, delay, following, gap, pair_up_secs, LANES = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), float(sys.argv[5]), float(sys.argv[6]), int(sys.argv[7])
seq = iter(range(1, 10**6))

def guard_for(n):
    return {"jsonrpc": "2.0", "id": n, "method": "hooks.preuse_bash_dispatch", "params": {
        "hook_event_name": "PreToolUse", "session_id": "single-delay-%d" % n, "cwd": dest, "tool_name": "Bash",
        "tool_input": {"command": "git status"}}}

def call():
    t = time.monotonic()
    try:
        r = client.try_warm_dispatch(guard_for(next(seq)), read_deadline_secs=8.0, hook_path=True)
    except Exception as exc:
        r = {"error": {"code": 0, "message": repr(exc)}}
    return r, (time.monotonic() - t) * 1000

up = time.monotonic() + pair_up_secs
while time.monotonic() < up:
    r, _ = call()
    if isinstance(r, dict) and "result" in r:
        break
    time.sleep(1)
else:
    print(json.dumps({"pair_up": False, "last": str(r)[:300]}), flush=True); sys.exit(2)
print(json.dumps({"pair_up": True}), flush=True)
sys.stdin.readline()

def row(r, ms):
    return {"result": isinstance(r, dict) and "result" in r, "none": r is None, "ms": round(ms), "reply": str(r)[:200], "bucket": client._cold_bucket_for_row(), "spawn": client.spawn_wanted(), "cold": client.last_cold_reason()}

for _round in range(2):
    warmers = [threading.Thread(target=call) for _ in range(4)]
    [w.start() for w in warmers]
    [w.join(30) for w in warmers]

t_start_epoch = time.time()
open(marker, "w").write(delay)
box = []
worker = threading.Thread(target=lambda: box.append(row(*call())))
worker.start()
time.sleep(gap)
followers = []

def lane():
    for _ in range(following):
        followers.append(row(*call()))
        time.sleep(gap)

lanes = [threading.Thread(target=lane) for _ in range(LANES)]
[x.start() for x in lanes]
[x.join(120) for x in lanes]
worker.join(60)
delayed = box[0] if box else {"result": False, "none": True, "ms": 0, "reply": "delayed call never returned"}
print(json.dumps({"t_start_epoch": t_start_epoch, "delayed": delayed, "followers": followers}))
"""


def _telemetry_rows(runtime_base: Path) -> list:
    rows: list = []
    for path in runtime_base.rglob("request-telemetry.jsonl*"):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def _build_with_delay_hook(work: Path, marker: Path) -> Path:
    """`canary._build` of the live tree, plus a one-shot delay armed by `marker` in the pool worker body."""
    dest = canary._build(canary.WORKTREE, work)
    target = dest / "coordinator_core" / "warm" / "server.py"
    target.write_text(target.read_text(encoding="utf-8") + _EPILOGUE.format(marker=str(marker)), encoding="utf-8")
    for args in (["add", "-A"], ["-c", "user.name=c", "-c", "user.email=c@c", "commit", "-q", "-m", "single-delay"]):
        subprocess.run(["git", "-c", "core.longpaths=true", "-C", str(dest), *args], check=True, capture_output=True, creationflags=canary.NO_WINDOW)
    return dest


@pytest.mark.pending_fix
@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.warm_tier
@pytest.mark.deliberate_wall_clock(reason="a server delay longer than the hook-lane budget is the behaviour under test")
def test_one_server_delay_on_a_real_pair_fails_open_only_its_own_call(tmp_path: Path) -> None:
    work = tmp_path / "pair"
    marker = tmp_path / "delay-marker"
    dest = _build_with_delay_hook(work, marker)
    env = dict(os.environ)
    env.update(
        COORDINATOR_WARM_RUNTIME_BASE=str(work / "rt"),
        PYTHONPATH=str(dest),
        COORDINATOR_CLONE_ROOT=str(dest),
        COORDINATOR_SETTINGS_HOME=str(work / "settings"),
        COORDINATOR_WARM="1",
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", _DRIVER, str(dest), str(marker), str(_DELAY_SECS), str(_FOLLOWING_CALLS), str(_FOLLOWING_GAP_SECS), str(canary.PAIR_UP_SECS), str(_LANES)],
        cwd=str(dest), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=canary.NO_WINDOW,
    )
    try:
        head = proc.stdout.readline().strip()  # type: ignore[union-attr]
        assert head and json.loads(head).get("pair_up") is True, (head, proc.poll())
        tail, stderr = proc.communicate("go\n", timeout=120)
        outcome = json.loads(tail.strip().splitlines()[-1])
        calls = 1 + _LANES * _FOLLOWING_CALLS
        deadline = time.monotonic() + _TELEMETRY_POLL_SECS
        served: list = []
        while time.monotonic() < deadline:
            served = [r for r in _telemetry_rows(work / "rt") if r.get("op") == _HOOK_METHOD and (r.get("enqueue_ts") or r.get("start_ts") or 0) >= outcome["t_start_epoch"]]
            if len(served) >= calls:
                break
            time.sleep(1.0)
    finally:
        if proc.poll() is None:
            proc.kill()
        subprocess.run(
            [sys.executable, "-c", "import sys\nfrom pathlib import Path\nfrom coordinator_core.warm.pair_stop import stop_warm_pair\nstop_warm_pair(Path(sys.argv[1]))", str(dest)],
            cwd=str(dest), env=env, capture_output=True, timeout=120, creationflags=canary.NO_WINDOW,
        )

    assert not outcome["delayed"]["result"], ("the delay did not outlive the hook-lane budget", outcome["delayed"])
    failed_open = [f for f in outcome["followers"] if not f["result"]]
    restarts = [ln[:120] for p in (work / "rt").rglob("hang-restarts.log") for ln in p.read_text(errors="replace").splitlines()]
    detail = json.dumps({
        "failed_open": [(f["ms"], f["none"], f["bucket"], f["spawn"]) for f in failed_open[:8]],
        "delayed": outcome["delayed"],
        "hang_restarts_log": restarts,
    })
    assert not failed_open, ("following calls inside the delay window failed open", len(failed_open), detail)
    assert len(served) >= calls, (len(served), calls, "the server saw fewer hook round trips than calls issued")

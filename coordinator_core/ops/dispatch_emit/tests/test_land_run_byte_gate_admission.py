"""Two landings in separate spawned processes over one repo: the second is admitted only after the
first leaves the byte gate, and its land budget clock starts at admission, not at submit."""

from __future__ import annotations

import multiprocessing
import subprocess
import time
import traceback
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261030T000000-abcdef"
HOLD_SECS = 1.5
CHILD_TIMEOUT = 90


def _landing(repo: str, locks: str, label: str, hold: float, budget: float, ready, out) -> None:
    """Child process: one product landing whose body records when it ran. Times are
    `time.monotonic()`, one clock across processes on a host."""
    try:
        from coordinator_core import locked_write
        from coordinator_core.ops.dispatch_emit import land_deadline, land_run

        locked_write._machine_lock_dir = lambda: Path(locks)
        import coordinator_core.lifecycle as lifecycle

        lifecycle.main_worktree_root = lambda repo_root: Path(repo)
        land_deadline.LAND_BUDGET_SECS = budget
        seen: dict = {}

        def body(mode, root, repo_root, params):
            seen["body_start"] = time.monotonic()
            seen["scope_entry"] = land_deadline._LEGS.get()[0][1]
            land_deadline.check("land_run.script_read")
            ready.set()
            time.sleep(hold)
            land_deadline.check("land_run.commit")
            seen["body_end"] = time.monotonic()
            return {"committed": True, "sha": "c" * 40}

        land_run._dispatch_mode = body
        seen["submit"] = time.monotonic()
        reply = land_run._dispatch({"run_id": f"{RUN[:-1]}{label}"}, Path(repo))
        out.put((label, "ok", reply, seen))
    except BaseException:  # noqa: BLE001 -- the parent asserts on the traceback
        out.put((label, "err", traceback.format_exc(), {}))


def test_second_landing_is_admitted_after_the_first_and_its_clock_starts_at_admission(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, **no_console_creationflags())
    locks = tmp_path / "locks"
    ctx = multiprocessing.get_context("spawn")
    out = ctx.Queue()
    first_in, second_in = ctx.Event(), ctx.Event()
    # The second's budget is shorter than the first's hold: a clock started at submit would halt it.
    first = ctx.Process(target=_landing, args=(str(repo), str(locks), "a", HOLD_SECS, 30.0, first_in, out))
    second = ctx.Process(target=_landing, args=(str(repo), str(locks), "b", 0.0, HOLD_SECS / 3, second_in, out))
    try:
        first.start()
        assert first_in.wait(CHILD_TIMEOUT), "first landing never reached its body"
        second.start()
        results = {}
        for _ in range(2):
            label, status, reply, seen = out.get(timeout=CHILD_TIMEOUT)
            assert status == "ok", reply
            results[label] = (reply, seen)
    finally:
        for proc in (first, second):
            proc.join(timeout=10)
            if proc.is_alive():
                proc.kill()
                proc.join()

    (reply_a, seen_a), (reply_b, seen_b) = results["a"], results["b"]
    assert reply_a["committed"] is True
    assert reply_b["committed"] is True, reply_b
    assert seen_b["body_start"] >= seen_a["body_end"]
    assert seen_b["body_start"] - seen_b["submit"] >= HOLD_SECS / 3
    assert seen_b["scope_entry"] >= seen_a["body_end"]
    assert seen_b["scope_entry"] <= seen_b["body_start"]

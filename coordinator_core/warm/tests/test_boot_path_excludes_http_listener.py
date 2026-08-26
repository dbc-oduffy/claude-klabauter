"""`ensure_listener` is off the pipe server's critical path, and `ready_secs`
no longer counts it.

Advisory item 5. `supervisor.ensure_listener` is documented never to wait and
waits up to `HEALTH_CHECK_TIMEOUT_SECS` (2.0 s) inside `check_health`, a
synchronous `urlopen`, whenever a discovery record names a live pid whose
listener has hung. It sat between `_preload_op_registry` and `serve_forever`, so
that wait landed on the successor's time-to-answerable -- during exactly the
window a caller is already waiting out a predecessor's drain.

It also sat ABOVE `_record_own_boot`, so every `ready_secs` row on disk included
it. The whole 2026-08-26 succession investigation reasoned from `ready_secs` and
never saw that, because its sandbox had no stale discovery record to make the
probe wait.

Both are structural properties of `_run_guarded`'s source, asserted here rather
than timed: a timing assertion on a box running 50-70 concurrent sessions
measures peer load, not this ordering (CLAUDE.md § Load norm).
"""

from __future__ import annotations

import inspect

from coordinator_core.warm import server


def _boot_source() -> str:
    return inspect.getsource(server._run_guarded)


def test_boot_records_its_own_timing_before_ensuring_the_http_listener():
    """`ready_secs` must not include somebody else's health probe."""
    src = _boot_source()
    record_at = src.index("_record_own_boot(spawn_epoch")
    ensure_at = src.index("ensure_listener(repo_root)")
    assert record_at < ensure_at, (
        "_record_own_boot must run BEFORE the ensure_listener call -- below it, "
        "every ready_secs row silently carries up to HEALTH_CHECK_TIMEOUT_SECS "
        "of a health probe that says nothing about whether this server can answer"
    )


def test_ensure_listener_is_not_called_synchronously_on_the_boot_path():
    """Off the critical path, on its own thread. The return value is ignored by
    contract and `entry_seam._trigger_listener_boot` covers the case this call
    does not, so nothing observes the difference except the successor's
    time-to-answerable."""
    src = _boot_source()
    assert "warm-http-ensure-listener" in src, (
        "the ensure_listener call is no longer on its own named thread -- if it "
        "moved back onto the boot path, a hung http listener costs every warm "
        "server 2.0s of time-to-answerable again"
    )
    ensure_line = next(
        line for line in src.splitlines() if "ensure_listener(repo_root)" in line
    )
    assert ensure_line.startswith(" " * 12), (
        "ensure_listener(repo_root) is no longer nested inside the thread target "
        f"-- found at outer indentation: {ensure_line!r}"
    )


def test_the_thread_target_still_swallows_everything():
    """A daemon thread's uncaught exception prints to a stderr `spawn_detached`
    opens as DEVNULL. The guard the synchronous call had must survive the move."""
    src = _boot_source()
    body = src[src.index("def _ensure_http_listener"):]
    assert "except Exception" in body.split("threading.Thread")[0]

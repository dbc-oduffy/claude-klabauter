"""
coordinator_core.install.door_route_signal -- a positive through-the-door
signal, read from telemetry rather than invented in-band.

Purpose: `README-posix.md` names the only discriminator an install step could
previously assert on as an EXTERNAL timing comparison (`time ./door ping`
against the cold entrypoint) -- not something `scripts/setup.py` can check
programmatically. The real discriminator already exists one layer down:
`coordinator_core.telemetry.op_latency` stamps every dispatched op's sink row
with a `route` field (`IN_PROCESS` / `WARM_SERVER` / `HTTP_SERVER`), and
`coordinator_core.warm.server._declare_execution_route` sets that stamp to
`WARM_SERVER` before the server starts accepting -- so a door invocation the
warm server actually served records `route: warm_server`, and a fall-through
(door found no peer, ran the original cold entrypoint) records `in_process`.
This module invokes the installed door for a named op and reads that stamp
back, rather than trusting the door's exit code -- which verifies only that
SOMETHING answered, never which path answered it
(docs/plans/2026-08-22-warm-engine-and-door-install-from-published-root.md
§ D3).

Spec backlink: docs/plans/2026-08-22-warm-engine-and-door-install-from-
published-root.md, chunk C5.

REPO-SCOPING (eng-director review F5, Major): `op_latency._write_entry`
resolves the sink it writes to under `git_common_dir(repo_root)`, where
`repo_root` comes from the dispatch envelope's `_origin_worktree`, else
`ipc.resolve_caller_cwd`'s `_caller_cwd`, else -- silently -- `Path.cwd()` of
the process that actually executed the op. The door protocol carries neither
`_origin_worktree` nor `_caller_cwd` (`door_core.c` builds no such fields), so
a door-routed invocation's row lands under whatever the EXECUTING process's
own cwd resolves to -- the warm server's, in a warm pool worker, which is the
published-mirror checkout, not necessarily the caller's own tree. Every
function below therefore takes `repo_root` as a REQUIRED keyword-only
argument naming the sink this call reads, rather than assuming a single
ambient sink -- `iter_sink_entries` yields nothing given neither `repo_root`
nor explicit `sink_paths`, and a caller supplying the wrong `repo_root` gets a
silent `UNRESOLVED`, indistinguishable from a genuine fall-through. Callers
(C6) own picking the `repo_root` that actually matches where the server's
process cwd resolves the sink to -- this module does not, and cannot,
re-derive that from here.

THE DISCRIMINATOR-INERT TRAP, one layer down from D3 itself: `_write_entry`
swallows every failure (COORDINATOR_OP_LATENCY_DISABLE=1, an unresolvable git
common dir, an unwritable sink, a full disk) silently, by design -- "a
telemetry defect must never fail a peer's op". On a box where any of those
hold, EVERY outcome reads back as UNRESOLVED, and this discriminator goes
silently inert exactly the way the exit-code check it replaces did. Do not
trust an UNRESOLVED result as "fall-through occurred" without first running
`run_cold_control_invocation` below (a known-cold invocation that bypasses
the door and the warm server entirely, so it is guaranteed to write an
`in_process` row through the exact same dispatch chokepoint,
`coordinator_core.ipc.dispatch_message`) -- if the control invocation ALSO
comes back UNRESOLVED, report `DISCRIMINATOR_UNAVAILABLE` explicitly, a
distinct outcome from both a genuine `WARM_SERVER` pass and an `IN_PROCESS`
fall-through; never fold it into the same ADVISORY a real fall-through
produces.

Negative-spec:
    - Does not add a response field, wire-format change, or env var to the
      door protocol itself -- the door's own argv/stdout contract is
      untouched.
    - Does not read wall clock or process time as a discriminator. The
      sink's `route` field is authoritative; timing is never consulted here.
    - Does not build or install the door -- see `door_install.py` /
      `door_install_posix_build.py`.
    - `WARM_SERVER` is the only PASS-worthy outcome. `IN_PROCESS` proves a
      real fall-through occurred; it is not a failure of THIS module, only
      of the thing it is measuring.

OPEN SUB-QUESTION, named rather than hidden (see `read_door_route`'s own
docstring): the door's own caller cannot observe the `corr_id` minted
server-side for its own invocation, so the row this module reads back is the
newest complete row for the dispatched op's name after the invocation
returns -- accurate at install time (single-threaded, no concurrent traffic
against the same op name) and NOT a general-purpose correlation primitive
under concurrent load.
"""

from __future__ import annotations

import subprocess
import time
import uuid
from pathlib import Path
from typing import List, NamedTuple, Optional, Sequence

from coordinator_core.telemetry import op_latency
from coordinator_core.telemetry.engine_report import iter_sink_entries
from coordinator_core.win_portability import no_console_creationflags

#: Re-exported so a caller need not import `op_latency` separately just to
#: compare a `DoorRouteResult.route` against the two PASS/fall-through values.
WARM_SERVER = op_latency.WARM_SERVER
IN_PROCESS = op_latency.IN_PROCESS

#: No matching sink row was found for the invocation -- either a genuine
#: fall-through whose row never got `route` stamped correctly (should not
#: happen given the module docstring's route-stamping contract, but this
#: module never assumes it), or the sink itself is silently inert (kill
#: switch, unresolvable git common dir, unwritable disk). Distinguish the
#: two via `run_cold_control_invocation` -- see module docstring.
UNRESOLVED = "unresolved"

#: The known-cold control invocation ALSO came back `unresolved` -- the sink
#: itself is inert on this box, so an `unresolved` result from the door
#: invocation proper cannot be trusted as "fall-through occurred". A distinct
#: outcome from both `WARM_SERVER` and `IN_PROCESS`, never folded into either.
DISCRIMINATOR_UNAVAILABLE = "discriminator_unavailable"

#: Timeout for the door subprocess itself -- generous relative to
#: `README-posix.md`'s measured ~1ms warm / low-double-digit-ms cold shape,
#: never load-bearing for correctness, only a hang guard.
_DOOR_TIMEOUT_SECS = 30.0


class DoorRouteResult(NamedTuple):
    """One classified outcome of a through-the-door invocation.

    `route` is one of `WARM_SERVER`, `IN_PROCESS`, or `UNRESOLVED`.
    `entry` is the raw sink row this classification was read from, or
    `None` when `route` is `UNRESOLVED` (no row was found).
    """

    route: str
    entry: Optional[dict]


def _newest_matching_row(op: str, *, repo_root: Path, since: float) -> Optional[dict]:
    """The newest COMPLETE sink row for `op` with `t_start >= since`.

    Newest-wins by `t_start` -- ties broken by scan order (oldest-first per
    `iter_sink_entries`, so the last one seen in a tie is kept). A row
    lacking `t_start` is never treated as newest (see `iter_sink_entries`'s
    own "kept, not dropped" contract for a MISSING `since` comparison; here
    a MISSING `t_start` cannot be compared to `since` at all, so it is
    excluded from the max rather than assumed current).
    """
    best: Optional[dict] = None
    best_t: Optional[float] = None
    for row in iter_sink_entries(repo_root=repo_root, since=since):
        if row.get("op") != op:
            continue
        kind = row.get("kind") or "complete"
        if kind != "complete":
            continue
        t_start = row.get("t_start")
        if not isinstance(t_start, (int, float)):
            continue
        if best_t is None or t_start >= best_t:
            best = row
            best_t = t_start
    return best


def read_door_route(
    door_path: Path,
    op: str,
    args: Optional[Sequence[str]] = None,
    *,
    repo_root: Path,
    timeout: float = _DOOR_TIMEOUT_SECS,
) -> DoorRouteResult:
    """Invoke the installed door for `op`, then read back its recorded route.

    Runs `door_path op *args` as a subprocess (matching `README-posix.md`'s
    `./door ping` invocation shape), then reads the newest complete sink row
    for `op` written since just before the invocation, via
    `coordinator_core.telemetry.engine_report.iter_sink_entries` scoped to
    `repo_root` (see module docstring's REPO-SCOPING section for why this
    argument is required rather than defaulted, and why the caller -- not
    this function -- must supply the `repo_root` that actually matches where
    the executing process's sink write lands).

    Correlation caveat (see module docstring's OPEN SUB-QUESTION): `corr_id`
    is minted server-side, once, at `dispatch_message` entry, and is never
    returned to the door client in its response envelope, so this function
    cannot ask for "the exact row this invocation wrote" -- it reads back
    the newest matching row for `op` instead. Acceptable ONLY under
    single-threaded, no-concurrent-traffic conditions (true of an install
    step); the newest-row-for-this-op technique does not generalise to
    concurrent callers of the same op name, and must not be reused as a
    general-purpose correlation primitive.

    Never raises on the door's own failure to run: a `FileNotFoundError`
    (door not installed) or non-zero exit both still fall through to reading
    the sink -- there may be no row at all, which correctly classifies as
    `UNRESOLVED` rather than raising past this function's caller (this
    module composes into a best-effort install step; see
    `door_install.py`'s and `start_warm_engine`'s "never fail the install"
    contract).
    """
    since = time.time()
    argv: List[str] = [str(door_path), op, *(args or [])]
    try:
        subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            check=False,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        pass

    row = _newest_matching_row(op, repo_root=repo_root, since=since)
    if row is None:
        return DoorRouteResult(UNRESOLVED, None)

    route = row.get("route")
    if route not in (WARM_SERVER, IN_PROCESS):
        # An unstamped or unrecognised route is UNOBSERVABLE, not a route --
        # matches `engine_report.route_distribution`'s "unstamped is
        # unobservable, not cold" rule; this module never guesses.
        return DoorRouteResult(UNRESOLVED, row)

    return DoorRouteResult(route, row)


def run_cold_control_invocation(op: str, *, repo_root: Path, params: Optional[dict] = None) -> DoorRouteResult:
    """A known-cold control: dispatch `op` in-process, bypassing the door and
    the warm server entirely, guaranteed to write an `in_process` row.

    Used to validate an `UNRESOLVED` result from `read_door_route` before
    trusting it as "fall-through occurred" (module docstring's
    DISCRIMINATOR-INERT TRAP). Calls
    `coordinator_core.ipc.dispatch_message` directly, in THIS process --
    the same dispatch chokepoint the door's fall-through and every other
    caller pass through, so it exercises the identical sink-write path
    `read_door_route` depends on, without needing a warm server or a door
    binary at all. `op_latency.ROUTE_ENV` is set only by
    `warm.server._declare_execution_route` inside an accepting warm-server
    process; this function's own process carries no such stamp, so
    `execution_route()` degrades to `IN_PROCESS` by construction, not by
    assumption -- see that function's own default.

    Returns `DoorRouteResult(IN_PROCESS, entry)` on a normal control run.
    Returns `DoorRouteResult(UNRESOLVED, None)` if -- and only if -- the
    sink itself is inert on this box (kill switch, unresolvable git common
    dir, unwritable disk); a caller reading that back from THIS function
    should report `DISCRIMINATOR_UNAVAILABLE`, not a fall-through, per the
    module docstring.
    """
    import asyncio

    from coordinator_core.ipc import dispatch_message

    since = time.time()
    msg = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": op,
        "params": params or {},
        "_origin_worktree": str(repo_root),
    }
    asyncio.run(
        dispatch_message(msg, caller="coordinator_core.install.door_route_signal")
    )

    row = _newest_matching_row(op, repo_root=repo_root, since=since)
    if row is None:
        return DoorRouteResult(UNRESOLVED, None)
    return DoorRouteResult(row.get("route") or IN_PROCESS, row)

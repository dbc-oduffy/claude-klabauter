"""coordinator_core.warm.telemetry — warm lifecycle observability.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C26

WHY THIS IS LOAD-BEARING, not decoration. With warmth opt-in (C23) and
demand-driven (idle demotion, C24), "was this session served warm?" stops
being a property of the box and becomes a per-session fact -- an
unobservable one by default. DR-313 item 5 makes this concrete against a
live precedent: DoE's live-tree env override is silent and inherited
through `child_env()`, so exempt sessions are unidentifiable, and that DR
downgrades it from "mitigation" to "an exemption that must be made
observable before any coverage claim is credible." A warm engine with an
opt-in and an idle timer has exactly that shape and must not repeat it.
With no `warm.status` verb, this module is the SOLE observability
surface by design -- "was this session warm?" has no other answer.

WHAT THIS MODULE RECORDS, cheaply, on an already-open in-process
structure (`ServerTelemetry`, one instance per server life -- no new
spawn, no new file per invocation):

  1. Whether each invocation was served warm or cold
     (`record_invocation(warm=...)`).
  2. The reason a server exited -- `EXIT_REASON_SKEW` (C16's
     `evict_on_skew`), `EXIT_REASON_SUPERSEDED` (`warm.idle`'s
     token-mismatch predicate: a generation whose pipe name no longer
     matches the current engine, retiring without traffic),
     `EXIT_REASON_IDLE_DEMOTION` (C24's `demote_if_idle`),
     `EXIT_REASON_OPERATOR_STOP` (the operator stop hatch,
     `coordinator/bin/warm-engine-stop.py`), or `EXIT_REASON_DEGRADED` (a
     self-detected degraded-health stop) -- recorded by
     `record_exit(reason)`.

     `SUPERSEDED` is deliberately distinct from `IDLE_DEMOTION` even
     though both exit through `demote_if_idle`: they answer different
     questions in the telemetry record. `IDLE_DEMOTION` means "nobody
     needed this server"; `SUPERSEDED` means "a newer engine replaced
     it." Folding the two would make the stranded-generation population
     -- the one that motivated the predicate -- unmeasurable in exactly
     the record that exists to measure it.
  3. The served-invocation count per server life (`served_count()`) --
     not decoration: it is the direct measurement of the amortization
     argument this plan rests on (a server serving ~130 invocations per
     2.1-minute life), and C21's re-measurement cannot honestly assert
     warmth paid off without it. `served_count` is also the exact
     zero-arg shape C24's `idle.ServedCountFn` seam expects, so a caller
     wires `telemetry.served_count` straight into `idle.should_demote` /
     `idle.demote_if_idle`'s `served_count=` argument with no adapter.

WIRING NOTE (scope boundary, not a gap in this module): this row's
`writes:` is exactly `{telemetry.py, this module's own test file}`.
Constructing a `ServerTelemetry` at server boot, calling
`record_invocation` from the dispatch seam, calling `record_exit` from
each of the four trigger call sites, and calling `flush()` from C17's
`ctx_shutdown` step are all edits to files OUTSIDE that list
(`warm/server.py`, `warm/skew.py`, `warm/idle.py`,
`coordinator/bin/warm-engine-stop.py`) and are therefore NOT made by
this chunk -- this module supplies the recording primitives and the
on-disk flush target; wiring each call site is a follow-up chunk's job,
the same scope split `warm.breadcrumb`'s own docstring already
documents for its own storage/decision-vs-wiring split.

ON-DISK SHAPE: `<svc dir>/telemetry.jsonl`, one JSON line appended per
`flush()` -- an APPEND log, not a latest-snapshot file like
`warm.breadcrumb`'s `warm.json`, because the thing worth reading back is
a HISTORY of server lives (C27's soak explicitly reads "served-invocation
count per server life" across many short lives), not only the most
recent one. `svc_dir()` reuses the exact resolution
`warm.breadcrumb.svc_dir` already establishes as this package's
precedent for a resident-engine concern's on-disk home, rather than
inventing a second directory convention -- so this log follows that
function wherever it resolves, and moved out of the engine clone with it
on 2026-08-19.

NEGATIVE-SPEC:
  - Does NOT decide WHEN a server exits or WHY -- that is each trigger's
    own job (`warm.skew.evict_on_skew`, `warm.idle.demote_if_idle`, the
    operator stop hatch, a degraded self-stop); this module only records
    the reason a caller reports.
  - Does NOT call `warm.lifecycle.begin_shutdown` / `drain_and_exit`, and
    is not itself part of the single-shot shutdown guard -- flushing
    telemetry is orthogonal to, and safely re-orderable around, the
    four-step shutdown sequence (though C17's own step-3 language ---
    "flush the log" -- names this module's `flush()` as that step's
    eventual body once wired).
  - Does NOT raise past a flush failure -- a telemetry write must never
    be the reason `ctx_shutdown()` (and therefore the whole shutdown
    sequence, since step 3 precedes `os._exit` in `warm.lifecycle`) fails
    to complete.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from coordinator_core import locked_write
from coordinator_core.warm.breadcrumb import svc_dir

__all__ = [
    "EXIT_REASON_SKEW",
    "EXIT_REASON_SUPERSEDED",
    "EXIT_REASON_IDLE_DEMOTION",
    "EXIT_REASON_OPERATOR_STOP",
    "EXIT_REASON_DEGRADED",
    "EXIT_REASONS",
    "TELEMETRY_FILENAME",
    "telemetry_path",
    "ServerTelemetry",
    "CLIENT_COLD_FILENAME",
    "client_cold_path",
    "record_client_cold_fallback",
    "client_cold_count",
    "warm_rate",
    "BOOT_WAIT_FILENAME",
    "boot_wait_path",
    "record_client_boot_wait",
    "boot_wait_samples",
    "SPAWN_EPOCH_ENV",
    "SERVER_BOOT_FILENAME",
    "server_boot_path",
    "record_server_boot",
    "server_boot_samples",
    "ELECTION_LOST_FILENAME",
    "election_lost_path",
    "record_election_lost",
    "election_lost_samples",
    "WORKER_POOL_DEPTH_FILENAME",
    "worker_pool_depth_path",
    "record_worker_pool_depth",
    "worker_pool_depth_samples",
    "DEGRADE_FILENAME",
    "KIND_COLD_RUN",
    "KIND_HOOK_TIMEOUT",
    "KIND_COLD_FAILED",
    "DEGRADE_KINDS",
    "degrade_path",
    "record_degrade",
    "degrade_samples",
    "PUBLISH_WARM_FILENAME",
    "publish_warm_path",
    "record_publish_warm_attempt",
    "publish_warm_samples",
]

EXIT_REASON_SKEW = "skew"
EXIT_REASON_SUPERSEDED = "superseded"
EXIT_REASON_IDLE_DEMOTION = "idle-demotion"
EXIT_REASON_OPERATOR_STOP = "operator-stop"
EXIT_REASON_DEGRADED = "degraded"

EXIT_REASONS = frozenset(
    {
        EXIT_REASON_SKEW,
        EXIT_REASON_SUPERSEDED,
        EXIT_REASON_IDLE_DEMOTION,
        EXIT_REASON_OPERATOR_STOP,
        EXIT_REASON_DEGRADED,
    }
)

TELEMETRY_FILENAME = "telemetry.jsonl"


def telemetry_path(engine_root: Optional[Path] = None) -> Path:
    return svc_dir(engine_root) / TELEMETRY_FILENAME


CLIENT_COLD_FILENAME = "client-cold.jsonl"


def client_cold_path(engine_root: Optional[Path] = None) -> Path:
    """`<svc dir>/client-cold.jsonl` -- the client-side counterpart to
    `telemetry_path()` above, deliberately a SEPARATE file rather than a
    row appended to `TELEMETRY_FILENAME`: that file is one row per SERVER
    life, flushed by the server on its own shutdown path, and a cold
    fallback is, by construction, the one outcome no server process ever
    observes (module docstring's "THE CLIENT IS THE ONLY PROCESS THAT CAN
    OBSERVE A COLD FALLBACK", `warm/client.py`). Follows `svc_dir` for the
    same reason `telemetry_path` does -- one resident-engine on-disk home,
    not a second convention.
    """
    return svc_dir(engine_root) / CLIENT_COLD_FILENAME


def record_client_cold_fallback(
    *,
    engine_root: Optional[Path] = None,
    op: Optional[str] = None,
    pid: Optional[int] = None,
    reason: Optional[str] = None,
) -> None:
    """Append one line recording a cold fallback observed by a CLIENT
    process -- the instrument `warm/client.py`'s `try_warm_dispatch` calls
    on every outcome that sends its caller down the cold dispatch path.

    `reason` (AC3, plan 2026-09-06-the-p90-reopens-on-a-measurement-not-a-
    rebuild.md § C2): one of `warm.client.COLD_BUCKET_SPAWN_TRIGGERING_MISS`
    or `warm.client.COLD_BUCKET_DRAIN_WINDOW_ZERO_BYTE_CLOSE` -- the two
    shared buckets every `_try_warm_dispatch_inner` None-return site
    classifies into, using (not replacing) `client._cold_reason`'s existing
    never-overwrite/first-reason-wins mechanism. Omitted, like `op`/`pid`,
    when the caller cannot name one, so the pre-AC3 rows on disk keep their
    exact shape.

    An APPEND log, matching `ServerTelemetry.flush()`'s own shape and for
    the same reason: many short-lived client processes each contribute at
    most a few rows, and what is worth reading back is the COUNT across
    all of them, not a single process's latest value -- a per-process
    in-memory counter would answer "did the client I am now" and nothing
    a separate reporting process (C2) could ever see.

    Best-effort: never raises, mirroring `ServerTelemetry.flush()`'s own
    contract -- a telemetry write must never be the reason the cold
    fallback itself fails (`warm/client.py`'s Backstop 2: nothing in the
    warm preamble may fail in a way that fails the op).
    """
    record: dict = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    # WHAT A BARE TIMESTAMP COULD NOT ANSWER. This file recorded 1600 rows in
    if op is not None:
        record["op"] = op
    if pid is not None:
        record["pid"] = pid
    if reason is not None:
        record["reason"] = reason
    path = client_cold_path(engine_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked_write.held_lock(path, holder_label="warm.telemetry.client_cold"):
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        return


SPAWN_EPOCH_ENV = "COORDINATOR_WARM_SPAWN_EPOCH"

SERVER_BOOT_FILENAME = "server-boot.jsonl"


def server_boot_path(engine_root: Optional[Path] = None) -> Path:
    return svc_dir(engine_root) / SERVER_BOOT_FILENAME


def record_server_boot(
    *,
    listener_secs: float,
    ready_secs: float,
    pid: int,
    engine_root: Optional[Path] = None,
) -> None:
    """Append one row measuring a warm server's own boot: spawn -> endpoint
    bound (`listener_secs`) and spawn -> ready to answer (`ready_secs`).

    THE MEASUREMENT THREE OTHER FILES CANNOT PRODUCE. Boot time has been
    argued all week from proxies, and every proxy is censored by WHEN
    CALLERS HAPPENED TO CALL: `client-cold.jsonl` samples an outage only
    when someone dispatched into it (42 of 121 windows hold a single miss
    and measure 0s, and the two defensible readings of that file disagree
    9x on the median); `telemetry.jsonl`'s server-succession gaps are
    bounded the other way, because the next server does not start until a
    caller arrives to trigger a spawn, so they measure caller absence as
    much as boot. This row measures the interval directly, inside the
    process whose boot it is, and no caller appears in it at all.

    Both numbers, not one: an endpoint that is bound will accept a
    connection, but the op registry preloads AFTER election
    (`_preload_op_registry`, ~703ms of imports on the first dispatch it
    exists to spare), so "connectable" and "will answer promptly" are
    different instants and a client that reaches the first still waits for
    the second.

    Best-effort: never raises, matching every other recorder here.
    """
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "listener_secs": round(listener_secs, 3),
        "ready_secs": round(ready_secs, 3),
        "pid": pid,
    }
    path = server_boot_path(engine_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked_write.held_lock(path, holder_label="warm.telemetry.server_boot"):
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        return


def server_boot_samples(engine_root: Optional[Path] = None) -> list:
    path = server_boot_path(engine_root)
    rows: list = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows


ELECTION_LOST_FILENAME = "election-lost.jsonl"


def election_lost_path(engine_root: Optional[Path] = None) -> Path:
    return svc_dir(engine_root) / ELECTION_LOST_FILENAME


def record_election_lost(
    *,
    endpoint: str,
    token: Optional[str] = None,
    pid: Optional[int] = None,
    lost_secs: Optional[float] = None,
    engine_root: Optional[Path] = None,
) -> None:
    """Append one row for a boot that ended at `election.ElectionLost` --
    the outcome `warm/server.py :: _run_guarded` previously reported ONLY
    by printing to `sys.stderr`, which `ops.ceremony.detached_spawn.
    spawn_detached` opens as `subprocess.DEVNULL` for every detached child.
    A failed succession attempt reached no file on disk at all, so every
    exit-reason census in the 2026-08-26 succession investigation was blind
    to them and censored upward
    (docs/research/2026-08-26-repo-warm-succession.md § 5.1, § 5.5).

    A SEPARATE FILE, not a `telemetry.jsonl` row, for the same reason
    `client_cold_path` is separate: that file is one row per server LIFE,
    written by `ServerTelemetry.flush()` from a `_ServerContext` -- and a
    losing process has no context, deliberately (the context is not
    constructed until after the election is won). This recorder runs
    pre-context, like `record_server_boot`, and writes only its own file:
    a process that lost the election must never touch the winner's
    artifacts, which is the same invariant `main`'s docstring states for
    the breadcrumb.

    `lost_secs` is spawn -> loss, available only when the spawner stamped
    `SPAWN_EPOCH_ENV`; omitted rather than invented otherwise, matching
    `record_server_boot`'s refusal to guess a start.

    VOLUME. This fires once per LOSING spawn, and losing spawns are most
    numerous under exactly the conditions that already produced 1600 client
    misses in 13 seconds
    (state/bug-backlog/2026-08-26-sixteen-hundred-warm-misses-in-thirteen-
    seconds.yaml). Sized for a burst: one small append under the same
    `held_lock` every writer here uses, and never-raises, so a storm
    degrades to missing rows rather than to failing exits.
    """
    record: dict = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint": endpoint,
    }
    if token is not None:
        record["token"] = token
    if pid is not None:
        record["pid"] = pid
    if lost_secs is not None:
        record["lost_secs"] = round(lost_secs, 3)
    path = election_lost_path(engine_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked_write.held_lock(path, holder_label="warm.telemetry.election_lost"):
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        return


def election_lost_samples(engine_root: Optional[Path] = None) -> list:
    path = election_lost_path(engine_root)
    rows: list = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows


WORKER_POOL_DEPTH_FILENAME = "worker-pool-depth.jsonl"


def worker_pool_depth_path(engine_root: Optional[Path] = None) -> Path:
    """`<svc dir>/worker-pool-depth.jsonl` -- one row per idle-watchdog tick
    that sampled a running server's live `_worker_loop` thread count
    (`warm/server.py :: _ServerContext.worker_pool_depth`, plan
    2026-09-06-warm-engine-survival-and-door-measurement.md T1). A depth
    below `WORKER_POOL_SIZE` (30) is the die-off `_worker_loop`'s own
    `except Exception` guard exists to close -- this is the only recorder
    that can ever observe one, since nothing else counts live worker
    threads."""
    return svc_dir(engine_root) / WORKER_POOL_DEPTH_FILENAME


def record_worker_pool_depth(
    *,
    depth: int,
    pid: int,
    engine_root: Optional[Path] = None,
) -> None:
    """Append one row recording a running server's live worker-thread
    count, sampled on the idle watchdog's own bounded tick
    (`_ServerContext._idle_tick`, every `_IDLE_WATCHDOG_POLL_SECS`) --
    never a new sampling process or loop of its own (CLAUDE.md § Load
    norm).

    Best-effort: never raises, matching every other recorder here.
    """
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "depth": depth,
        "pid": pid,
    }
    path = worker_pool_depth_path(engine_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked_write.held_lock(path, holder_label="warm.telemetry.worker_pool_depth"):
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        return


def worker_pool_depth_samples(engine_root: Optional[Path] = None) -> list:
    path = worker_pool_depth_path(engine_root)
    rows: list = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows


BOOT_WAIT_FILENAME = "client-boot-wait.jsonl"


def boot_wait_path(engine_root: Optional[Path] = None) -> Path:
    return svc_dir(engine_root) / BOOT_WAIT_FILENAME


def record_client_boot_wait(
    *,
    waited_secs: float,
    served: bool,
    deadline_secs: float,
    engine_root: Optional[Path] = None,
) -> None:
    """Append one row for a bounded warm-boot wait entered by a client.

    THE MEASUREMENT NOBODY HAD. Before this, the interval between a
    detached spawn and the first call that server accepted was supplied by
    a human retrying by hand, so every number on record was an operator's
    patience, not a boot. `waited_secs` is measured from the moment the
    client's own dispatch missed (which is also the moment its spawn
    attempt went out) to the moment a warm server served it, or to the
    deadline when none did -- `served` says which. Rows accumulate across
    processes exactly like `record_client_cold_fallback`'s, and are the
    only evidence that can settle whether this box's boot is seconds or
    minutes.

    Best-effort: never raises, same contract as
    `record_client_cold_fallback` -- an instrument may not be the reason
    an op fails.
    """
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "waited_secs": round(waited_secs, 3),
        "served": served,
        "deadline_secs": deadline_secs,
    }
    path = boot_wait_path(engine_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked_write.held_lock(path, holder_label="warm.telemetry.boot_wait"):
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        return


def boot_wait_samples(engine_root: Optional[Path] = None) -> list:
    path = boot_wait_path(engine_root)
    rows: list = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows


def client_cold_count(engine_root: Optional[Path] = None) -> int:
    path = client_cold_path(engine_root)
    try:
        with path.open("r", encoding="utf-8") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def warm_rate(engine_root: Optional[Path] = None) -> dict:
    warm_count = 0
    cold_count = 0

    path = telemetry_path(engine_root)
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                warm_count += record.get("warm_count", 0) or 0
                cold_count += record.get("cold_count", 0) or 0
    except OSError:
        pass

    cold_count += client_cold_count(engine_root)

    total = warm_count + cold_count
    return {
        "warm_count": warm_count,
        "cold_count": cold_count,
        "total": total,
        "warm_rate": (warm_count / total) if total else None,
    }


DEGRADE_FILENAME = "degrade.jsonl"

#: distinction PM ruling 2 draws against the HARNESS-side silent fail-open
KIND_COLD_RUN = "cold_run"

KIND_HOOK_TIMEOUT = "hook_timeout"

KIND_COLD_FAILED = "cold_failed"

DEGRADE_KINDS = frozenset({KIND_COLD_RUN, KIND_HOOK_TIMEOUT, KIND_COLD_FAILED})


def degrade_path(engine_root: Optional[Path] = None) -> Path:
    """`<svc dir>/degrade.jsonl` -- the durable sink `record_degrade`
    appends to, resolved through the same `svc_dir()` every other recorder
    in this module uses. A stderr print into a hook response is not
    something anyone reads a week later (module NEGATIVE-SPEC's sibling
    recorders make the identical argument for their own on-disk homes);
    this file is what makes "running cold for weeks" a fact recoverable
    from disk rather than a reconstructed session transcript."""
    return svc_dir(engine_root) / DEGRADE_FILENAME


def record_degrade(
    *,
    kind: str,
    cause: str,
    engine_root: Optional[Path] = None,
) -> None:
    """Append one attributable, durable row recording a cold run or a
    hook-budget overrun -- the sink `transports.json`'s `degrade_signal`
    field points callers at for `http-hook-loopback` (PM ruling 2).

    `kind` must be one of `DEGRADE_KINDS` (`KIND_COLD_RUN` /
    `KIND_HOOK_TIMEOUT`); an unrecognized kind is a caller bug and raised
    loudly here, matching `ServerTelemetry.record_exit`'s identical
    contract for its own closed `EXIT_REASONS` set -- this instrument must
    never silently accept a kind nothing downstream can attribute.

    `cause` is a short, free-text, human-attributable reason (naming the
    call site and what was observed), never omitted -- an empty cause row
    is exactly the escape clause AC15 exists to close for the schema, and
    this recorder does not reopen it for its own payload.

    Best-effort: never raises past the point `kind` is validated, mirroring
    every other recorder in this module -- an instrument may not be the
    reason the request it is describing also fails.
    """
    if kind not in DEGRADE_KINDS:
        raise ValueError(f"unknown degrade kind: {kind!r}, expected one of {sorted(DEGRADE_KINDS)}")
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kind": kind,
        "cause": cause,
    }
    path = degrade_path(engine_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked_write.held_lock(path, holder_label="warm.telemetry.degrade"):
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        return


def degrade_samples(engine_root: Optional[Path] = None) -> list:
    path = degrade_path(engine_root)
    rows: list = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows


PUBLISH_WARM_FILENAME = "publish-warm.jsonl"


def publish_warm_path(engine_root: Optional[Path] = None) -> Path:
    """`<svc dir>/publish-warm.jsonl` -- one row per publish-path attempt to
    warm the round's successor listener (C9,
    docs/plans/2026-09-01-a-guard-that-cannot-reach-warmth-still-r.md).

    A SEPARATE FILE, not a fold-in to `TELEMETRY_FILENAME` or
    `ELECTION_LOST_FILENAME`: this row is written by the PUBLISHING
    process, at `percolate.round.step_commit`'s call site, never by a
    server life or a losing election -- neither existing file's writer is
    this one, and folding in would blur which process wrote which row."""
    return svc_dir(engine_root) / PUBLISH_WARM_FILENAME


def record_publish_warm_attempt(
    *,
    stamped: bool,
    listener_reachable: Optional[bool] = None,
    engine_root: Optional[Path] = None,
) -> None:
    """Append one row recording a publish-path attempt to warm the round's
    successor listener (C9's spawn-attribution row, folded in from C1 per
    review -- not `record_election_lost`'s mirror, not a `listener_secs`/
    `ready_secs` split, not a dedicated test file: one row).

    `stamped=False` is the POSITIVELY DETECTED unstamped-destination case
    (`warm.engine_root.is_engine_root` returned False against
    `context.dest_repo_root`) -- recorded here explicitly rather than
    silently returning, which is exactly the gap C9's chunk body names
    (finding #4: `ensure_listener`'s `is_engine_root` gate returns `None`
    silently against an unstamped root, which would otherwise make the
    publish-warm attempt spawn nothing, log nothing, and pass any test
    asserting only "no exception").

    `listener_reachable` is omitted when `stamped` is False (no listener
    call was ever attempted); when `stamped` is True it is
    `supervisor.ensure_listener(...)  is not None` -- True means a live
    listener already answered, False means the call fell through to its
    own fail-open spawn-or-debounce path (§ `ensure_listener`'s own
    docstring: never a raise, never a wait for a boot).

    Best-effort: never raises, matching every other recorder in this
    module -- this instrument must never be the reason a publish round
    fails."""
    record: dict = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stamped": bool(stamped),
    }
    if listener_reachable is not None:
        record["listener_reachable"] = bool(listener_reachable)
    path = publish_warm_path(engine_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked_write.held_lock(path, holder_label="warm.telemetry.publish_warm"):
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        return


def publish_warm_samples(engine_root: Optional[Path] = None) -> list:
    path = publish_warm_path(engine_root)
    rows: list = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows


class ServerTelemetry:

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        transport: Optional[str] = None,
        engine_token: Optional[str] = None,
    ):
        # `engine_token` names WHICH ENGINE GENERATION this life served, and
        # WHY IT IS LOAD-BEARING RATHER THAN DECORATIVE: `supervisor_pipe_name`
        # new-token generation elect on DISTINCT pipe names and legitimately
        self._lock = threading.Lock()
        self._clock = clock
        self._transport = transport
        self._engine_token = engine_token
        self._started_monotonic = clock()
        self._served_count = 0
        self._warm_count = 0
        self._cold_count = 0
        self._exit_reason: Optional[str] = None
        self._exit_detail: Optional[str] = None

    def record_invocation(self, *, warm: bool) -> int:
        with self._lock:
            self._served_count += 1
            if warm:
                self._warm_count += 1
            else:
                self._cold_count += 1
            return self._served_count

    def served_count(self) -> int:
        with self._lock:
            return self._served_count

    def record_exit(self, reason: str, detail: Optional[str] = None) -> None:
        """Record why this server is exiting. `reason` must be one of
        `EXIT_REASONS` (skew / superseded / idle-demotion / operator-stop /
        degraded).

        `detail` is an optional free-text refinement of `reason`, surfaced
        as `exit_detail` and OMITTED when absent, so every row written
        before this existed keeps its exact shape and no reader has to
        learn a new key to keep working. Its first use is the skew axis
        (`warm.skew.SKEW_AXIS_SOURCE` / `SKEW_AXIS_TOKEN`, comma-joined
        when both hold): `skew` was the largest exit reason on this box and
        collapsed two mechanisms whose remediations point in opposite
        directions, so the aggregate could not tell anyone which one to go
        fix.

        First call wins -- a server exits at most once (`warm.lifecycle`'s
        single-shot guard), so a second call is a caller bug, not a
        legitimate second exit; it is silently ignored rather than raised,
        since telemetry recording must never be the reason a shutdown
        sequence fails (module docstring's negative-spec).
        """
        if reason not in EXIT_REASONS:
            raise ValueError(f"unknown exit reason: {reason!r}, expected one of {sorted(EXIT_REASONS)}")
        with self._lock:
            if self._exit_reason is None:
                self._exit_reason = reason
                self._exit_detail = detail

    def snapshot(self) -> dict:
        with self._lock:
            record = {
                "served_count": self._served_count,
                "warm_count": self._warm_count,
                "cold_count": self._cold_count,
                "exit_reason": self._exit_reason,
                "life_seconds": self._clock() - self._started_monotonic,
            }
            if self._exit_detail is not None:
                record["exit_detail"] = self._exit_detail
            if self._transport is not None:
                record["transport"] = self._transport
            if self._engine_token is not None:
                record["engine_token"] = self._engine_token
            return record

    def flush(self, *, engine_root: Optional[Path] = None) -> None:
        record = self.snapshot()
        record["flushed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        path = telemetry_path(engine_root)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with locked_write.held_lock(path, holder_label="warm.telemetry"):
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            return

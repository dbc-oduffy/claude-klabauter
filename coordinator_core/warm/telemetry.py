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
    """`<svc dir>/telemetry.jsonl` for `engine_root` -- the same
    directory `warm.breadcrumb.svc_dir` resolves, see module docstring's
    "ON-DISK SHAPE"."""
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
) -> None:
    """Append one line recording a cold fallback observed by a CLIENT
    process -- the instrument `warm/client.py`'s `try_warm_dispatch` calls
    on every outcome that sends its caller down the cold dispatch path.

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
    # 13 seconds on 2026-08-25 (~123/s) -- a burst that is, by the shape of
    # this instrument, many short-lived processes each taking one miss rather
    # than one process retrying. Which processes, and running which op, was
    # unanswerable from the rows, so the defect could not be chased at all
    # (state/bug-backlog/2026-08-26-sixteen-hundred-warm-misses-in-thirteen-
    # seconds.yaml). Both keys are OMITTED when unknown, so the six days of
    # rows already on disk keep their exact shape and a caller that cannot
    # name its op is not made to invent one.
    if op is not None:
        record["op"] = op
    if pid is not None:
        record["pid"] = pid
    path = client_cold_path(engine_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with locked_write.held_lock(path, holder_label="warm.telemetry.client_cold"):
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        return


#: Env var carrying the spawner's `time.time()` at the moment it launched a
#: warm server, read by that server to measure its OWN boot. Set by
#: `warm.client._spawn_once`; absent for any other spawn route, which is why
#: `record_server_boot` omits the row entirely rather than guessing a start.
SPAWN_EPOCH_ENV = "COORDINATOR_WARM_SPAWN_EPOCH"

SERVER_BOOT_FILENAME = "server-boot.jsonl"


def server_boot_path(engine_root: Optional[Path] = None) -> Path:
    """`<svc dir>/server-boot.jsonl` -- one row per server that booted from a
    stamped spawn."""
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
    """Every recorded server-boot row, oldest first. Absent file reads as an
    empty list; an unparseable row is skipped, not fatal."""
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
    """`<svc dir>/election-lost.jsonl` -- one row per spawned server that
    lost its generation's election and exited without ever serving."""
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
    """Every recorded election-lost row, oldest first. Absent file reads as
    an empty list; an unparseable row is skipped, not fatal."""
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


BOOT_WAIT_FILENAME = "client-boot-wait.jsonl"


def boot_wait_path(engine_root: Optional[Path] = None) -> Path:
    """`<svc dir>/client-boot-wait.jsonl` -- one row per bounded wait a
    client actually entered, alongside `client_cold_path()` and for the
    same reason it is separate from the server's own file: only the
    CLIENT can observe how long it waited for a server to start
    answering."""
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
    """Every recorded boot-wait row, oldest first -- a plain file read, no
    running server required. Absent file reads as an empty list, and an
    unparseable row is skipped rather than failing the read (the file is
    append-only from many processes; a torn line is a lost sample, not a
    corrupt instrument)."""
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
    """The number of client-side cold fallbacks recorded by
    `record_client_cold_fallback` -- reachable by a plain file read, with
    NO warm-pipe round trip and no running server required (the counter
    this chunk exists to make reachable: AC4's first half). Absent file
    (no cold fallback has ever been recorded, or `svc_dir` has never been
    created) reads as zero, not an error.
    """
    path = client_cold_path(engine_root)
    try:
        with path.open("r", encoding="utf-8") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def warm_rate(engine_root: Optional[Path] = None) -> dict:
    """The one-command answer to "is warmth serving on this box right
    now" -- AC4's second half. Deliberately not an alerting system: this
    is a plain read-and-compute over the two on-disk logs this module
    already owns, not a new push/pull signal or a new file.

    Server-side `ServerTelemetry.flush()` rows only ever carry `warm_count`
    (each recorded invocation reached a running server, so it is warm by
    construction -- `_serve_line` has no cold path to record, see
    `warm/client.py`'s C1 fix for why cold can only be observed
    client-side) plus whatever `cold_count` any given row happens to
    carry. `client_cold_count()` (C1) is the population no server row can
    ever see: a client that fell cold never contacted a server at all.
    Both are summed here so the reported rate reflects every observed
    outcome across BOTH populations, not just the server's partial view.

    Returns a dict with `warm_count`, `cold_count`, `total`, and
    `warm_rate` (a 0..1 float, or `None` when `total` is zero -- no
    outcomes recorded yet is a distinct answer from "0% warm", not an
    error).
    """
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

#: A request WAS delivered to a transport handler and this process chose
#: (or was forced) to answer without the served warm response -- the
#: distinction PM ruling 2 draws against the HARNESS-side silent fail-open
#: `http-hook-loopback`/`http-front-door`'s own `cannot_observe_reason`
#: already names: that reason is honest about the caller never reaching us
#: at all, and silent about what happens once one does. `kind="cold_run"`
#: is this module's answer for the latter.
KIND_COLD_RUN = "cold_run"

#: A request WAS served, but the serving took long enough to be
#: indistinguishable, from the operator's chair, from the box being
#: unreachable -- the "UserPromptSubmit hook timed out after 5s" case the
#: PM named. Recorded from inside the handler that measured its own
#: elapsed time, not inferred from a caller-side timeout.
KIND_HOOK_TIMEOUT = "hook_timeout"

#: The cold rung ITSELF failed -- a caller that had already exhausted the
#: warm listener asked for a verdict in process and the guard chain could
#: not produce one either. DR-402's rung 3: the act proceeds, and this row
#: is the whole of what makes that proceed accountable afterwards.
#: Distinct from `cold_run` on purpose. A cold run that answered and a cold
#: run that collapsed are the same event up to the moment the chain is
#: entered, so recording both under one kind would make the box report its
#: guards as running cold when they are not running at all -- precisely the
#: "running cold for weeks" blindness PM ruling 2 named, one rung lower
#: down and correspondingly worse.
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
    """Every recorded degrade row, oldest first. Absent file reads as an
    empty list; an unparseable row is skipped, not fatal -- matches every
    other `*_samples` reader in this module."""
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
    """Every recorded publish-warm row, oldest first. Absent file reads as
    an empty list; an unparseable row is skipped, not fatal -- matches
    every other `*_samples` reader in this module."""
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
    """One instance per server life. Thread-safe counters on an
    already-open structure -- every connection thread in `warm.server`
    may call `record_invocation` concurrently, so all mutation is behind
    a single lock, mirroring `warm.server.InFlightCounter`'s own shape.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        transport: Optional[str] = None,
        engine_token: Optional[str] = None,
    ):
        # `transport` names which transport's life this row describes, and is
        # OMITTED from `snapshot()` when None. That default is what keeps the
        # pipe server's rows byte-identical to the ~seven days already on disk.
        # It exists because the HTTP transport was untelemetered until
        # 2026-08-26 and, once it is not, both transports append to the SAME
        # `telemetry.jsonl` -- an undifferentiated file would silently change
        # the denominator under every existing census. Absence therefore means
        # "the pipe server, or a row written before this field", and a reader
        # separating the two populations filters on the presence of this key
        # rather than inferring one.
        # `engine_token` names WHICH ENGINE GENERATION this life served, and
        # is OMITTED from `snapshot()` when None, on the identical contract as
        # `transport` above -- absence means "a row written before this field",
        # never "no token".
        #
        # WHY IT IS LOAD-BEARING RATHER THAN DECORATIVE: `supervisor_pipe_name`
        # embeds `skew.compute_client_token(root)`, so an old-token and a
        # new-token generation elect on DISTINCT pipe names and legitimately
        # coexist during a stamp rotation's drain window. A concurrent-listener
        # high-water taken across the whole file therefore counts that designed
        # overlap as if it were orphaning, and can never fall to 1 no matter how
        # correct the election is. Keying lifetimes by this field is what turns
        # that census from an unfalsifiable global number into the per-generation
        # one the single-instance property is actually about.
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
        """Record one served invocation as warm or cold. Returns the
        running served-invocation count (post-increment) -- the module
        docstring's point 3, and the exact value `served_count()` (below)
        also returns, kept in sync under the same lock.
        """
        with self._lock:
            self._served_count += 1
            if warm:
                self._warm_count += 1
            else:
                self._cold_count += 1
            return self._served_count

    def served_count(self) -> int:
        """Zero-arg served-invocation count -- the exact shape C24's
        `idle.ServedCountFn` expects, so this method binds directly into
        `idle.should_demote(served_count=telemetry.served_count, ...)`
        with no adapter (module docstring's "WHAT THIS MODULE RECORDS",
        point 3).
        """
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
        """A point-in-time dict of this server life's counters -- the
        record shape `flush()` appends, also useful directly in tests
        without touching disk.
        """
        with self._lock:
            record = {
                "served_count": self._served_count,
                "warm_count": self._warm_count,
                "cold_count": self._cold_count,
                "exit_reason": self._exit_reason,
                "life_seconds": self._clock() - self._started_monotonic,
            }
            # Present only when a detail was actually recorded, which keeps
            # every row written before this field, and every reader of them,
            # working unchanged.
            #
            # THE COST OF THAT, NAMED SO NOBODY READS IT AS A RESULT: absence
            # is ambiguous. A `skew` row with no `exit_detail` is either a
            # pre-2026-08-26 row that could not carry one or a server that
            # recorded none, and nothing in the file tells them apart. The 112
            # historical skews in this box's seven-day file therefore cannot be
            # attributed to an axis, ever -- an axis split is a FORWARD
            # measurement over rows written after `584c452b5`, not a re-read of
            # what is already on disk. Anyone who goes back to the old rows
            # will find no axes and must not conclude the axes were absent
            # (claude-klabauter-22, 2026-08-26).
            if self._exit_detail is not None:
                record["exit_detail"] = self._exit_detail
            if self._transport is not None:
                record["transport"] = self._transport
            if self._engine_token is not None:
                record["engine_token"] = self._engine_token
            return record

    def flush(self, *, engine_root: Optional[Path] = None) -> None:
        """Append this server life's `snapshot()` (plus a wall-clock
        `flushed_at`) as one JSON line to `telemetry_path()`. Best-effort:
        never raises past a lock timeout or `OSError`, mirroring
        `warm.breadcrumb.unlink_breadcrumb`'s "never raises" contract --
        this is meant to be called from C17's `ctx_shutdown` step, which
        must complete before `os._exit(0)` regardless of whether the
        telemetry write itself succeeded (module docstring's
        negative-spec).
        """
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

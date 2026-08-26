"""
coordinator_core.telemetry.op_latency — durable per-op wall-clock record.

Purpose: the engine records per-op wall-clock nowhere, ever (2026-08-08 static
sweep of state/handoffs/2026-08-08-engine-fails-the-load-norm.md) — dispatch_message
times nothing, cc_invoke.py enforces timeout budgets without measuring elapsed, and
the only existing timing site (coordinator_core.benchmarks.timer.time_invocation)
is harness-only with a durable sink that does not exist on disk. This module is the
real-traffic measurement instrument the load-norm investigation was granted (PM
grant, not a remedy) — it records facts about every op invocation so they can later
be joined against docs/wiki/machine-load-norm.md's ambient-load claim. It performs
NO remediation: no timeout is read or changed here.

Sink path: <git_common_dir>/coordinator-sessions/logs/op-latency.jsonl — the
established untracked-log home (logs/agent-audit.jsonl already lives there; see
coordinator_core.hooks.agent_completion_log for the sibling writer this module's
append discipline was checked against, tightened to a single atomic os.write of a
pre-encoded line rather than a buffered file-object append, per this module's own
concurrency requirement below).

Negative-spec (hard-won):
    - Never breaks dispatch. record_op_latency() and record_op_started() both
      swallow every exception — unwritable dir, full disk, permission error —
      to a debug log; a telemetry defect must never fail a peer's op. Mirrors
      the import-time _warn_on_near_miss_timeout_env try/except pattern in
      coordinator_core.ipc.
    - No read-modify-write, no lock file: concurrency safety comes from a single
      os.write() of one pre-encoded ``line + "\\n"`` via
      coordinator_core.atomic_append.append_line — plain O_APPEND on POSIX
      (genuinely atomic for a single small write there), but NOT plain
      O_APPEND on Windows, where the CRT only emulates append via seek-then-
      write and two processes can silently clobber each other's line (see
      that module's docstring for the reproduced failure and the
      CreateFileW/FILE_APPEND_DATA fix it uses instead). The completion row
      is a second, independent append — never a mutation of the started row;
      there is no read-modify-write "close out" step anywhere in this module.
    - "timeout" outcome means the CALLER gave up, not that the handler stopped —
      see coordinator_core.ipc's DISPATCH_TIMEOUT_SECS negative-spec: a client-side
      timeout does not cancel a blocking-sync handler thread, which keeps running
      to completion (and can still commit) after the caller unblocks.
    - Kill switch: COORDINATOR_OP_LATENCY_DISABLE=1 hard-disables recording,
      checked first and cheaply (single os.environ.get). Default is ON — the
      point is capturing ambient traffic without asking every peer to opt in.
      Applies identically to BOTH row kinds (started and complete).
    - Cheap: no heavy imports at module import time (json/os/time are stdlib and
      already paid for elsewhere on this hot path); no corpus walk, no process
      spawn, no live-session resolution. Budget: well under 1ms. The
      correlation id (below) is built from os.getpid() + time.perf_counter_ns()
      — no uuid import, since uuid4's os.urandom call is measurably heavier
      than a monotonic counter read and this sits on the same hot path.
    - Vanished vs timed-out (the reconcile-hazard distinction this row kind
      exists for): a "started" row with no matching "complete" row means the
      invocation VANISHED — the process was killed (most likely by a
      client-side ``subprocess.run(timeout=)`` in cc_invoke) before it ever
      reached the ``finally`` block that writes the completion row. This is
      DISTINCT from a "complete" row with ``outcome: "timeout"``, which means
      the caller gave up waiting but the handler kept running and MAY STILL
      COMMIT (see the "timeout" outcome note above). Both are reconcile
      hazards an operator must treat differently: a vanished invocation left
      no completion row at all, so whether it committed is unknown from this
      sink alone; a timed-out-but-complete invocation has a recorded outcome
      and elapsed time. Only the second was visible before this row kind was
      added — pairing "started" against "complete" by ``corr_id`` is what
      makes the first population countable instead of merely inferred.
    - Row kind and backward reading: every row now carries an explicit
      ``"kind"`` field, ``"started"`` or ``"complete"``. Rows already on disk
      before this field existed have no ``"kind"`` key at all — readers MUST
      treat an absent ``"kind"`` as ``"complete"`` (every pre-existing row was
      written by the single old code path, which only ever recorded finished
      invocations). A reader must never treat a missing ``"kind"`` as
      ``"started"``.
    - Never restore an early ``return`` on a falsy ``repo_root`` in
      ``_write_entry``. A None ``repo_root`` means the JSON-RPC envelope
      carried no ``_origin_worktree`` (see ``ipc.resolve_request_repo``) — it
      does NOT mean the invocation happened outside a repo, and dropping the
      row on that condition makes the op invisible rather than making the
      instrument cheaper. Measured case: ``hooks.postuse_advisory_dispatch``
      fires on every PostToolUse ``Write|Edit|MultiEdit|NotebookEdit|Agent``
      and recorded ZERO rows in 85 hours under the old drop-on-None behavior
      — absent from every ranking built on this ledger while plausibly the
      single largest engine consumer. An instrument with a silent blind spot
      cannot support the kill disposition's budget rule
      (docs/wiki/cost-budgets-and-the-kill-disposition.md: measurement
      answers "does this fit"). ``_write_entry`` now falls back to
      ``Path.cwd()`` instead and stamps ``repo_key_source`` so the row is
      still written, just flagged as lower-confidence attribution — see
      ``repo_key_source`` below.
    - ``repo_key_source`` (``"envelope"`` or ``"cwd"``): every row carries
      this field. ``"envelope"`` means ``repo_key`` came from a repo_root the
      caller of ``_write_entry`` supplied explicitly; ``"cwd"`` means no
      repo_root was supplied at all and the sink was resolved from THIS
      process's own working directory instead — the row is real, but the
      repo attribution is inferred, not asserted by the caller. Analysis
      joining rows across repos should treat ``cwd``-sourced rows as
      lower-confidence attribution.

      As of C7 (2026-08-20-a-refusal-cannot-exit-zero), "explicit" above
      covers two upstream cases that both read as ``"envelope"`` here, by
      design: the caller's own ``_origin_worktree`` (worktree-scoped ops), OR
      ``coordinator_core.ipc.resolve_caller_cwd``'s ``_caller_cwd`` envelope
      field (a "none"-scoped op, which never carries ``_origin_worktree`` —
      see ``invoke.__main__.main``'s ``WORKTREE_SCOPED_OPS`` gate). Both are
      the CALLER's own process cwd/worktree, asserted at the point the
      request was built, never THIS (possibly warm-server) process's cwd —
      the distinction this field exists to preserve is caller-asserted vs
      locally-inferred, not which envelope field carried it. Without the
      second case, a "none"-scoped op served warm fell through to this
      module's own ``Path.cwd()`` fallback, which in a warm pool worker is
      the SERVER's cwd (the klabauter clone) — attributing every such row to
      the wrong repo while reading as ordinary ``"cwd"``-sourced telemetry,
      corrupting the very denominator the warmth sweeps measure.

    - Pairing reader staleness (C2 / AC3): ``pairing_summary``'s
      ``staleness_cutoff_secs`` default (see that function) is derived from
      ``coordinator/bin/lib/cc_invoke.py``'s own ``_op_timeout_ceiling``:
      ``max(FLOOR=10, engine_budget(op)=DISPATCH_TIMEOUT_SECS default 30 +
      MARGIN=10) == 40`` seconds — the longest a client-side
      ``subprocess.run(timeout=)`` waits before killing the child for any op
      that has not overridden its budget. A "started" row younger than that
      is simply an invocation still in flight, not a vanished one.

Spec backlink: state/handoffs/2026-08-08-engine-fails-the-load-norm.md
               docs/wiki/machine-load-norm.md
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from coordinator_core import atomic_append

_DISABLE_ENV = "COORDINATOR_OP_LATENCY_DISABLE"

#: Execution routes a logical op can take. THE one place this set is stated
#: (AC6c, docs/plans/2026-08-19-the-fired-path-reaches-the-engine.md § C12).
#:
#: The invariant AC6c needs is MUTUAL EXCLUSION PER LOGICAL OP, not writer
#: ownership: a given op takes exactly one of these routes, never two. Writer
#: ownership alone does not hold the line -- a call that goes over `http` while
#: a shim also calls `ipc.dispatch_from_hook` emits a second
#: `record_op_started`/`record_op_latency` pair however careful each writer is,
#: and that double-count corrupts the traffic census the warm-engine win figures
#: are derived from. Cross-process is the gap: `dispatch_message` is already the
#: sole chokepoint WITHIN a process, so nothing below this line is about
#: double-counting inside one interpreter.
#:
#: Ownership rule, once exclusion holds: the process that EXECUTES the op owns
#: the record. In-process route -> the caller's own hook process; http route ->
#: the warm server.
IN_PROCESS = "in_process"
WARM_SERVER = "warm_server"
HTTP_SERVER = "http_server"
EXECUTION_ROUTES = frozenset({IN_PROCESS, WARM_SERVER, HTTP_SERVER})

#: A serving process declares its own route here (PUBLIC: `warm.server.
#: _declare_execution_route` writes it; this module only ever reads it). Deliberately an env var and
#: not an import: this module sits on the dispatch hot path and must not import
#: `warm.server` (or anything that imports the engine) to answer the question.
ROUTE_ENV = "COORDINATOR_EXECUTION_ROUTE"


def execution_route() -> str:
    """Route the CURRENT process executes ops under.

    Defaults to ``IN_PROCESS``, which is what every caller that has not
    declared itself a server is. Never raises and never rejects: an
    unrecognised value degrades to ``IN_PROCESS`` rather than failing a peer's
    op, per this module's never-breaks-dispatch contract. A wrong route label
    costs one mislabelled census row; a raise here costs the op.
    """
    declared = os.environ.get(ROUTE_ENV)
    return declared if declared in EXECUTION_ROUTES else IN_PROCESS


#: Invocation ORIGIN — what kind of caller produced this row. Orthogonal to
#: `route` above, which records the TRANSPORT (which process served the op) and
#: says nothing about the nature of the caller. Both ship: a warm-server row and
#: a benchmark row are different facts, and neither answers the other's question.
#:
#: Why this exists (the contamination the census could not see): `ping` recorded
#: 10,832 completions in seven days, none of them production traffic — five
#: benchmark modules (`benchmarks/floor.py`, `harness.py`, `interleave.py`,
#: `concurrency_probe.py`, `op_fixtures.py`) time it as the engine's bare-invoke
#: floor. Before this field, an in-process test dispatch and a real one were
#: indistinguishable on disk, so EVERY completion count used to convict an op
#: was contaminated by an unknown amount. Measured 2026-08-25 against the live
#: sink: `route` and `source_path` both describe transport, and exactly 9 rows in
#: a 29,596-row generation carried a fixture-shaped `t_start` — so no read-time
#: heuristic could recover origin either. It has to be written down at the sink.
PRODUCTION = "production"
TEST = "test"
BENCHMARK = "benchmark"
INVOCATION_ORIGINS = frozenset({PRODUCTION, TEST, BENCHMARK})

# Review: coordinatorcode-reviewer -- readers are told "treat absent origin as
# unknown, never as production" but had nothing to spell that with, and
# `entry.get("origin", PRODUCTION)` is the tempting wrong reach given this
# module's own default direction. This is what a READER substitutes for a
# missing "origin" key on a pre-existing row -- the writer (invocation_origin
# above) never returns it and never will. Deliberately NOT added to
# INVOCATION_ORIGINS: that frozenset gates what a caller may DECLARE, and
# nobody may declare themselves unknown.
UNKNOWN = "unknown"

#: A harness declares its own origin here; benchmark runners set it to
#: `BENCHMARK`. Same env-var-not-import discipline as ROUTE_ENV: this module is
#: on the dispatch hot path and must not import a harness to ask what it is.
ORIGIN_ENV = "COORDINATOR_INVOCATION_ORIGIN"

#: pytest stamps this per test. Reading it is how a test dispatch self-identifies
#: without every test file having to remember to declare anything — the failure
#: mode of an opt-in-only tag is that the tests which forget are exactly the ones
#: contaminating the census.
_PYTEST_ENV = "PYTEST_CURRENT_TEST"


def invocation_origin() -> str:
    """Origin of the invocation the CURRENT process is recording.

    Precedence: an explicit ``ORIGIN_ENV`` declaration wins; otherwise a live
    ``PYTEST_CURRENT_TEST`` (pytest genuinely running in THIS interpreter)
    means ``TEST``; otherwise ``PRODUCTION``.

    Defaults to ``PRODUCTION`` deliberately, and this is the one place the
    default direction is arguable. An unrecognised or absent declaration reads
    as production traffic, so a harness that forgets to declare itself
    CONTAMINATES rather than disappears. The alternative default hides real ops
    from their own census, which is the worse failure: an over-counted op is
    visibly wrong to anyone who reads the row, an absent one is invisible to
    everyone (see this module's `repo_key_source` fallback for the same call
    made the same way, and the 85-hour blind spot that motivated it).

    # Review: coordinatorcode-reviewer -- PYTEST_CURRENT_TEST is inherited as
    # an env-var SNAPSHOT at spawn time, not live-linked to the parent. A
    # long-lived process (a warm server, most concretely) booted by a test
    # fixture keeps that stale env var baked in for its entire life; if it
    # later serves genuine interactive traffic -- exactly what
    # `route=warm_server` exists to describe -- every row it writes would
    # still read TEST, silently deleting real traffic from the production
    # census. That is the same invisible-to-everyone failure this function's
    # PRODUCTION default claims to avoid, just reached via a stale
    # declaration rather than an absent one. Gating on `"pytest" in
    # sys.modules` as well as the env var means only a process where pytest
    # is ACTUALLY running reads as TEST; a spawned/forked child that merely
    # inherited the env var but never imported pytest reads as PRODUCTION.

    Never raises, per the never-breaks-dispatch contract: two `os.environ.get`
    calls, one dict-membership check against `sys.modules` (no import --
    `sys` is always already loaded), well inside this module's sub-1ms budget.
    """
    declared = os.environ.get(ORIGIN_ENV)
    if declared in INVOCATION_ORIGINS:
        return declared
    if os.environ.get(_PYTEST_ENV) and "pytest" in sys.modules:
        return TEST
    return PRODUCTION


def double_routed_corr_ids(entries) -> set:
    """`corr_id`s that appear under more than one execution route.

    A non-empty result is the AC6c violation made visible: the same logical op
    was recorded by two processes, so the census counts it twice. Returns a set
    rather than raising -- this is a census reader, not a guard, and the rows it
    reads are already on disk by the time anyone asks.
    """
    seen: dict = {}
    doubled = set()
    for entry in entries:
        corr_id = entry.get("corr_id")
        route = entry.get("route")
        if corr_id is None or route is None:
            continue
        prior = seen.setdefault(corr_id, route)
        if prior != route:
            doubled.add(corr_id)
    return doubled


# See atomic_append.IS_WINDOWS for why this is os.name and not platform.system().
_IS_WINDOWS = atomic_append.IS_WINDOWS

_logger = None


def _log():
    global _logger
    if _logger is None:
        import logging
        _logger = logging.getLogger(__name__)
    return _logger


def _sink_path(git_common_dir_path: Path) -> Path:
    """Resolve the op-latency sink path under the given git common dir."""
    return Path(git_common_dir_path) / "coordinator-sessions" / "logs" / "op-latency.jsonl"


def tail_entries(path, *, tail_bytes: int, max_rows: int):
    """Parse the LAST ``tail_bytes`` of one JSONL generation, newest rows kept.

    Returns ``(entries, head_truncated)``. Promoted here (2026-08-21) from
    ``ops.op_budget_breaches._tail_entries``, which still calls through under
    its old name: it is a SINK READER, and two ops holding two copies of how
    to read this sink is how the two drift into disagreeing about the same
    rows. ``op_census_report`` read the whole generation head-first and paid
    250-312ms of `json.loads` for it against DR-344's 200ms per-process bar,
    while its sibling had already bounded the same read — the duplication is
    what let one op carry a fix the other did not.

    Why a byte bound and not ``engine_report.iter_sink_entries``'s row bound:
    that reader walks generations OLDEST-first and caps total lines read, a
    shape its own docstring flags as unable to protect recency, and a row cap
    set above the live row count bounds nothing at all — the parse cost tracks
    sink GROWTH. A byte bound is flat against growth, and recency is what both
    consumers need.

    No semantics live here. Which rows count, and as what, belongs entirely to
    the caller's aggregator, so there is no second opinion about a row to drift
    from the first. The first line after the seek is almost always a partial
    row and is dropped. Never raises: a missing or unreadable generation yields
    ``([], False)``.
    """
    entries: list = []
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            start = max(0, size - tail_bytes)
            if start:
                fh.seek(start)
                fh.readline()
            for raw_line in fh:
                if len(entries) >= max_rows:
                    break
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line.decode("utf-8", errors="replace"))
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
    except OSError:
        return [], False
    return entries, size > tail_bytes


def sink_generations(repo_root: Path) -> list:
    """The op-latency sink plus its rotated generations, NEWEST-FIRST.

    Promoted out of ``coordinator_core.telemetry.cost_census._sink_paths``
    (2026-08-19, plan ``2026-08-19-warm-engine-gets-an-honest-instrument``
    C1) to a supported surface beside the ``_sink_path`` it wraps.
    ``cost_census._sink_paths`` is now a thin call-through to this function
    so its existing (newest-first) output shape and every existing caller
    are unchanged.

    Walks ``log_rotation.py``'s naming convention (``X.jsonl``,
    ``X.1.jsonl``, ...) read-only — does not import or call into that
    module. Returns only generations that exist on disk (``is_file()``),
    resolved relative to ``repo_root`` via ``git_common_dir``. Returns an
    empty list rather than raising if the git common dir cannot be
    resolved — this is a reader over a sink several live processes are
    appending to, never a source of truth for repo identity.
    """
    from coordinator_core.lifecycle import git_common_dir

    try:
        common_dir = git_common_dir(repo_root)
    except (RuntimeError, OSError):
        return []

    sink = _sink_path(common_dir)
    paths = [sink]
    stem, suffix = sink.stem, sink.suffix
    generation = 1
    while True:
        candidate = sink.with_name(f"{stem}.{generation}{suffix}")
        if not candidate.is_file():
            break
        paths.append(candidate)
        generation += 1
    return [p for p in paths if p.is_file()]


#: Modules internal to the dispatch chokepoint itself -- never the fact
#: `caller_module()` exists to name. A frame whose `__name__` equals one of
#: these, or is a dotted child of one, is skipped rather than reported: it is
#: this instrument's own plumbing (this module, `coordinator_core.ipc`'s
#: `dispatch_message` wrapper) or the asyncio machinery that schedules a
#: coroutine between the caller's own call and the frame that actually runs
#: it (`loop.run_until_complete(dispatch_message(msg))` interposes several
#: `asyncio.*` frames between the caller and this point -- see module-level
#: "Caller provenance" note below for the traced shape). Every OTHER
#: `coordinator_core.*` submodule is left un-skipped on purpose: an op
#: handler that itself calls back into `dispatch_message` (rare, but not
#: forbidden) should attribute to ITS OWN module, not disappear into this
#: skip list merely for sharing the `coordinator_core` package prefix.
_CALLER_SKIP_PREFIXES = (
    "coordinator_core.telemetry.op_latency",
    "coordinator_core.ipc",
    "asyncio",
)

#: Bound on how many frames `caller_module()` walks before giving up. A
#: finite, small cap -- never an unbounded walk up the stack -- keeps this
#: within the module's own sub-1ms "Cheap" budget even in the deepest
#: observed call shape (dispatch_message -> several asyncio scheduling
#: frames -> the caller).
_CALLER_WALK_MAX_FRAMES = 20


def caller_module() -> Optional[str]:
    """Best-effort ``__name__`` of the invoking module/entry point.

    Caller provenance (why C1 exists): `dispatch_message` is the SOLE
    process-level dispatch chokepoint (see its own docstring), so its one
    `record_op_started`/`record_op_latency` call site cannot distinguish a
    CLI invocation (`coordinator_core.invoke.__main__`) from a direct-import
    dispatch (`coordinator_core.ops.check_auto_reconcile.get_response`) from
    a warm-server pool worker -- 63 of 65 `handoff.reconcile_open` rows
    carried no attribution at all before this. Walking the call stack from
    inside the dispatch chokepoint is the only way to answer "who fired
    this" without threading a new parameter through every existing caller
    (out of this chunk's `writes:` scope) -- see `_CALLER_SKIP_PREFIXES` for
    which frames are the chokepoint's own plumbing rather than the answer.

    Traced shape for an event-loop-mediated caller (e.g.
    ``loop.run_until_complete(dispatch_message(msg))``): the frame that
    calls `dispatch_message` does not itself appear directly above this
    function's frame -- several `asyncio.tasks`/`asyncio.base_events` frames
    interpose while the loop schedules and resumes the coroutine. Skipping
    the `asyncio` prefix (rather than only this module's own) is what lets
    the walk reach past that scheduling machinery to the real caller.

    Returns the first frame's `__name__` that is not itself skipped, or
    ``None`` if the walk is exhausted (`_CALLER_WALK_MAX_FRAMES`) or the
    module cannot be determined -- never a free-text label, and never a
    guess: an unattributed row stays unattributed rather than fabricating
    an identity for it.

    Never raises: `sys._getframe(1)` raising ``ValueError`` (called with no
    caller on the stack -- effectively unreachable in real use, but
    defensive per this module's never-breaks-dispatch contract) is caught
    and treated as unknown. Pure frame-object attribute reads, no stat, no
    filesystem walk, no import -- well inside the module's sub-1ms budget.
    """
    try:
        frame = sys._getframe(1)
    except ValueError:
        return None
    depth = 0
    while frame is not None and depth < _CALLER_WALK_MAX_FRAMES:
        name = frame.f_globals.get("__name__")
        if isinstance(name, str) and not any(
            name == prefix or name.startswith(prefix + ".") for prefix in _CALLER_SKIP_PREFIXES
        ):
            return name
        frame = frame.f_back
        depth += 1
    return None


def new_correlation_id() -> str:
    """Build a correlation id unique across concurrent processes on this box.

    ``f"{pid}-{perf_counter_ns()}"``: pid alone is insufficient (a single
    process dispatches many ops, and pids get recycled across processes), so
    it is paired with a per-process monotonic nanosecond counter. Deliberately
    NOT ``uuid`` — see module docstring's "Cheap" requirement; this sits on
    the same hot path record_op_latency does.
    """
    return f"{os.getpid()}-{time.perf_counter_ns()}"


def _write_entry(entry: dict, repo_root: Optional[Path]) -> None:
    """Shared append body for both row kinds: resolve sink, encode, atomic-append.

    Never raises — see module docstring's "Never breaks dispatch" negative-spec.
    Both ``record_op_latency`` and ``record_op_started`` funnel through this
    single function so there is exactly one append discipline (one pre-encoded
    line, one ``atomic_append.append_line`` call) rather than two independently
    maintained copies of it.
    """
    try:
        if os.environ.get(_DISABLE_ENV) == "1":
            return

        entry["route"] = execution_route()
        entry["origin"] = invocation_origin()

        key_source = "envelope"
        if repo_root is None:
            # A None repo_root means the JSON-RPC envelope carried no
            # `_origin_worktree` (see ipc.resolve_request_repo) — it does NOT mean
            # the invocation happened outside a repo. Dropping the row here made
            # every such op invisible to this instrument: `hooks.postuse_advisory_dispatch`
            # fires on every PostToolUse Write|Edit|MultiEdit|NotebookEdit|Agent and
            # recorded ZERO rows in 85 hours, so it was absent from every ranking
            # built on this ledger while plausibly being the single largest consumer.
            # A blind instrument cannot support the kill disposition's budget rule
            # (docs/wiki/cost-budgets-and-the-kill-disposition.md: measurement answers
            # "does this fit"), so fall back to the process cwd rather than dropping.
            # Zero-spawn: git_common_dir resolves by pure-Python upward walk and is
            # lru_cached — see its docstring's "hot path may treat this as zero-spawn".
            try:
                repo_root = Path.cwd()
            except OSError:
                return
            key_source = "cwd"

        from coordinator_core.lifecycle import git_common_dir

        try:
            common_dir = git_common_dir(repo_root)
        except (RuntimeError, OSError):
            return

        sink = _sink_path(common_dir)
        entry = dict(entry)
        entry["repo_key"] = str(common_dir)
        entry["repo_key_source"] = key_source

        line = json.dumps(entry, separators=(",", ":")) + "\n"
        encoded = line.encode("utf-8")

        try:
            os.makedirs(sink.parent, exist_ok=True)
        except OSError:
            return

        _append_line(sink, encoded)
    except Exception:
        try:
            _log().debug(
                "coordinator_core.telemetry.op_latency: _write_entry failed for op %r",
                entry.get("op") if isinstance(entry, dict) else None, exc_info=True,
            )
        except Exception:
            pass


def _append_line(sink: Path, encoded: bytes) -> None:
    """Atomically append ``encoded`` (already newline-terminated) to ``sink``.

    Thin re-export of ``coordinator_core.atomic_append.append_line`` — the
    shared atomic-append primitive, promoted out of this module so
    ``coordinator_core.install.resolution_journal`` and
    ``coordinator_core.benchmarks.ambient_sampler`` use the same mechanism
    rather than each carrying its own copy (see that module's docstring for
    the Windows negative-spec this fixes). Kept as a module-level name here,
    not inlined at the ``record_op_latency`` call site, so
    coordinator_core.telemetry.tests.test_op_latency's concurrent-append
    test can keep importing it directly to exercise the write-concurrency
    guarantee without dragging in git/repo resolution.
    """
    atomic_append.append_line(sink, encoded)


def record_op_latency(
    *,
    op: str,
    t_start: float,
    elapsed_ms: float,
    outcome: str,
    repo_root: Optional[Path] = None,
    sid: Optional[str] = None,
    corr_id: Optional[str] = None,
    caller: Optional[str] = None,
) -> None:
    """Append one JSON line recording a single op invocation's wall-clock cost.

    Record shape:
        {"op": str, "t_start": float epoch, "elapsed_ms": float,
         "outcome": "ok"|"error"|"timeout", "pid": int, "sid": str|null,
         "repo_key": str|null, "repo_key_source": "envelope"|"cwd",
         "kind": "complete", "corr_id": str|null, "caller": str|null}

    ``outcome`` "timeout" means the CALLING side gave up waiting — it does NOT
    mean the op handler stopped running (see module docstring negative-spec).

    ``corr_id``, when provided, is the same id passed to the paired
    ``record_op_started`` call for this invocation — see that function and
    the module docstring's "Vanished vs timed-out" note for why the two rows
    need to be joinable. Optional and defaults to None so existing callers
    that predate the started-row instrument are unaffected; this is the ONLY
    signature change made to this function, and it is purely additive.

    ``caller``, when provided, is the invoking module/entry point (see
    `caller_module`) -- optional and additive in the same way as ``corr_id``,
    defaulting to ``None`` so pre-C1 callers are unaffected.

    Resolves the sink path via ``coordinator_core.lifecycle.git_common_dir`` from
    ``repo_root`` (deferred import — see module docstring's "Cheap" requirement).
    If ``repo_root`` is None, or the git common dir cannot be resolved, or the
    write fails for any reason, this function does nothing observable to the
    caller — see the module docstring's "Never breaks dispatch" requirement.

    Never raises.
    """
    entry = {
        "op": op,
        "t_start": t_start,
        "elapsed_ms": elapsed_ms,
        "outcome": outcome,
        "pid": os.getpid(),
        "sid": sid,
        "repo_key": None,
        "kind": "complete",
        "corr_id": corr_id,
        "caller": caller,
    }
    _write_entry(entry, repo_root)


def record_op_started(
    *,
    op: str,
    t_start: float,
    corr_id: str,
    repo_root: Optional[Path] = None,
    sid: Optional[str] = None,
    caller: Optional[str] = None,
) -> None:
    """Append one JSON line marking an op invocation's START, before it runs.

    Record shape:
        {"op": str, "t_start": float epoch, "pid": int, "sid": str|null,
         "repo_key": str|null, "repo_key_source": "envelope"|"cwd",
         "kind": "started", "corr_id": str, "caller": str|null}

    ``caller``, when provided, is the invoking module/entry point that
    called through to `coordinator_core.ipc.dispatch_message` for this
    invocation (see `caller_module`) -- optional and additive, defaulting to
    ``None`` so pre-C1 callers are unaffected.

    Written at the ``coordinator_core.ipc.dispatch_message`` wrapper's entry,
    BEFORE ``_dispatch_message_impl`` is awaited — so it is already durable
    on disk if the process is killed (e.g. by a caller-side
    ``subprocess.run(timeout=)`` in cc_invoke) before the handler returns and
    the completion row's ``finally`` block ever runs. ``corr_id`` must be the
    same id later passed to ``record_op_latency`` for this same invocation
    (see ``new_correlation_id``) so the two rows are joinable without
    inferring identity from ``(pid, op, t_start)`` proximity.

    An unpaired "started" row (no "complete" row sharing its ``corr_id``)
    means the invocation VANISHED — see module docstring's "Vanished vs
    timed-out" note for why that is a distinct, previously-invisible
    reconcile hazard from an ``outcome: "timeout"`` completion row.

    Same fail-open contract as ``record_op_latency``: resolves the sink via
    ``coordinator_core.lifecycle.git_common_dir``, honours
    ``COORDINATOR_OP_LATENCY_DISABLE``, and never raises — a telemetry defect
    must never fail a peer's op, and this now sits on the dispatch ENTRY
    path, where a raise would be worse than on the exit path.
    """
    entry = {
        "op": op,
        "t_start": t_start,
        "pid": os.getpid(),
        "sid": sid,
        "repo_key": None,
        "kind": "started",
        "corr_id": corr_id,
        "caller": caller,
    }
    _write_entry(entry, repo_root)


def record_composition_span(
    *,
    composition_id: str,
    name: str,
    invocation_count: int,
    elapsed_secs: float,
    outcome: str,
    t_start: float,
    repo_root: Optional[Path] = None,
    sid: Optional[str] = None,
) -> None:
    """Append one JSON line recording a whole COMPOSITION's span -- one row per
    composition, written at flush time (not per directive/invocation).

    Record shape:
        {"composition_id": str, "name": str, "invocation_count": int,
         "elapsed_secs": float, "outcome": str, "t_start": float epoch,
         "pid": int, "sid": str|null, "repo_key": str|null,
         "kind": "composition"}

    Deliberately NOT ``record_op_latency``: that function hardcodes
    ``kind: "complete"`` plus an ``op`` field, and this module's own
    backward-reading rule treats an absent ``kind`` as ``"complete"`` too --
    routing a composition row through it would enter the completed-OP
    population ``pairing_summary``/``cost_census`` read, adding a spurious
    35th "op" name to that population. ``kind: "composition"`` is a new,
    explicit row kind neither reader recognises: both ``pairing_summary``
    (this module) and ``coordinator_core.telemetry.cost_census`` dispatch on
    ``kind`` and already ignore any row matching neither ``"started"`` nor
    ``"complete"`` (cost_census: ``if (entry.get("kind") or "complete") !=
    "complete": continue``), so adding this row kind is safe without
    touching either reader.

    ``name`` is the composition's identity (which assembler/ceremony --
    the field a plain per-op row lacks, and the reason grouping by op alone
    cannot reconstruct which composition an invocation belonged to).
    ``t_start`` is the composition's wall-clock start as a float epoch, same
    field name and semantics as ``record_op_latency``'s. It is what makes a
    row's calendar day derivable: ``CompositionBudget`` measures span with
    ``time.monotonic``, which carries no date, and the ceiling-derivation
    population is partitioned across calendar days (docs/plans/
    2026-08-18-arm-the-composition-budget.md § C4). Without it a reader can
    only date rows by sink-file mtime, which rotation destroys.

    ``outcome`` is exactly one of ``"success"``, ``"partial_mutation"``, or
    ``"directive_failed"`` -- see
    ``coordinator_core.telemetry.composition_record.flush_composition_record``,
    this function's sole caller.

    Same fail-open contract as ``record_op_latency``/``record_op_started``:
    resolves the sink via ``coordinator_core.lifecycle.git_common_dir``,
    honours ``COORDINATOR_OP_LATENCY_DISABLE``, and never raises.
    """
    entry = {
        "composition_id": composition_id,
        "name": name,
        "invocation_count": invocation_count,
        "elapsed_secs": elapsed_secs,
        "outcome": outcome,
        "t_start": t_start,
        "pid": os.getpid(),
        "sid": sid,
        "repo_key": None,
        "kind": "composition",
    }
    _write_entry(entry, repo_root)


# The longest a client-side subprocess.run(timeout=) waits before killing an
# invocation, for any op that has not overridden its budget — see
# coordinator/bin/lib/cc_invoke.py::_op_timeout_ceiling:
#   max(FLOOR=10, engine_budget(op)=DISPATCH_TIMEOUT_SECS default 30 + MARGIN=10) == 40
# A "started" row younger than this is unfinished, not vanished. Kept as a
# named module constant rather than inlined so a future cc_invoke change to
# the FLOOR/MARGIN/DISPATCH_TIMEOUT_SECS numbers has one place to update.
DEFAULT_STALENESS_CUTOFF_SECS: float = 40.0


def pairing_summary(
    *,
    repo_root: Optional[Path] = None,
    sink_path: Optional[Path] = None,
    staleness_cutoff_secs: float = DEFAULT_STALENESS_CUTOFF_SECS,
    now: Optional[float] = None,
) -> dict:
    """Read the real sink and return the started/complete pairing summary (AC3).

    Either ``sink_path`` (used directly, exactly that one file — never
    generation-spanning, callers and tests depend on single-file reads) or
    ``repo_root`` (resolved via ``sink_generations``, reading the live sink
    PLUS every rotated generation on disk — see that function and C1/C3, plan
    ``2026-08-19-warm-engine-gets-an-honest-instrument``) must be given;
    ``sink_path`` wins if both are given. Returns a plain dict rather than a
    registered op — see C2 task body: op registration drags in a
    classification/authz surface out of proportion to a reader.

    Return shape:
        {"total": int, "paired": int, "unpaired_started": int,
         "unpaired_rate": float, "in_flight": int,
         "malformed_lines_skipped": int}

    ``total`` counts distinct "started" rows (rows with no ``corr_id`` cannot
    participate in pairing and are excluded — only rows written by the C1
    instrument carry one). ``paired`` is the count of those whose ``corr_id``
    also appears on a "complete" row. Of the remainder, a "started" row is
    ``in_flight`` (excluded from ``unpaired_started``) if
    ``now - t_start < staleness_cutoff_secs``, else it counts toward
    ``unpaired_started`` — a genuinely vanished invocation (module docstring's
    "Vanished vs timed-out" note). ``unpaired_rate = unpaired_started / total``
    (0.0 if ``total`` is 0).

    Per C1's documented backward-reading rule, a row with no ``"kind"`` field
    at all (written before this field existed) is treated as ``"complete"``,
    never ``"started"`` — it therefore cannot introduce a phantom unpaired
    row, only (harmlessly) a phantom pairing partner nothing needs.

    This reads a JSONL sink several live processes are actively appending to.
    A torn or unparseable final line (or any line) is skipped and counted in
    ``malformed_lines_skipped`` rather than raising — never raises.
    """
    if sink_path is not None:
        sink_paths = [sink_path]
    else:
        if repo_root is None:
            return {
                "total": 0, "paired": 0, "unpaired_started": 0,
                "unpaired_rate": 0.0, "in_flight": 0,
                "malformed_lines_skipped": 0,
            }
        sink_paths = sink_generations(repo_root)
        if not sink_paths:
            return {
                "total": 0, "paired": 0, "unpaired_started": 0,
                "unpaired_rate": 0.0, "in_flight": 0,
                "malformed_lines_skipped": 0,
            }

    if now is None:
        now = time.time()

    started_t_start: dict = {}
    completed_corr_ids: set = set()
    malformed = 0

    for path in sink_paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        malformed += 1
                        continue
                    if not isinstance(entry, dict):
                        malformed += 1
                        continue

                    kind = entry.get("kind") or "complete"
                    corr_id = entry.get("corr_id")

                    if kind == "started":
                        if corr_id is not None:
                            started_t_start[corr_id] = entry.get("t_start")
                    elif kind == "complete":
                        if corr_id is not None:
                            completed_corr_ids.add(corr_id)
        except FileNotFoundError:
            pass
        except OSError:
            pass

    total = len(started_t_start)
    paired = 0
    in_flight = 0
    unpaired_started = 0

    for corr_id, t_start in started_t_start.items():
        if corr_id in completed_corr_ids:
            paired += 1
            continue

        age = None
        if isinstance(t_start, (int, float)):
            age = now - t_start

        if age is not None and age < staleness_cutoff_secs:
            in_flight += 1
        else:
            unpaired_started += 1

    unpaired_rate = (unpaired_started / total) if total else 0.0

    return {
        "total": total,
        "paired": paired,
        "unpaired_started": unpaired_started,
        "unpaired_rate": unpaired_rate,
        "in_flight": in_flight,
        "malformed_lines_skipped": malformed,
    }


# --- budget-breach view ----------------------------------------------------
#
# Why here and not beside a reader: the two failure kinds a breach view must
# never merge are stated in THIS module's own negative-spec ("Vanished vs
# timed-out"), and the pairing rule that separates them is
# `pairing_summary`'s directly above. A per-op breach aggregation built in a
# reader module would restate both, and a restated distinction drifts.
#
# `breach_summary` is PURE over already-parsed rows -- no file IO, no sink
# resolution, no new module-level import, so it costs the hot-path writers in
# this module nothing (module docstring's "Cheap" requirement).
# `coordinator_core.ops.op_budget_breaches` does the bounded read and hands
# the rows in; `coordinator_core.ops.op_census_report.census` passes the rows
# it has ALREADY read, paying one extra pass rather than a second read.

#: The bar a breach is measured against. Deliberately NOT a per-op caller
#: timeout: a caller timeout is a dial somebody chose, so an op that was
#: given a bigger dial reads as compliant against it, and the ops this view
#: exists to find are exactly the ones that got more grace. DR-344's
#: brightline is PM-ratified and identical for every op. Stated once, in
#: `coordinator_core.op_census.timing.PROCESS_TIME_BAR_MS`; mirrored here as
#: a default only so this module keeps its no-new-imports property -- callers
#: pass `bar_ms` explicitly, and
#: `coordinator_core.telemetry.tests.test_breach_summary` asserts the two
#: numbers still agree.
DEFAULT_BREACH_BAR_MS: float = 500.0

#: Minimum attempts a half-window must hold before a per-op trend is reported
#: at all. Below it the answer is `"insufficient_data"`, never `"flat"` -- a
#: two-sample rate cannot separate a trend from noise, and reading "flat" off
#: it is the same false-pass `op_census.timing`'s three-state rule forbids.
TREND_MIN_ATTEMPTS_PER_HALF: int = 20

#: Relative change in breach rate between the two half-windows below which a
#: trend reads `"flat"`.
TREND_FLAT_BAND: float = 0.10

#: Breach kinds, kept separate everywhere. Never reported as one number
#: without the three alongside it:
#:   over_bar       -- a `complete` row that finished but took at least
#:                     `bar_ms`. It ran to completion and held the box for
#:                     the whole of it.
#:   caller_timeout -- a `complete` row with `outcome: "timeout"`. The CALLER
#:                     gave up; the handler kept running and MAY STILL HAVE
#:                     COMMITTED (module docstring, the "timeout" outcome).
#:   vanished       -- a `started` row with no `complete` row sharing its
#:                     `corr_id`, older than `staleness_cutoff_secs`. Killed
#:                     mid-flight; whether it committed is UNKNOWN from this
#:                     sink alone, and it carries no `elapsed_ms`, so it
#:                     contributes nothing to `stolen_ms` rather than a
#:                     fabricated cost.
BREACH_KINDS = ("over_bar", "caller_timeout", "vanished")

#: Epoch seconds before which a `t_start` cannot be a real invocation time
#: (2020-01-01). Rows below it exist in the live sink — 5 of 46,416 on
#: 2026-08-21, sitting at epoch ~1 — so some writer reaches `_write_entry`
#: with a monotonic or zeroed clock reading instead of a wall-clock epoch.
#: `breach_summary` counts them (`window.implausible_t_start_rows`) rather
#: than dropping them silently: they are a defect in a writer, and the
#: instrument that notices must say so. It does not correct them — the
#: correction belongs at the writer, and guessing here would hide it.
PLAUSIBLE_T_START_FLOOR: float = 1_577_836_800.0


def _percentile_idx(sorted_vals: list, fraction: float):
    """Index-based percentile over a pre-sorted list, no interpolation.

    Same rule as `coordinator_core.telemetry.engine_report._percentile`,
    which this module cannot import -- `engine_report` imports THIS module,
    and the cycle would fire at hot-path import time. `fraction` is a
    fraction (0.95), never a percentage.
    """
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = min(len(sorted_vals) - 1, int(round(fraction * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def _trend(early_attempts, early_breaches, late_attempts, late_breaches) -> str:
    """Direction of an op's breach rate across the two half-windows.

    Returns `"insufficient_data"` unless BOTH halves hold at least
    `TREND_MIN_ATTEMPTS_PER_HALF` attempts -- an op seen twice has no trend,
    and reporting one for it makes the ranking act on noise.
    """
    if early_attempts < TREND_MIN_ATTEMPTS_PER_HALF or late_attempts < TREND_MIN_ATTEMPTS_PER_HALF:
        return "insufficient_data"
    early_rate = early_breaches / early_attempts
    late_rate = late_breaches / late_attempts
    if early_rate == 0.0:
        return "worsening" if late_rate > 0.0 else "flat"
    delta = (late_rate - early_rate) / early_rate
    if delta > TREND_FLAT_BAND:
        return "worsening"
    if delta < -TREND_FLAT_BAND:
        return "improving"
    return "flat"


def breach_summary(
    entries,
    *,
    bar_ms: float = DEFAULT_BREACH_BAR_MS,
    staleness_cutoff_secs: float = DEFAULT_STALENESS_CUTOFF_SECS,
    now: Optional[float] = None,
    top_n: Optional[int] = None,
) -> dict:
    """Per-op budget-breach view over already-parsed op-latency rows.

    Answers, for every op that breached: how often, how badly, when first and
    last seen, and which way it is trending -- ranked so the ops doing the
    most damage to the shared box sort first.

    Ranking is `stolen_ms` DESCENDING, never raw breach count. `stolen_ms` is
    the summed process time an op took PAST the bar
    (``sum(elapsed_ms - bar_ms)`` over its `over_bar` and `caller_timeout`
    rows) -- frequency times cost by construction. One 30s breach outranks
    fifty 520ms ones because that is what the ~50 sessions queued behind it
    actually paid; a raw count inverts that. Ties break on total breach count,
    then op name, so the order is deterministic across runs.

    The three breach kinds (`BREACH_KINDS`) are counted separately and never
    merged away: `breaches` is their sum and is reported ALONGSIDE the three,
    not instead of them. A `vanished` row has no `elapsed_ms` and adds nothing
    to `stolen_ms` or to any percentile.

    `bar_ms` is the brightline, not a per-op caller timeout -- see
    `DEFAULT_BREACH_BAR_MS` for why raising a dial must not clear an op.

    Return shape:
        {"bar_ms": float,
         "window": {"first_seen": float|None, "last_seen": float|None,
                    "trend_split_t_start": float|None,
                    "implausible_t_start_rows": int},
         "totals": {"attempts": int, "complete_rows": int, "over_bar": int,
                    "caller_timeout": int, "vanished": int, "in_flight": int,
                    "breaching_ops": int, "stolen_ms": float},
         "ops": [{"op": str, "attempts": int, "invocations": int,
                  "over_bar": int, "caller_timeout": int, "vanished": int,
                  "breaches": int, "breach_rate": float, "stolen_ms": float,
                  "p50_ms": float|None, "p95_ms": float|None,
                  "max_ms": float|None, "first_seen": float|None,
                  "last_seen": float|None, "trend": str}, ...]}

    `ops` lists ONLY ops with at least one breach -- a clean op has nothing to
    surface and padding the list with it buries the ones that do. `top_n`, if
    given, truncates the ranked list; `totals` always counts the whole
    population, truncated or not, and `totals.breaching_ops` is the untruncated
    count so a truncated list can never read as the whole population.

    ONE pass over `entries`. The trend split is not known until every row has
    been seen, so each op carries a compact `(t_start, breached)` timeline and
    is bucketed after the split is computed -- a second pass over the raw rows
    costs more, on a path held to DR-344's 200ms per-process bar. `entries` is
    materialised once; pass a BOUNDED iterable
    (`coordinator_core.ops.op_budget_breaches` holds the read bound).

    Never raises on a malformed row: a row that is not a dict, or that carries
    a non-numeric `elapsed_ms`/`t_start`, is skipped.
    """
    if now is None:
        now = time.time()

    rows = [e for e in entries if isinstance(e, dict)]

    per_op: dict = {}
    started: dict = {}
    completed_corr_ids: set = set()
    complete_t_starts: list = []
    implausible_t_start = 0
    window_first = None
    window_last = None

    def _op_bucket(op_name: str) -> dict:
        # Membership test before construction, never `setdefault(op, {...})`:
        # the dict literal is built on every call there, hit or miss, which
        # over a 47k-row sink is ~47k throwaway 11-key dicts on a path held
        # to DR-344's 200ms per-process bar.
        bucket = per_op.get(op_name)
        if bucket is None:
            bucket = {
                "op": op_name,
                "invocations": 0,
                "over_bar": 0,
                "caller_timeout": 0,
                "vanished": 0,
                "stolen_ms": 0.0,
                "elapsed": [],
                # (t_start, breached) per complete row, collected here so the
                # trend split does not need a second full pass over `rows` --
                # the median that defines the split is not known until every
                # row has been seen, but re-reading the raw dicts to find it
                # costs more than carrying two numbers per row.
                "timeline": [],
                "first_seen": None,
                "last_seen": None,
            }
            per_op[op_name] = bucket
        return bucket

    def _note_seen(bucket: dict, t_start) -> None:
        if not isinstance(t_start, (int, float)):
            return
        if bucket["first_seen"] is None or t_start < bucket["first_seen"]:
            bucket["first_seen"] = float(t_start)
        if bucket["last_seen"] is None or t_start > bucket["last_seen"]:
            bucket["last_seen"] = float(t_start)

    for entry in rows:
        kind = entry.get("kind") or "complete"
        if kind not in ("started", "complete"):
            continue
        op_name = entry.get("op")
        if not isinstance(op_name, str):
            continue

        t_start = entry.get("t_start")
        if isinstance(t_start, (int, float)):
            if t_start < PLAUSIBLE_T_START_FLOOR:
                implausible_t_start += 1
            if window_first is None or t_start < window_first:
                window_first = float(t_start)
            if window_last is None or t_start > window_last:
                window_last = float(t_start)

        bucket = _op_bucket(op_name)
        _note_seen(bucket, t_start)
        corr_id = entry.get("corr_id")

        if kind == "started":
            if corr_id is not None:
                started[corr_id] = (op_name, t_start)
            continue

        if corr_id is not None:
            completed_corr_ids.add(corr_id)
        bucket["invocations"] += 1
        if isinstance(t_start, (int, float)):
            complete_t_starts.append(float(t_start))
        elapsed = entry.get("elapsed_ms")
        if isinstance(elapsed, (int, float)):
            bucket["elapsed"].append(float(elapsed))

        # A "timeout" outcome is its OWN kind and is checked first: it is a
        # breach whatever its elapsed_ms says, and classifying it by elapsed
        # would fold it into over_bar and lose the may-still-have-committed
        # distinction this view exists to preserve.
        breached = True
        if entry.get("outcome") == "timeout":
            bucket["caller_timeout"] += 1
        elif isinstance(elapsed, (int, float)) and float(elapsed) >= bar_ms:
            bucket["over_bar"] += 1
        else:
            breached = False

        if breached and isinstance(elapsed, (int, float)):
            bucket["stolen_ms"] += max(0.0, float(elapsed) - bar_ms)
        if isinstance(t_start, (int, float)):
            bucket["timeline"].append((float(t_start), breached))

    in_flight = 0
    for corr_id, (op_name, t_start) in started.items():
        if corr_id in completed_corr_ids:
            continue
        age = (now - t_start) if isinstance(t_start, (int, float)) else None
        if age is not None and age < staleness_cutoff_secs:
            in_flight += 1
            continue
        _op_bucket(op_name)["vanished"] += 1

    # Split at the MEDIAN t_start, never at (first + last) / 2. Measured on
    # the live sink 2026-08-21: five of 46,416 rows carry a near-epoch
    # `t_start` (1970), which drags an arithmetic midpoint to 1998 and leaves
    # 5 rows early against 46,411 late — every op then reports
    # "insufficient_data" and the whole trend axis goes dark on a handful of
    # bad rows. A median is outlier-proof and splits the population evenly by
    # construction, which is also what a rate comparison wants.
    midpoint = None
    if complete_t_starts:
        ordered = sorted(complete_t_starts)
        candidate = ordered[len(ordered) // 2]
        if ordered[0] < candidate < ordered[-1]:
            midpoint = candidate

    ranked = []
    totals_over_bar = totals_timeout = totals_vanished = totals_complete = 0
    totals_stolen = 0.0

    for bucket in per_op.values():
        totals_complete += bucket["invocations"]
        totals_over_bar += bucket["over_bar"]
        totals_timeout += bucket["caller_timeout"]
        totals_vanished += bucket["vanished"]
        totals_stolen += bucket["stolen_ms"]

        breaches = bucket["over_bar"] + bucket["caller_timeout"] + bucket["vanished"]
        if breaches == 0:
            continue

        attempts = bucket["invocations"] + bucket["vanished"]
        elapsed_sorted = sorted(bucket["elapsed"])

        early_attempts = early_breaches = late_attempts = late_breaches = 0
        if midpoint is not None:
            for row_t_start, row_breached in bucket["timeline"]:
                if row_t_start >= midpoint:
                    late_attempts += 1
                    late_breaches += 1 if row_breached else 0
                else:
                    early_attempts += 1
                    early_breaches += 1 if row_breached else 0

        ranked.append(
            {
                "op": bucket["op"],
                "attempts": attempts,
                "invocations": bucket["invocations"],
                "over_bar": bucket["over_bar"],
                "caller_timeout": bucket["caller_timeout"],
                "vanished": bucket["vanished"],
                "breaches": breaches,
                "breach_rate": (breaches / attempts) if attempts else 0.0,
                "stolen_ms": round(bucket["stolen_ms"], 3),
                "p50_ms": _percentile_idx(elapsed_sorted, 0.50),
                "p95_ms": _percentile_idx(elapsed_sorted, 0.95),
                "max_ms": elapsed_sorted[-1] if elapsed_sorted else None,
                "first_seen": bucket["first_seen"],
                "last_seen": bucket["last_seen"],
                "trend": _trend(early_attempts, early_breaches, late_attempts, late_breaches),
            }
        )

    ranked.sort(key=lambda row: (-row["stolen_ms"], -row["breaches"], row["op"]))
    breaching_ops = len(ranked)
    if top_n is not None:
        ranked = ranked[:top_n]

    return {
        "bar_ms": bar_ms,
        "window": {
            "first_seen": window_first,
            "last_seen": window_last,
            "trend_split_t_start": midpoint,
            "implausible_t_start_rows": implausible_t_start,
        },
        "totals": {
            "attempts": totals_complete + totals_vanished,
            "complete_rows": totals_complete,
            "over_bar": totals_over_bar,
            "caller_timeout": totals_timeout,
            "vanished": totals_vanished,
            "in_flight": in_flight,
            "breaching_ops": breaching_ops,
            "stolen_ms": round(totals_stolen, 3),
        },
        "ops": ranked,
    }

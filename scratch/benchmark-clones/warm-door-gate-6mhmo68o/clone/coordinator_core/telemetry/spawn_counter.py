"""
coordinator_core.telemetry.spawn_counter — process-local count of child spawns.

Purpose: the brightline is stated in two axes, "process time and spawn count"
(CLAUDE.md § The brightline), and only the first one was ever written down. The
op-latency sink records `elapsed_ms` and, since C9, `process_ms` — nothing on any
row says how many processes an op created. Every spawn figure quoted in the
2026-08-23 kill sweep was therefore produced by a bespoke external probe, one per
investigation, and none of them are joinable against the op ledger.

This module is the counter those figures should have come from: a bare integer,
incremented at the sanctioned spawn chokepoint, read as a delta around one op's
dispatch. That delta is then a field on the op's own row.

Why a module-level counter and not a context manager or a thread-local:

  - The engine's spawn chokepoint (`coordinator_core.git.run.run_git`) is called
    from deep inside op handlers, many frames below any dispatch site that could
    hold a context. A counter is the only shape that reaches both ends without
    threading a parameter through every intermediate signature.
  - It is deliberately PROCESS-wide, not thread-local. The pool-worker path runs
    `WORKER_POOL_SIZE` threads against one clock and already records its samples
    as `MEASUREMENT_SCOPE_PROCESS_WIDE` for exactly that reason — a thread-local
    counter would produce a per-op spawn figure that looks precise while the
    process-time figure beside it is admittedly not, which is a worse lie than
    two honestly process-wide numbers. Readers must apply the row's own
    `measurement_scope` to the spawn count the same way they apply it to
    `process_ms`.

Negative-spec:

  - Never raises, never allocates, never imports. `bump()` is one integer add on
    the git hot path; anything heavier than that belongs in the reader, not here.
  - Not reset between ops, ever. A reader takes a DELTA around the window it
    cares about; a counter that resets cannot be read concurrently by two
    overlapping measurements without one of them silently zeroing the other's
    baseline.
  - Counts SPAWN ATTEMPTS, not successes. A `git` invocation that fails still
    paid process-creation cost — which is the cost being measured (CLAUDE.md:
    "process creation is the cost, not the query"), so excluding failures would
    understate exactly the case worth finding.
  - Undercounts for TWO independent reasons, and a reader must hold both.

    (a) It sees the chokepoint only. A private `subprocess.run` bypasses it and
    is invisible here. That is a defect in the caller
    (`coordinator_core.git.git_state`'s docstring already rules that every git
    call goes through `run_git`, "never a private `subprocess.run`").

    (b) **Even a complete Python-keyed count is low against job accounting.**
    `claude-klabauter-37`'s caller census of the close ceremony's gate path
    (`state/audits/2026-08-25-close-ceremony-gate-path-caller-census.md`,
    `e99ff0b5f`) keyed on `_winapi.CreateProcess` — strictly wider than this
    counter, catching `os.spawn*` and `multiprocessing` too — and still found
    **8 Python-created processes against 16 counted by the job object** on the
    same path. Each of the 8 git commands measures exactly 1 process run in
    isolation, so the extra 8 are not reproducible git children. The cause is
    unresolved (console host under a console-less parent, a git-internal helper
    under that stdio shape, and a job-accounting artefact are all still live).

    So the honest statement is: a figure from this counter is a FLOOR on
    process count, plausibly by ~2x on a git-heavy path, and it is NOT
    comparable to a job-accounted figure. Time figures are unaffected — a job
    object accounts every process in the job whether or not anything can name
    it — but counts from the two mechanisms must never be quoted side by side
    as if they measured the same thing.

Spec backlink: state/handoffs/2026-08-25_roadmap-the-meter-02.md
               state/audits/2026-08-25-the-meter-corpus-shape-spike.md
"""

from __future__ import annotations

_spawns = 0


def bump(n: int = 1) -> None:
    """Record ``n`` child-process spawn attempts by this process.

    Called at the spawn chokepoint. One integer add — no lock (CPython's
    bytecode for an in-place int add on a module global is not atomic across
    threads, and that is accepted: a lost increment is bounded at 1 PER RACE,
    but on the pool-worker path (`WORKER_POOL_SIZE` threads bumping
    concurrently) races recur across the whole measurement window, so the
    AGGREGATE loss is proportional to contention, not a fixed single-digit
    constant -- while a lock on the git hot path costs every op that spawns).
    """
    global _spawns
    _spawns += n


def spawn_count() -> int:
    """Total spawn attempts by this process since interpreter start.

    Monotonic. Meaningful only as a delta between two readings — the absolute
    value carries no information a caller can use, since it includes every spawn
    by every op this process served before the one being measured.
    """
    return _spawns

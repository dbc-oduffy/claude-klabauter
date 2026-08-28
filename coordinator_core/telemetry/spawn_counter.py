"""
coordinator_core.telemetry.spawn_counter — process-local count of child spawns.

Purpose: the brightline is stated in two axes, "process time and spawn count"
(CLAUDE.md § The brightline), and only the first one was ever written down. The
op-latency sink records `elapsed_ms` and, since C9, `process_ms` — nothing on any
row says how many processes an op created. Every spawn figure quoted in the
2026-08-23 kill sweep was therefore produced by a bespoke external probe, one per
investigation, and none of them are joinable against the op ledger.

This module is the counter those figures should have come from: a bare integer,
incremented at the interpreter's own spawn seam, read as a delta around one op's
dispatch. That delta is then a field on the op's own row.

WHERE THE COUNT COMES FROM

`sys.addaudithook` (PEP 578), filtered to the two events CPython raises exactly
once per child process it creates from Python: `subprocess.Popen` and
`os.system`. Every `subprocess.run` / `.call` / `.check_call` / `.check_output`
/ `Popen(...)` in the engine constructs a `Popen`, so ONE hook sees all of them
without a call-site migration.

That seam replaces an earlier one. The counter used to be bumped by hand at
`coordinator_core.git.run.run_git` and nowhere else, which made it a GIT-spawn
count that `ipc.py` and `warm/server.py` then attached to an op's row as that
op's spawn figure — 934 direct `subprocess.*` call sites across 346 non-test
modules were invisible to it, and `ceremony.scoped_git_commit` reported 0 against
a documented 31 processes. The hand bump survives only as the fallback below.

Why a module-level counter and not a context manager or a thread-local:

  - An audit hook is a process-global callback with no place to hold a caller's
    context, and the spawn sites it fires for sit deep inside op handlers, many
    frames below any dispatch site that could hold one. A counter is the only
    shape that reaches both ends without threading a parameter through every
    intermediate signature.
  - It is deliberately PROCESS-wide, not thread-local. The pool-worker path runs
    `WORKER_POOL_SIZE` threads against one clock and already records its samples
    as `MEASUREMENT_SCOPE_PROCESS_WIDE` for exactly that reason — a thread-local
    counter would produce a per-op spawn figure that looks precise while the
    process-time figure beside it is admittedly not, which is a worse lie than
    two honestly process-wide numbers. Readers must apply the row's own
    `measurement_scope` to the spawn count the same way they apply it to
    `process_ms`.

Negative-spec:

  - The hook is installed at import and NEVER raises past its own `try`. A
    broken counter must not break the op it is instrumenting.
  - Hook cost is not on the spawn path only — a CPython audit hook is called for
    every audited event the interpreter raises, `open` included. Measured on the
    normal-tier box (`X:` 285K, 100k iterations, 3 pairs): 26.5µs per `open`
    with the hook against 26.5µs without, the difference inside run-to-run
    noise. Against a 500ms brightline that is unmeasurable, and it is why the
    filter below is an identity compare against a frozenset and nothing else —
    anything heavier than that belongs in the reader, not here.
  - `os.exec*` / `os.posix_spawn` are deliberately NOT counted. On POSIX
    `subprocess` reaches the child through `os.posix_spawn`, so counting both
    would double every subprocess spawn on one platform and not the other,
    producing a figure that is not comparable across the fleet. The only bare
    `os.exec` in the engine is `coordinator_core.bare_forwarder`, which REPLACES
    this process rather than adding one — nothing survives to read the counter.
  - `multiprocessing` / `ProcessPoolExecutor` workers are likewise uncounted:
    `warm.server`'s pool is spawned once at pool init as infrastructure, not as
    a cost any single op incurred, and charging it to whichever op happened to
    be in flight would be a worse reading than omitting it.
  - Not reset between ops, ever. A reader takes a DELTA around the window it
    cares about; a counter that resets cannot be read concurrently by two
    overlapping measurements without one of them silently zeroing the other's
    baseline.
  - Counts SPAWN ATTEMPTS, not successes. A child that fails to start, or is
    killed on timeout, still paid process-creation cost — which is the cost
    being measured (CLAUDE.md: "process creation is the cost, not the query"),
    so excluding failures would understate exactly the case worth finding. The
    audit event fires before the child exists, which is what makes this true by
    construction rather than by remembering to bump on the error path.
  - Undercounts for TWO independent reasons, and a reader must hold both.

    (a) It sees Python-created processes only. A child created by a NON-Python
    parent in the chain — notably `cmd.exe` in a Windows `.cmd` launcher — never
    raises a Python audit event and is invisible here, as is anything a C
    extension spawns below the audited Python APIs.

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
               state/audits/2026-08-27-kill-ledger-cost-lines-without-a-measurement-scope.md
               § "The second axis"
"""

from __future__ import annotations

import sys

_spawns = 0

#: CPython raises exactly one of these per child process this interpreter
#: creates from Python. See the negative-spec above for what is excluded and
#: why — the omissions are load-bearing, not an oversight to be widened.
_COUNTED_EVENTS = frozenset({"subprocess.Popen", "os.system"})

_hook_installed = False


def bump(n: int = 1) -> None:
    """Record ``n`` child-process spawn attempts by this process.

    One integer add — no lock (CPython's bytecode for an in-place int add on a
    module global is not atomic across threads, and that is accepted: a lost
    increment is bounded at 1 PER RACE, but on the pool-worker path
    (`WORKER_POOL_SIZE` threads bumping concurrently) races recur across the
    whole measurement window, so the AGGREGATE loss is proportional to
    contention, not a fixed single-digit constant -- while a lock on the audit
    hook would cost every audited event in the process, `open` included).

    Public because `coordinator_core.git.run.run_git` still calls it when
    `audit_hook_installed()` is false, so a machine that refuses the hook keeps
    the git-spawn count it had before this seam existed rather than dropping to
    silence. Nothing else should call it: a second hand-bumped site is
    double-counted the moment the hook is up.
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


def audit_hook_installed() -> bool:
    """Whether the audit hook is counting, i.e. whether `bump()` is redundant.

    False means this interpreter refused `sys.addaudithook` (an already-resident
    hook may veto additions) and the counter has fallen back to the pre-existing
    hand-bumped git seam — which is a GIT-spawn count, not a spawn count. A
    reader that cares about the difference must consult this, not the delta.
    """
    return _hook_installed


def _count_spawn_event(event: str, _args: tuple) -> None:
    """`sys.addaudithook` callback. Hot for EVERY audited event in the process.

    Deliberately branch-then-return with no `try`: a frozenset membership test
    on a str cannot raise, and an in-place int add on a global cannot raise, so
    there is nothing here for a `try` to catch — while the `try` itself would be
    paid on every `open` in the process. `_args` is unread; the counter needs the
    fact of the spawn, never the argv.
    """
    global _spawns
    if event in _COUNTED_EVENTS:
        _spawns += 1


def _install() -> None:
    """Install the counting hook once. Idempotent, and never raises.

    Import-time, not first-read: a spawn that happens before the first reader
    calls `spawn_count()` is exactly the spawn a lazy install would miss, and
    the op-latency reader takes its baseline AFTER the process is already warm.
    Audit hooks cannot be removed once added (CPython, deliberate) — which is
    why this is guarded rather than merely called.
    """
    global _hook_installed
    if _hook_installed:
        return
    try:
        sys.addaudithook(_count_spawn_event)
    except Exception:
        return
    _hook_installed = True


_install()

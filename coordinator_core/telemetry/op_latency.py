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
import time
from pathlib import Path
from typing import Optional

from coordinator_core import atomic_append

_DISABLE_ENV = "COORDINATOR_OP_LATENCY_DISABLE"

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

        if repo_root is None:
            return

        from coordinator_core.lifecycle import git_common_dir

        try:
            common_dir = git_common_dir(repo_root)
        except (RuntimeError, OSError):
            return

        sink = _sink_path(common_dir)
        entry = dict(entry)
        entry["repo_key"] = str(common_dir)

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
) -> None:
    """Append one JSON line recording a single op invocation's wall-clock cost.

    Record shape:
        {"op": str, "t_start": float epoch, "elapsed_ms": float,
         "outcome": "ok"|"error"|"timeout", "pid": int, "sid": str|null,
         "repo_key": str|null, "kind": "complete", "corr_id": str|null}

    ``outcome`` "timeout" means the CALLING side gave up waiting — it does NOT
    mean the op handler stopped running (see module docstring negative-spec).

    ``corr_id``, when provided, is the same id passed to the paired
    ``record_op_started`` call for this invocation — see that function and
    the module docstring's "Vanished vs timed-out" note for why the two rows
    need to be joinable. Optional and defaults to None so existing callers
    that predate the started-row instrument are unaffected; this is the ONLY
    signature change made to this function, and it is purely additive.

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
    }
    _write_entry(entry, repo_root)


def record_op_started(
    *,
    op: str,
    t_start: float,
    corr_id: str,
    repo_root: Optional[Path] = None,
    sid: Optional[str] = None,
) -> None:
    """Append one JSON line marking an op invocation's START, before it runs.

    Record shape:
        {"op": str, "t_start": float epoch, "pid": int, "sid": str|null,
         "repo_key": str|null, "kind": "started", "corr_id": str}

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

    Either ``sink_path`` (used directly) or ``repo_root`` (resolved to the
    sink the same way ``record_op_latency``/``record_op_started`` do) must be
    given; ``sink_path`` wins if both are given. Returns a plain dict rather
    than a registered op — see C2 task body: op registration drags in a
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
    if sink_path is None:
        if repo_root is None:
            return {
                "total": 0, "paired": 0, "unpaired_started": 0,
                "unpaired_rate": 0.0, "in_flight": 0,
                "malformed_lines_skipped": 0,
            }
        from coordinator_core.lifecycle import git_common_dir

        try:
            common_dir = git_common_dir(repo_root)
        except (RuntimeError, OSError):
            return {
                "total": 0, "paired": 0, "unpaired_started": 0,
                "unpaired_rate": 0.0, "in_flight": 0,
                "malformed_lines_skipped": 0,
            }
        sink_path = _sink_path(common_dir)

    if now is None:
        now = time.time()

    started_t_start: dict = {}
    completed_corr_ids: set = set()
    malformed = 0

    try:
        with open(sink_path, "r", encoding="utf-8", errors="replace") as fh:
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

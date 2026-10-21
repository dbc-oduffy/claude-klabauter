"""
coordinator_core.ops.ceremony.close_background -- JSON-RPC "close.background": pays the run
close legs that cannot fit inside ``dispatch.terminal_commit`` and records each outcome in the
run's close-job record.

Purpose: ``terminal_commit`` writes the close-job record (``run_close.write_close_job``), launches
this module by ``detached_spawn.spawn_detached`` (``python close_background.py ...``) and returns.
This job pays ``baton-drain``, ``orientation-refresh``, ``memo-lifecycle`` and ``post-close`` in that
order and rewrites the record through ``run_close.record_leg_outcome``; the reader
(``run_close.read_close_job``) turns a leg this job never recorded into ``failed``.

Negative-spec:
  - Runs only the legs the record still lists as ``queued``, and none once its deadline passed.
  - One leg's exception is recorded ``failed`` and the next leg still runs.
  - A leg is not started once the job's cumulative process time reaches ``JOB_BUDGET_MS``; it is
    recorded ``failed`` with that reason, never dropped.
  - ``sweep-terminal-sizings`` (638ms cold) is over the job's budget
    (docs/research/2026-10-13-run-close-ws1-headroom-spike.md) and is left out of ``baton-drain``.
  - ``post-close`` runs an index launcher only when ``post_close_admission.admit`` admits it (peak +
    reserve <= kernel-available); a deferral keeps the leg ``ran`` and is named in the detail, and
    each admitted launch posts one Group EM START line. It then runs ``baton-chain-closure``'s ``chain_verdicts``
    for the claimed batons; it is ``failed`` only when a launcher failed or that call raised.
  - The handoff sweep is scoped to the run's claimed batons (``plan_sweep(candidate_ids=...)``).
  - No leg detail carries an absolute or backslashed path; exception text is scrubbed first.
  - Imports are lazy inside each leg; the op module itself stays cheap to load at server start.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from coordinator_core.ipc import register_op
from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.ops.dispatch_emit.run_close import LegResult

GENERATES = []

JOB_BUDGET_MS = 400.0
"""Cumulative process time (this process, not its git children) after which no leg starts."""

BACKGROUND_LEGS: tuple[str, ...] = (
    "baton-drain",
    "orientation-refresh",
    "memo-lifecycle",
    "post-close",
)

_HANDOFF_SWEEP_CAP = 150
_SIZINGS_SWEEP_NOTE = "sweep-terminal-sizings not run: 638ms cold, over the 500ms bar"
_CEREMONY_NAME = "run-close"
_ENSURE_WAIT_SECS = 5.0

# close_review imports this name; the scrubber itself lives in run_close.
_scrub = run_close.scrub_detail


@dataclass(frozen=True)
class JobContext:
    """What a leg needs: the main worktree root, the git common dir and the op params."""

    root: Path
    common_dir: Path
    run_id: str
    claimed_batons: tuple[str, ...]
    source_memo: Optional[str]
    terminal_sha: Optional[str] = None


Runner = Callable[[JobContext], LegResult]


def _failed(prefix: str, exc: BaseException) -> LegResult:
    return LegResult("failed", run_close.scrub_detail(f"{prefix}{type(exc).__name__}: {exc}"))


def _bin_script(name: str) -> Any:
    """Load ``coordinator/bin/<name>.py`` as a module, with that bin dir's ``lib`` bound."""
    import importlib.machinery
    import importlib.util

    from coordinator_core.bin_lib_binding import exec_module_bin_bound

    bin_dir = Path(__file__).resolve().parents[3] / "coordinator" / "bin"
    module_name = "_close_background_" + name.replace("-", "_")
    loader = importlib.machinery.SourceFileLoader(module_name, str(bin_dir / f"{name}.py"))
    spec = importlib.util.spec_from_file_location(module_name, bin_dir / f"{name}.py", loader=loader)
    if spec is None:
        raise ImportError(f"cannot load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        exec_module_bin_bound(loader, module, str(bin_dir))
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _baton_drain(ctx: JobContext) -> LegResult:
    """Archive the run's claimed batons that have reached a terminal state."""
    if not ctx.claimed_batons:
        return LegResult("not-applicable", "run claims no baton")
    from coordinator_core.ops.fleet._common import archive_and_commit
    from coordinator_core.ops.fleet.archive_terminal_handoffs import plan_sweep

    ids = [Path(b).as_posix() for b in ctx.claimed_batons]
    cascade_note, cascade_failed = _fire_cascade(ctx, ids)
    moves, skipped = plan_sweep(ctx.root, ctx.common_dir, _HANDOFF_SWEEP_CAP, candidate_ids=ids)
    archived = 0
    if moves:
        acted, failed = asyncio.run(
            archive_and_commit(
                worktree_root=ctx.root,
                moves=moves,
                subject=f"fleet: archive {len(moves)} terminal handoff(s)\n\nclose.background for run {ctx.run_id}.",
            )
        )
        archived = len(acted)
        if failed:
            return LegResult(
                "failed",
                run_close.scrub_detail(
                    f"{len(failed)} of {len(moves)} move(s) failed; {cascade_note}; {_SIZINGS_SWEEP_NOTE}"
                ),
            )
    return LegResult(
        "failed" if cascade_failed else "ran",
        run_close.scrub_detail(
            f"archived {archived} of {len(ids)} claimed baton(s), "
            f"{len(skipped)} retained; {cascade_note}; {_SIZINGS_SWEEP_NOTE}"
        ),
    )


def _fire_cascade(ctx: JobContext, ids: Sequence[str]) -> tuple[str, bool]:
    """Fire deliverable.cascade_terminal for the run's terminal batons; returns (detail clause, failed).

    A raise or a commit_error marks the clause failed; the caller still archives."""
    try:
        from coordinator_core.ops.dispatch_emit.run_baton_cascade import fire_for_batons

        reply = fire_for_batons(ctx.root, ctx.common_dir, ids, ctx.terminal_sha)
    except Exception as exc:  # a cascade failure never strands the archive step
        return f"cascade: failed {type(exc).__name__}", True
    fired = reply.get("fired") or []
    advanced = sum(len(f.get("advanced") or []) for f in fired)
    refused = sum(len(f.get("refused") or []) for f in fired)
    errored = any(f.get("commit_error") for f in fired)
    note = f"cascade: {len(fired)} fired, {advanced} advanced, {refused} refused, {len(reply.get('skipped') or [])} skipped"
    return (f"{note}, commit error" if errored else note), errored


def _orientation_refresh(ctx: JobContext) -> LegResult:
    """Pinboard-only orientation refresh, keeping the pinboard the cache already carries."""
    from coordinator_core.orientation import regenerate_cache as rc

    cache_file = rc.resolve_cache_file(ctx.root)
    rc.patch_pinboard_only(cache_file, rc.read_existing_pinboard(cache_file), repo_root=ctx.root)
    return LegResult("ran", "pinboard-only refresh")


def _memo_lifecycle(ctx: JobContext) -> LegResult:
    """Resolve the plan's own source memo from the inbox; already archived is discharged."""
    if not ctx.source_memo:
        return LegResult("not-applicable", "plan names no source memo")
    from coordinator_core.memo_corpus import memo_corpus_root

    name = Path(ctx.source_memo).name
    corpus = Path(memo_corpus_root(str(ctx.root)))
    inbox = corpus / "inbox" / name
    if not inbox.is_file():
        if (corpus / "archive" / name).is_file():
            return LegResult("discharged", "source memo already archived")
        return LegResult("failed", run_close.scrub_detail(f"source memo {name} is in neither inbox nor archive"))

    from coordinator_core import archive_stamp

    if ctx.terminal_sha:
        args = (
            "--actioned-note", f"resolved by run {ctx.run_id} at {ctx.terminal_sha}",
            "--realized-by", ctx.terminal_sha,
        )
    else:
        args = ("--actioned-note", f"resolved by run {ctx.run_id}")
    result = archive_stamp.cs_resolve_memo(str(inbox), *args, return_result=True)
    if int(result.get("exit_code", 1)) != 0:
        return LegResult("failed", run_close.scrub_detail(f"resolve refused: {result.get('error', 'exit non-zero')}"))
    return LegResult("ran", run_close.scrub_detail(f"source memo {name} resolved"))


def _launch_close_review(ctx: JobContext) -> bool:
    """Detach ``close_review.py`` for this run; False when the spawn failed."""
    from coordinator_core.ops.ceremony.detached_spawn import spawn_detached

    engine = Path(__file__).resolve().parents[3]
    params = json.dumps({"claimed_batons": list(ctx.claimed_batons)})
    return spawn_detached(
        str(engine), str(Path(__file__).with_name("close_review.py")),
        [ctx.run_id, "--root", str(ctx.root), "--params", params],
    )


def _post_close(ctx: JobContext) -> LegResult:
    """Fire the structural-index and scip launchers and the detached ``close.review`` job, then
    run ``baton-chain-closure`` for the claimed batons and record its outcome."""
    from coordinator_core.ops.ceremony import post_close_admission as admission

    launched: list[str] = []
    deferred: list[str] = []
    problems: list[str] = []
    avail_mb, trusted = admission.read_available()
    admitted_mb = 0
    for label, call in (
        ("structural-index", lambda: _bin_script("structural-index-refresh").refresh(ctx.root, _ENSURE_WAIT_SECS)),
        ("scip-rebuild", lambda: _bin_script("scip-rebuild-at-ceremony").run(_CEREMONY_NAME, ctx.root)),
    ):
        verdict = admission.admit(label, avail_mb, trusted, admitted_mb)
        if not verdict.admitted:
            deferred.append(f"{label} ({verdict.reason})")
            continue
        try:
            call()
            launched.append(label)
            admitted_mb += admission.DECLARED_PEAK_MB[label]
            admission.notice(verdict, ctx.run_id)
        except Exception as exc:  # one launcher's failure never stops the other
            problems.append(f"{label} {type(exc).__name__}")
    try:
        if _queued_legs(ctx.root, ctx.run_id, ("review",)):
            if _launch_close_review(ctx):
                launched.append("close.review")
            else:
                problems.append("close.review not started")
    except Exception as exc:  # the review leg then reads failed at the deadline
        problems.append(f"close.review {type(exc).__name__}")
    chain_note = _chain_closure_note(ctx, problems)
    reap_note = _reap_note(ctx, problems)
    detail = f"launched: {', '.join(launched) or 'none'}"
    if deferred:
        detail += f"; deferred: {', '.join(deferred)}"
    detail += f"; {chain_note}; {reap_note}"
    if problems:
        detail += f"; failed: {', '.join(problems)}"
    return LegResult("failed" if problems else "ran", run_close.scrub_detail(detail))


def _reap_note(ctx: JobContext, problems: list[str]) -> str:
    """Run the abandoned-run reaper in-process, last; a raise or ``ok: false`` is appended to
    ``problems`` as ``reap <reason>`` and never stops the leg. Trap: the leg carries no session id, so
    the reaper's peer-claim withhold does not run here."""
    try:
        from coordinator_core.ops.dispatch_emit import abandoned_runs

        reply = abandoned_runs._handler({}, repo_root=ctx.root)
    except Exception as exc:
        problems.append(f"reap {type(exc).__name__}")
        return "reaped: not run"
    if not isinstance(reply, dict) or not reply.get("ok"):
        reason = reply.get("error", "not ok") if isinstance(reply, dict) else "bad reply"
        problems.append(f"reap {reason}")
        return "reaped: not run"
    reaped = reply.get("reaped") or []
    return f"reaped: {len(reaped)} run(s)"


def _chain_closure_note(ctx: JobContext, problems: list[str]) -> str:
    """Run ``chain_verdicts`` for the claimed batons; a raise is appended to ``problems``."""
    if not ctx.claimed_batons:
        return "chain-closure: run claims no baton"
    try:
        verdicts = _bin_script("baton-chain-closure").chain_verdicts(ctx.root, list(ctx.claimed_batons))
    except Exception as exc:  # the launchers already ran; the leg is recorded failed
        problems.append(f"baton-chain-closure {type(exc).__name__}")
        return "chain-closure: not run"
    closed = sorted({v.chain for v in verdicts if v.closed and v.chain})
    note = f"chain-closure: {len(verdicts)} baton(s), {len(closed)} chain(s) closed"
    return f"{note}: {', '.join(closed)}" if closed else note


DEFAULT_RUNNERS: Mapping[str, Runner] = {
    "baton-drain": _baton_drain,
    "orientation-refresh": _orientation_refresh,
    "memo-lifecycle": _memo_lifecycle,
    "post-close": _post_close,
}


def _now_for(deadline_text: str) -> datetime:
    aware = datetime.now().astimezone()
    try:
        return aware if aware.tzinfo and datetime.fromisoformat(deadline_text).tzinfo else datetime.now()
    except ValueError:
        return aware


def _load_record(root: Path, run_id: str) -> Any:
    try:
        return json.loads((root / run_close.close_job_path(run_id)).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FileNotFoundError(run_close.close_job_path(run_id)) from exc


def _queued_legs(
    root: Path, run_id: str, legs: Sequence[str] = BACKGROUND_LEGS, record: Any = None
) -> list[str]:
    """Legs the record still lists ``queued`` and whose deadline has not passed, in ledger order.

    ``record`` is the already-parsed close-job record; omitted, it is read here."""
    if record is None:
        record = _load_record(root, run_id)
    resolved = run_close.read_close_job(root, run_id, _now_for(str(record.get("deadline", ""))))
    return [leg for leg in legs if leg in resolved and resolved[leg].status == "queued"]


def run_background(
    ctx: JobContext,
    runners: Optional[Mapping[str, Runner]] = None,
    budget_ms: float = JOB_BUDGET_MS,
    cpu_ms: Callable[[], float] = lambda: time.process_time() * 1000.0,
) -> dict[str, LegResult]:
    """Pay each queued background leg in ledger order and record its outcome.

    Raises FileNotFoundError when the run has no close-job record. Returns leg -> outcome.
    """
    table = DEFAULT_RUNNERS if runners is None else runners
    started = cpu_ms()
    outcomes: dict[str, LegResult] = {}
    record = _load_record(ctx.root, ctx.run_id)
    if ctx.terminal_sha is None:
        sha = record.get("terminal_sha") if isinstance(record, dict) else None
        if isinstance(sha, str) and sha:
            ctx = replace(ctx, terminal_sha=sha)
    for leg in _queued_legs(ctx.root, ctx.run_id, record=record):
        spent = cpu_ms() - started
        if spent >= budget_ms:
            result = LegResult("failed", f"not started: job process time {spent:.0f}ms reached {budget_ms:.0f}ms")
        else:
            try:
                result = table[leg](ctx)
            except Exception as exc:  # a raising leg is recorded; the others still run
                result = _failed("", exc)
        outcomes[leg] = result
        run_close.record_leg_outcome(ctx.root, ctx.run_id, leg, result.status, result.detail)
    return outcomes


def _context(root: Path, common_dir: Path, params: Mapping[str, Any]) -> JobContext:
    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id is required")
    batons = params.get("claimed_batons") or []
    if not isinstance(batons, (list, tuple)) or not all(isinstance(b, str) for b in batons):
        raise ValueError("claimed_batons must be a list of repo-relative paths")
    memo = params.get("source_memo")
    if memo is not None and not isinstance(memo, str):
        raise ValueError("source_memo must be a string")
    return JobContext(root, common_dir, run_id, tuple(batons), memo or None)


def _summary(run_id: str, outcomes: Mapping[str, LegResult]) -> dict[str, Any]:
    return {"run_id": run_id, "legs": {leg: r.to_dict() for leg, r in outcomes.items()}}


@register_op("close.background")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "close.background": pay the run's queued background legs.

    Params: run_id (str), claimed_batons (list of repo-relative paths), source_memo (str|null).
    repo_root: the git common dir injected by dispatch. Returns ``{run_id, legs}`` telemetry;
    the caller never acts on it (FIRE_AND_FORGET). A malformed request or a missing close-job
    record returns ``{"error": ...}``.
    """
    from coordinator_core.lifecycle import main_worktree_root

    if repo_root is None:
        return {"error": "close.background requires a repo root"}
    try:
        ctx = _context(main_worktree_root(Path(repo_root)), Path(repo_root), params or {})
        return _summary(ctx.run_id, run_background(ctx))
    except (ValueError, FileNotFoundError) as exc:
        return {"error": run_close.scrub_detail(f"{type(exc).__name__}: {exc}")}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry for ``spawn_detached``: ``close_background.py <run_id> --root DIR --params JSON``."""
    parser = argparse.ArgumentParser(description="close.background job")
    parser.add_argument("run_id")
    parser.add_argument("--root", required=True)
    parser.add_argument("--params", default="{}", help="JSON: claimed_batons, source_memo")
    args = parser.parse_args(argv)
    from coordinator_core.git.git_dir import resolve_git_common_dir

    root = Path(args.root)
    try:
        params = {**json.loads(args.params), "run_id": args.run_id}
        run_background(_context(root, resolve_git_common_dir(root), params))
    except (ValueError, FileNotFoundError) as exc:
        print(run_close.scrub_detail(f"close.background: {type(exc).__name__}: {exc}"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

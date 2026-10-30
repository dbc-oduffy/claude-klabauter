"""
coordinator_core.ops.ceremony.close_background -- JSON-RPC "close.background": pays the run
close legs that cannot fit inside the landing op and records each outcome in the run's
close-job record.

Purpose: the landing op writes the close-job record (``run_close.write_close_job``), launches
this module by ``detached_spawn.spawn_detached`` (``python close_background.py ...``) and returns.
This job pays ``BACKGROUND_LEGS`` in order and rewrites the record through
``run_close.record_leg_outcome``; the reader (``run_close.read_close_job``) turns a leg this job
never recorded into ``failed``. The landing's deferred legs (review-stamp mint, coded stamp, sizing
ship, implemented flip, deliverable cascade, completion entry, baton ship, lessons) write tracked
records; ``close-commit`` then lands exactly the paths those legs wrote in one commit.

Negative-spec:
  - Runs only the legs the record still lists as ``queued``, and none once its deadline passed.
  - One leg's exception is recorded ``failed`` and the next leg still runs.
  - A leg is not started once the job's cumulative process time reaches ``JOB_BUDGET_MS``; it is
    recorded ``queued`` ("deferred#N"), and ``run_and_continue`` detaches one continuation job for
    it. A leg deferred ``MAX_STALLED_DEFERRALS`` times by jobs that ran nothing is ``failed``.
    ``close-commit`` is exempt, and is re-queued when a leg before it was deferred.
  - No leg globs ``state/sizings`` or scans transcripts: the cascade reads the records the run's own
    plan names (``deliverable_cascade.cascade_run``).
  - The close commit carries only the paths the legs wrote; a peer's dirty file is never staged.
    A lost compare-and-swap re-seeds from the new HEAD up to ``CLOSE_COMMIT_CAS_RETRIES`` times, then
    the leg is ``failed`` naming the still-dirty paths.
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
import re
import sys
import time
from dataclasses import dataclass, field, replace
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
    "review-stamp",
    "coded-stamp",
    "sizing-ship",
    "plan-reconciliation",
    "deliverable-cascade",
    "completion-entry",
    "baton-ship",
    "lessons",
    "close-commit",
    "baton-drain",
    "orientation-refresh",
    "memo-lifecycle",
    "post-close",
    "symbol-knowledge",
)


def owed_on_reclose(statuses: Mapping[str, str]) -> list[str]:
    """The background legs a re-close re-runs: those ``queued`` or ``failed``, plus a ``not-applicable``
    ``sizing-ship`` when ``plan-reconciliation`` is owed again -- it settles the register that gates it."""
    owed = {leg for leg in BACKGROUND_LEGS if statuses.get(leg) in ("queued", "failed")}
    if "plan-reconciliation" in owed and statuses.get("sizing-ship") == "not-applicable":
        owed.add("sizing-ship")
    return [leg for leg in BACKGROUND_LEGS if leg in owed]


UNBUDGETED_LEGS: frozenset[str] = frozenset({"close-commit"})
"""Legs the budget guard never stops: they land what the earlier legs already wrote."""

MAX_STALLED_DEFERRALS = 2
"""A leg deferred this many times in a row by jobs that ran nothing is recorded ``failed``."""

_DEFER_TAG = "deferred#"

CLOSE_COMMIT_CAS_RETRIES = 3
"""Re-seeds from the new HEAD after a lost compare-and-swap, before the close commit gives up."""

_HANDOFF_SWEEP_CAP = 150
_SIZINGS_SWEEP_NOTE = "sweep-terminal-sizings not run: 638ms cold, over the 500ms bar"
_CEREMONY_NAME = "run-close"

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
    record: Mapping[str, Any] = field(default_factory=dict)
    written: list = field(default_factory=list)
    trailers: list = field(default_factory=list)
    """``record`` is the parsed close-job record; ``written`` the repo-relative paths the legs wrote
    (what ``close-commit`` lands); ``trailers`` the lines the close commit's message carries."""


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
    from coordinator_core.ops.dispatch_emit import process_artifact_policy

    if not process_artifact_policy.hosts_coordinator_state(ctx.root):
        return LegResult("not-applicable", "repo not opted into coordinator state")
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

    A raise or a commit_error marks the clause failed; the caller still archives. A run whose
    ``deliverable-cascade`` leg already ran skips the corpus op: that leg advanced the run's own records."""
    try:
        paid = (_load_record(ctx.root, ctx.run_id).get("legs") or {}).get("deliverable-cascade")
        if isinstance(paid, dict) and paid.get("status") in ("ran", "discharged"):
            return "cascade: paid by deliverable-cascade", False
    except FileNotFoundError:
        pass
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
    from coordinator_core.ops.ceremony.memo_close_reply import send_close_reply

    reply = send_close_reply(ctx.root, _plan_rel(ctx), ctx.source_memo, ctx.run_id, ctx.terminal_sha)
    if reply is not None and reply.status == "failed":
        return reply
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
    note = f"; {reply.detail}" if reply is not None else ""
    return LegResult("ran", run_close.scrub_detail(f"source memo {name} resolved{note}"))


# --------------------------------------------------------------------------------------------
# Deferred landing legs. Each reads the close-job record (``ctx.record``), the plan on disk and
# the terminal commit's object, writes tracked records in place and notes the paths in
# ``ctx.written`` for ``close-commit``. None runs git, globs a corpus or scans a transcript.
# --------------------------------------------------------------------------------------------

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_RESOLUTION_SHA = re.compile(r"^[0-9a-f]{7,64}$")
_LANDS_CHUNKS = re.compile(r"^Lands-Chunks:[ \t]*(?P<v>.+?)\s*$", re.M)
_HALTED_WALK_CAP = 500
SIZING_LEDGER_REL = ".coordinator-local/sizings.jsonl"


_NOT_OPTED_IN = "repo not opted into coordinator state"


def _not_opted_in(ctx: JobContext) -> bool:
    """True when the repo has not opted into hosting coordinator state; legs that write state/,
    docs/plans or archive/ return not-applicable (a write would create archive/ and flip the signal)."""
    from coordinator_core.ops.dispatch_emit import process_artifact_policy

    return not process_artifact_policy.hosts_coordinator_state(ctx.root)


def _leg(status: str, text: str, ctx: Optional[JobContext] = None) -> LegResult:
    return LegResult(status, run_close.scrub_detail(text, ctx.root if ctx is not None else None))


def _leg_full(status: str, text: str, ctx: Optional[JobContext] = None) -> LegResult:
    """``_leg`` without the ``DETAIL_CAP`` clip: each whitespace token is scrubbed on its own (a path
    token never spans whitespace), so a refusal's field, value and reason survive whole."""
    root = ctx.root if ctx is not None else None
    cap = run_close.DETAIL_CAP
    tokens = ["".join(run_close.scrub_detail(tok[i:i + cap], root) for i in range(0, len(tok), cap))
              for tok in str(text).split()]
    return LegResult(status, " ".join(tokens))


def _inputs(ctx: JobContext) -> Mapping[str, Any]:
    value = ctx.record.get("inputs")
    return value if isinstance(value, dict) else {}


def _plan_rel(ctx: JobContext) -> Optional[str]:
    value = ctx.record.get("plan_rel")
    return value if isinstance(value, str) and value else None


def _criterion(ctx: JobContext) -> Optional[dict]:
    value = ctx.record.get("criterion")
    return value if isinstance(value, dict) else None


def _note_written(ctx: JobContext, *rels: str) -> None:
    for rel in rels:
        if rel and rel not in ctx.written:
            ctx.written.append(rel)


def _read(ctx: JobContext, rel: str) -> Optional[str]:
    from coordinator_core.ops.dispatch_emit.worktree_snapshot import read_rel

    return read_rel(ctx.root, rel)


def _plan_fm(text: str) -> dict:
    from coordinator_core.frontmatter.primitives import split_frontmatter
    from coordinator_core.ops.dispatch_emit.plan_row_coding import yaml_load

    split = split_frontmatter(text.replace("\r\n", "\n"))
    try:
        fm = (yaml_load(split.fm_text) or {}) if split is not None else {}
    except Exception:  # noqa: BLE001 -- an unparseable frontmatter reads as empty
        return {}
    return fm if isinstance(fm, dict) else {}


def _spine_rows(text: str) -> list:
    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
    from coordinator_core.ops.dispatch_emit.plan_row_coding import yaml_load

    located = locate_fenced_block(text.replace("\r\n", "\n"))
    if located.status != LocateStatus.LOCATED:
        return []
    try:
        rows = yaml_load(located.body) or []
    except Exception:  # noqa: BLE001
        return []
    return [r for r in rows if isinstance(r, dict) and r.get("id")] if isinstance(rows, list) else []


def _open_ids(rows: Sequence[dict]) -> set:
    from coordinator_core.execute_plan_assemble.row_spans import _row_disposition

    return {str(r["id"]) for r in rows if _row_disposition(r) == "open"}


def _meta(ctx: JobContext, sha: Optional[str]) -> Optional[dict]:
    from coordinator_core.git.commit_walk import commit_meta

    return commit_meta(ctx.common_dir, sha) if sha else None


def _blob(ctx: JobContext, sha: Optional[str], rel: str) -> Any:
    from coordinator_core.git.rollback_check import _blob_at_commit

    return _blob_at_commit(ctx.common_dir, sha, rel) if sha else None


def _lands_chunk_ids(message: str) -> list:
    return sorted({
        i for m in _LANDS_CHUNKS.finditer(message) for i in re.split(r"[,\s]+", m.group("v")) if i
    })


def _close_kind(text: str) -> tuple:
    """``(close kind, plan_governs)`` off the plan frontmatter, as ``run_close.CLOSE_KINDS`` names them;
    the landing op's own classifier, so the two cannot disagree."""
    from coordinator_core.ops.dispatch_emit.land_run import _close_kind as landing_close_kind

    return landing_close_kind(text)


def _queue_review(ctx: JobContext, receipt_rel: str) -> None:
    """Point the record at the stamped file and queue the ``review`` leg ``post-close`` launches."""
    try:
        record = _load_record(ctx.root, ctx.run_id)
        if "review" in (record.get("legs") or {}):
            return
        deadline = datetime.fromisoformat(str(record.get("deadline", "")))
        run_close.amend_close_job(
            ctx.root, ctx.run_id, review_receipt=receipt_rel, queue=("review",), deadline=deadline
        )
    except Exception:  # noqa: BLE001 -- the stamp stands; the review leg then reads absent
        pass


def _review_stamp(ctx: JobContext) -> LegResult:
    """Mint the plan's ``review_stamp`` (a mise run: the receipt on its inventory record) against the
    terminal commit. A refusal is the leg's ``failed`` detail; the landed commit stands."""
    plan_rel, sha = _plan_rel(ctx), ctx.terminal_sha
    if not plan_rel:
        return run_close.review_skipped("no-plan", root=ctx.root)
    if not sha:
        return run_close.review_skipped("no-terminal-commit", root=ctx.root)
    if _not_opted_in(ctx):
        return run_close.review_skipped("not-opted-in", _NOT_OPTED_IN, ctx.root)
    from coordinator_core.ops import review_stamp as rs
    from coordinator_core.ops._path_guard import contained_path
    from coordinator_core.ops.dispatch_emit import plan_row_coding
    from coordinator_core.ops.dispatch_emit.ask_contract import SPINE_SUFFIX

    if contained_path(ctx.root / plan_rel, [ctx.root]) is None:
        return run_close.review_unminted("plan path escapes the worktree", ctx.root)
    resolved = None
    record_rel = _inputs(ctx).get("review_record")
    if isinstance(record_rel, str) and record_rel:
        data = rs._load_sidecar(ctx.root / record_rel)
        if data is None:
            return run_close.review_unminted(f"review record {record_rel} is unreadable", ctx.root)
        resolved = (sha, ctx.root / record_rel, data)
    try:
        if plan_rel.endswith(SPINE_SUFFIX):
            found = plan_row_coding.inventory_record_rel(ctx.root, plan_rel)
            if found is None:
                return run_close.review_skipped("no-inventory-record", root=ctx.root)
            rs.mint_receipt(ctx.root / found[0], ctx.root, resolved=resolved)
            stamped = found[0]
        else:
            rs.mint(
                ctx.root / plan_rel, ctx.root, build_test_path=None, resolved=resolved, criterion_open_ok=True
            )
            stamped = plan_rel
    except rs.MintRefusal as exc:
        return run_close.review_unminted(str(exc), ctx.root)
    _note_written(ctx, stamped)
    _queue_review(ctx, stamped)
    return _leg("ran", f"review stamp minted on {stamped}", ctx)


def _checkpoint_rows_from(ctx: JobContext, plan_rel: str, text: str, start: Any, exclude: frozenset) -> dict:
    """``{checkpoint sha: {plan: {row id}}}`` of still-open rows same-plan checkpoints landed, walking
    first parents in-process from *start*."""
    if not isinstance(start, str) or not _FULL_SHA.fullmatch(start):
        return {}
    wanted = frozenset(_open_ids(_spine_rows(text)) - set(exclude))
    if not wanted:
        return {}
    from coordinator_core.ops.dispatch_emit import plan_row_coding
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import checkpoint_rows_landed, plan_created_floor

    walk = checkpoint_rows_landed(
        ctx.root, plan_rel, start, wanted, floor_epoch=plan_created_floor(text), cap=_HALTED_WALK_CAP
    )
    landed: dict = {}
    for csha, ids in walk.landed.items():
        by_plan = plan_row_coding.source_rows_by_plan(ctx.root, plan_rel, sorted(ids))
        if by_plan:
            landed[csha] = by_plan
    return landed


def _checkpoint_of(ctx: JobContext, plan_rel: str, sha: str, chunks: list) -> dict:
    """Chunk id -> sha of the run's own ``checkpoint(wave N)`` commit that landed its writes, walking
    first parents from the terminal commit's parent while the subject is a checkpoint of this plan."""
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import (
        CHECKPOINT_PLAN_LINE,
        CHECKPOINT_SUBJECT,
        is_stamp_commit,
    )

    parents = (_meta(ctx, sha) or {}).get("parents") or []
    cur = parents[0] if parents else None
    checkpoints: list = []  # newest first: (sha, parent, named ids)
    for _ in range(_HALTED_WALK_CAP):
        meta = _meta(ctx, cur)
        if meta is None:
            break
        message = meta.get("message") or ""
        if is_stamp_commit(message) and meta.get("parents"):
            cur = meta["parents"][0]
            continue
        subject = CHECKPOINT_SUBJECT.match((message.splitlines() or [""])[0].strip())
        plan = CHECKPOINT_PLAN_LINE.search(message)
        if not subject or not meta.get("parents") or not plan or plan.group("v") != plan_rel:
            break
        named = {i.strip() for i in subject.group("ids").split(",") if i.strip()}
        checkpoints.append((cur, meta["parents"][0], named))
        cur = meta["parents"][0]
    out: dict = {}
    for chunk in chunks:
        hits = [
            (csha, named) for csha, parent, named in reversed(checkpoints)
            if any(_blob(ctx, csha, p) != _blob(ctx, parent, p) for p in chunk.paths)
        ]
        if hits:
            out[chunk.id] = next((h[0] for h in hits if chunk.id in h[1]), hits[0][0])
    return out


def _review_only_hits(ctx: JobContext, sha: str, review_only: Mapping[str, Any], open_ids: set) -> tuple:
    """``({row id: sha}, [row ids with no delta])`` for a review-only run: the newest commit in
    ``run_base..sha`` changing a blob in the row's writes closes it."""
    base = review_only.get("run_base")
    wanted = {str(i) for i in review_only.get("open_rows") or []} & open_ids
    rows = [r for r in review_only.get("rows") or [] if isinstance(r, dict) and str(r.get("id")) in wanted]
    if not isinstance(base, str) or not base or not rows:
        return {}, []
    delta: list = []  # newest first: (sha, parent)
    cur, reached = sha, False
    for _ in range(_HALTED_WALK_CAP):
        if cur.startswith(base):
            reached = True
            break
        meta = _meta(ctx, cur)
        if meta is None or not meta.get("parents"):
            break
        delta.append((cur, meta["parents"][0]))
        cur = meta["parents"][0]
    if not reached:
        delta = []
    hits: dict = {}
    no_delta: list = []
    for row in rows:
        paths = [p for p in row.get("paths") or [] if isinstance(p, str)]
        hit = next((c for c, parent in delta if any(_blob(ctx, c, p) != _blob(ctx, parent, p) for p in paths)), None)
        if hit is None:
            no_delta.append(str(row["id"]))
        else:
            hits[str(row["id"])] = hit
    return hits, no_delta


def _land_if_withheld(text: str) -> str:
    """``text`` with the plan's status advanced to ``landed`` when every open row is withheld behind an
    uncleared gate or ``needs_slot`` (the rung ``roadmap.plan_gate`` counts as coded); else unchanged."""
    from coordinator_core.ops.dispatch_emit.spine_read import coded_or_withheld
    from coordinator_core.roadmap.blitz_land import LandingRefused, _read_field, _set_field

    if (_read_field(text, "status") or "").lower() not in ("draft", "reviewed", "approved", "executing"):
        return text
    if not coded_or_withheld(text):
        return text
    crlf = "\r\n" in text
    try:
        new = _set_field(text.replace("\r\n", "\n"), "status", "landed")
    except LandingRefused:
        return text
    return new.replace("\n", "\r\n") if crlf else new


def _coded_stamp(ctx: JobContext) -> LegResult:
    """Stamp the run's credited rows ``coded`` in the plan, each against the commit that landed it: the
    terminal commit, the run's own checkpoint, an outside landing, or (no change) the terminal commit with
    a no-op detail. Rows an earlier fire, a halt commit or a review-only run landed close at that commit."""
    plan_rel, sha = _plan_rel(ctx), ctx.terminal_sha
    if not plan_rel or not sha:
        return _leg("not-applicable", "run has no plan or terminal commit")
    from coordinator_core.ops.dispatch_emit import process_artifact_policy

    if not process_artifact_policy.hosts_coordinator_state(ctx.root):
        return _leg("not-applicable", "repo not opted into coordinator state")
    text = _read(ctx, plan_rel)
    if text is None:
        return _leg("failed", f"plan {plan_rel} is unreadable", ctx)
    from types import SimpleNamespace

    from coordinator_core.ops.dispatch_emit import plan_row_coding as prc

    inputs = _inputs(ctx)
    rows = {str(r["id"]): r for r in _spine_rows(text)}
    given = inputs.get("credited_rows")
    credited = [str(i) for i in given] if isinstance(given, list) else _lands_chunk_ids((_meta(ctx, sha) or {}).get("message") or "")
    parents = (_meta(ctx, sha) or {}).get("parents") or []
    parent = parents[0] if parents else None

    groups: dict = {}  # (sha, detail or None) -> {run-plan row id}
    undecided: list = []
    for cid in credited:
        paths = [w for w in (rows.get(cid, {}).get("writes") or []) if isinstance(w, str) and w]
        if not paths:
            groups.setdefault((sha, prc.NOOP_DETAIL), set()).add(cid)
        elif any(_blob(ctx, sha, p) != _blob(ctx, parent, p) for p in paths):
            groups.setdefault((sha, None), set()).add(cid)
        else:
            undecided.append(SimpleNamespace(id=cid, paths=paths))
    attributed = _checkpoint_of(ctx, plan_rel, sha, undecided) if undecided else {}
    for cid, csha in attributed.items():
        groups.setdefault((csha, None), set()).add(cid)
    rest = [c for c in undecided if c.id not in attributed]
    outside = prc.outside_landings(ctx.root, sha, plan_rel, rest) if rest else {}
    for cid, shas in outside.items():
        groups.setdefault((shas[0], prc.outside_landing_detail(shas)), set()).add(cid)
    for c in rest:
        if c.id not in outside:
            groups.setdefault((sha, prc.NOOP_DETAIL), set()).add(c.id)

    no_delta: list = []
    review_only = inputs.get("review_only")
    criterion = _criterion(ctx)
    if isinstance(review_only, dict) and not (criterion or {}).get("status") == "not_met":
        hits, no_delta = _review_only_hits(ctx, sha, review_only, _open_ids(list(rows.values())))
        for rid, csha in hits.items():
            groups.setdefault((csha, None), set()).add(rid)

    coding: dict = {}  # plan -> [(sha, ids, details)]
    own = {i for ids in groups.values() for i in ids}
    for (csha, detail), ids in groups.items():
        for plan, mapped in prc.source_rows_by_plan(ctx.root, plan_rel, sorted(ids)).items():
            coding.setdefault(plan, []).append((csha, set(mapped), {r: detail for r in mapped} if detail else {}))
    resume = inputs.get("resume_halt_sha")
    predecessor: dict = {}
    if isinstance(resume, str) and _FULL_SHA.fullmatch(resume):
        halt_ids = _lands_chunk_ids((_meta(ctx, resume) or {}).get("message") or "")
        predecessor = prc.source_rows_by_plan(ctx.root, plan_rel, halt_ids) if halt_ids else {}
        for plan, ids in predecessor.items():
            coding.setdefault(plan, []).append((resume, set(ids), {}))
    earlier = _checkpoint_rows_from(
        ctx, plan_rel, text, resume if isinstance(resume, str) else (parents[0] if parents else None), frozenset(own)
    )
    for csha, by_plan in earlier.items():
        for plan, ids in by_plan.items():
            fresh = set(ids) - set(predecessor.get(plan, ()))
            if fresh:
                coding.setdefault(plan, []).append((csha, fresh, {}))

    from coordinator_core.frontmatter.schema_validate import check_plan_tasks_source
    from coordinator_core.session.claimed_write import replace_text

    coded = 0
    for plan, steps in sorted(coding.items()):
        original = _read(ctx, plan)
        if original is None:
            continue
        updated, flipped_all = original, set()
        for csha, ids, details in steps:
            updated, flipped = prc.flip_rows_coded(updated, set(ids) - flipped_all, csha, details)
            flipped_all.update(flipped)
        if not flipped_all:
            continue
        updated = _land_if_withheld(updated)
        invalid = check_plan_tasks_source(updated)
        if invalid is not None and check_plan_tasks_source(original) is None:
            return _leg("failed", f"{plan}: stamped spine fails schema: {invalid}", ctx)
        replace_text(ctx.root / plan, updated)
        _note_written(ctx, plan)
        coded += len(flipped_all)
    detail = f"{coded} row(s) coded ({sha[:7]})"
    if no_delta:
        detail += f"; review-only rows with no delta left open: {', '.join(no_delta)}"
    return _leg("ran", detail, ctx)


def _unsent_memos(ctx: JobContext, text: str) -> list:
    """``row id (slug)`` of the plan's open memo-send rows whose sender-side receipt is absent."""
    from coordinator_core.ops.dispatch_emit.memo_rows import _is_memo_send_row, memo_receipt_exists, memo_row_slug

    out: list = []
    for row in _spine_rows(text):
        if row.get("performer") == "em" or (row.get("disposition") or "open") != "open" or not _is_memo_send_row(row):
            continue
        slug = memo_row_slug(row)
        if slug is None or not memo_receipt_exists(ctx.root, slug):
            out.append(f"{row['id']} ({slug or 'no slug'})")
    return out


def _sibling_wait_notes(entries: Any) -> dict[str, str]:
    """Row id -> ``" (waits on sibling run <id>, <status>; after it lands: <refire>)"`` from the close
    input ``sibling_blocked``; first entry per row wins, malformed entries are skipped."""
    notes: dict[str, str] = {}
    if not isinstance(entries, list):
        return notes
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("row") or not entry.get("run_id"):
            continue
        notes.setdefault(
            str(entry["row"]),
            f" (waits on sibling run {entry['run_id']}, {entry.get('status')}; after it lands: {entry.get('refire')})",
        )
    return notes


def _plan_flip(ctx: JobContext) -> LegResult:
    """Write the plan's ``implemented`` status (uncommitted; ``close-commit`` lands it) once its minted
    review stamp records a met criterion, no cascade (the ``deliverable-cascade`` leg owns it)."""
    plan_rel, sha = _plan_rel(ctx), ctx.terminal_sha
    if not plan_rel or not sha:
        return _leg("not-applicable", "run has no plan or terminal commit")
    from coordinator_core.ops.dispatch_emit import process_artifact_policy

    if not process_artifact_policy.hosts_coordinator_state(ctx.root):
        return _leg("not-applicable", "repo not opted into coordinator state")
    text = _read(ctx, plan_rel)
    if text is None:
        return _leg("failed", f"plan {plan_rel} is unreadable", ctx)
    kind, governs = _close_kind(text)
    preset = run_close.kind_presets(kind, plan_governs=governs).get("plan-reconciliation")
    if preset is not None:
        return preset
    tells = ctx.record.get("falsifier_broken")
    if isinstance(tells, list):
        return _leg(
            "failed",
            "falsifier integrity BROKEN at pre-dispatch (" + (", ".join(tells) or "no tell named")
            + "): not self-stamped; close through the EM re-judge route",
            ctx,
        )
    inputs = _inputs(ctx)
    from coordinator_core.ops.dispatch_emit.spine_read import NON_DISPATCHABLE_DISPOSITIONS

    resolved = {
        str(r.get("id")) for r in _spine_rows(text)
        if (r.get("disposition") or "open") in NON_DISPATCHABLE_DISPOSITIONS
    }
    remaining = [c for c in (inputs.get("incomplete") or ()) if str(c) not in resolved]
    if remaining:
        return _leg("failed", f"incomplete chunks remain: {sorted(remaining)}", ctx)
    fm = _plan_fm(text)
    stamp = fm.get("review_stamp")
    if not isinstance(stamp, dict):
        return _leg("failed", "no review stamp minted")
    unsent = _unsent_memos(ctx, text)
    if unsent:
        return _leg("failed", "memo not sent (no sender-side receipt): " + ", ".join(unsent), ctx)
    import copy

    from coordinator_core.ops.dispatch_emit import register_gate

    judged = _criterion(ctx)
    if judged is not None:
        reason = register_gate.register_gate(
            ctx.root, plan_rel, {"criterion": copy.deepcopy(judged)}, tuple(inputs.get("ledger_judged") or ()),
            run_id=ctx.run_id,
        )
        if reason:
            return _leg("failed", reason, ctx)
    from coordinator_core.archive_stamp import _open_spine_row_ids

    open_rows = _open_spine_row_ids(ctx.root / plan_rel)
    if open_rows:
        waits = _sibling_wait_notes(inputs.get("sibling_blocked"))
        shown = [f"{row}{waits[row]}" if row in waits else row for row in open_rows]
        return _leg("failed", f"{len(open_rows)} spine row(s) still open: {', '.join(shown)}", ctx)
    criterion = stamp.get("criterion") if isinstance(stamp.get("criterion"), dict) else {}
    status = criterion.get("status")
    ledger_met = inputs.get("ledger_met") if isinstance(inputs.get("ledger_met"), str) else None
    if status == "met":
        observation = str(criterion.get("observation") or "").strip()
        source = criterion.get("sidecar") or "the run's criterion leg"
    elif ledger_met and ledger_met.strip() and status != "not_met":
        observation, source = ledger_met.strip(), "requirements ledger"
    else:
        return _leg("failed", "review_stamp criterion is not met")
    if not observation:
        return _leg("failed", "criterion observation is empty")
    from coordinator_core.ops import plan_status_transition

    import contextlib
    import io

    why = (judged or {}).get("reason")
    recorded = fm.get("exit_criterion_met") is not None
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        rc = plan_status_transition.stamp_implemented(
            str(ctx.root / plan_rel),
            **(
                {}
                if recorded
                else {
                    "falsifier_verdict": "pass",
                    "falsifier_output": observation[: plan_status_transition._FALSIFIER_OUTPUT_MAX],
                    "prose": f"criterion met at {sha} per {source}"
                    + (f"; {why.strip()}" if isinstance(why, str) and why.strip() else ""),
                }
            ),
            no_commit=True,
            ship_sha=sha,
            ledger_met=ledger_met if status != "met" else None,
            cascade=False,
        )
    if rc != 0:
        return _leg("failed", err.getvalue().strip(), ctx)
    _note_written(ctx, plan_rel)
    deferred = [] if kind == "xs-s-run" else sorted(
        str(r["id"]) for r in _spine_rows(_read(ctx, plan_rel) or "") if r.get("deferred") is True
    )
    if deferred:
        return _leg("failed", "deferred rows unharvested: " + ", ".join(deferred), ctx)
    return _leg("ran", "plan implemented")


def _cited(ctx: JobContext, plan_rel: str) -> tuple:
    """``(sizing rel, baton rel)`` the plan's ``sizing_object`` and that sizing's ``baton`` name, each
    ``None`` when absent or not an existing file in its records home."""
    from coordinator_core.ops._path_guard import contained_path
    from coordinator_core.ops.dispatch_emit import register_gate
    from coordinator_core.ops.dispatch_emit.plan_row_coding import yaml_load
    from coordinator_core.session.record_homes import home_dir

    text = _read(ctx, plan_rel)
    cited = _plan_fm(text).get("sizing_object") if text is not None else None
    sizing_rel = register_gate.sizing_file_rel(ctx.root, cited) if isinstance(cited, str) and cited else None
    if sizing_rel is None:
        return None, None
    body = _read(ctx, sizing_rel)
    try:
        doc = yaml_load(body) if body is not None else None
    except Exception:  # noqa: BLE001
        doc = None
    baton = doc.get("baton") if isinstance(doc, dict) else None
    if not isinstance(baton, str) or not baton:
        return sizing_rel, None
    guarded = contained_path(ctx.root / baton, [Path(home_dir(str(ctx.root), "handoffs"))])
    if guarded is None or not guarded.is_file():
        return sizing_rel, None
    return sizing_rel, guarded.relative_to(ctx.root.resolve()).as_posix()


def _write_resolution(ctx: JobContext, sizing_rel: str, sha: str) -> Optional[str]:
    """Write ``resolution: {sha, run_id}`` beside the shipped sizing's status; an error text or None."""
    from coordinator_core.frontmatter.primitives import write_fm_nested_field
    from coordinator_core.frontmatter.schema_validate import format_validation_errors, validate_frontmatter
    from coordinator_core.locked_write import MutateAbort, locked_rmw
    from coordinator_core.ops.dispatch_emit.plan_row_coding import yaml_load
    from coordinator_core.ops.dispatch_emit.register_gate import RESOLUTION_RUN_ID

    if not _RESOLUTION_SHA.match(sha or "") or not RESOLUTION_RUN_ID.match(ctx.run_id or ""):
        return f"unrecordable resolution sha={sha!r} run_id={ctx.run_id!r}"
    schema_path = Path(__file__).resolve().parents[2] / "frontmatter" / "schemas" / "sizing-object.schema.json"

    def mutate(old: str) -> str:
        new = write_fm_nested_field(old, "resolution", f'  sha: "{sha}"\n  run_id: "{ctx.run_id}"\n')
        errors = validate_frontmatter(yaml_load(new) or {}, schema_path)
        if errors:
            raise MutateAbort(f"resolution write refused: {format_validation_errors(errors)}")
        return new

    try:
        locked_rmw(ctx.root / sizing_rel, mutate, repo_root=ctx.root)
    except MutateAbort as exc:
        return str(exc.args[0]) if exc.args else "resolution write refused"
    except Exception as exc:  # noqa: BLE001 -- lock timeout, I/O
        return f"resolution write refused: {type(exc).__name__}"
    return None


def _record_resolution(ctx: JobContext, sizing_rel: str, sha: str) -> None:
    """Append the sizing's row to the local resolution ledger (never committed); failures are dropped."""
    import hashlib
    import json as _json
    from datetime import timezone

    from coordinator_core.atomic_append import append_line
    from coordinator_core.ops.dispatch_emit.plan_row_coding import yaml_load

    try:
        doc = yaml_load(_read(ctx, sizing_rel) or "") or {}
        estimate = doc.get("estimate") if isinstance(doc.get("estimate"), dict) else {}
        words = "\n\n".join(
            str(v.get("text")) for v in doc.get("pm_verbatims") or [] if isinstance(v, dict) and v.get("text")
        ) or str(doc.get("intent") or "")
        row = {
            "id": doc.get("deliverable_id") or Path(sizing_rel).stem,
            "tshirt": estimate.get("tshirt") or "?",
            "route": doc.get("route"),
            "ask_sha256": hashlib.sha256(words.encode("utf-8")).hexdigest(),
            "resolved_sha": sha,
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sizing": sizing_rel,
        }
        sink = ctx.root / SIZING_LEDGER_REL
        sink.parent.mkdir(parents=True, exist_ok=True)
        append_line(sink, (_json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))
    except Exception:  # noqa: BLE001 -- a local ledger row is a report, never a gate
        pass


def _ledger_met_rows(ctx: JobContext) -> list:
    """Judge rows for the register ids the passed requirements ledger judged met (``ledger_met`` is
    set only when the ledger passed with nothing open); an unmet row is absent, so it stays open."""
    inputs = _inputs(ctx)
    text = inputs.get("ledger_met")
    if not isinstance(text, str) or ":" not in text:
        return []
    judged = {str(i) for i in inputs.get("ledger_judged") or ()}
    ids = [i.strip() for i in text.split(":", 1)[1].split(",") if i.strip()]
    return [
        {"id": i, "claim": "this-plan", "status": "met", "wired": True, "observed_ref": "requirements ledger"}
        for i in ids if i in judged
    ]


def _sizing_ship(ctx: JobContext) -> LegResult:
    """Write the judged register rows back onto the cited sizing, then ship it through ``sizing.ship``;
    an applied ship stamps the resolution and adds the ``Sizing-Shipped`` trailer to the close commit."""
    plan_rel, sha = _plan_rel(ctx), ctx.terminal_sha
    if not plan_rel or not sha:
        return _leg("not-applicable", "run has no plan or terminal commit")
    sizing_rel, _ = _cited(ctx, plan_rel)
    if sizing_rel is None:
        return _leg("not-applicable", "plan cites no sizing")
    if _not_opted_in(ctx):
        return _leg("not-applicable", _NOT_OPTED_IN)
    from coordinator_core.ops.dispatch_emit import register_gate
    from coordinator_core.warm.entry_seam import reentrant_dispatch

    notes: list = []
    criterion = _criterion(ctx)
    ledger_rows = _ledger_met_rows(ctx)
    if ledger_rows:
        merged = {**(criterion or {}), "register_rows": [*((criterion or {}).get("register_rows") or []), *ledger_rows]}
        settled = register_gate.register_writeback(
            ctx.root, plan_rel, {"criterion": merged}, sha, also_claim=[r["id"] for r in ledger_rows]
        )
        if settled.get("register_sizing"):
            _note_written(ctx, settled["register_sizing"])
            notes.append("ledger-met register rows written")
        elif settled.get("register_writeback_error"):
            return _leg_full("failed", f"ledger-met rows: {settled['register_writeback_error']}", ctx)
    if _inputs(ctx).get("incomplete"):
        return _leg_full(
            "ran" if notes else "not-applicable", "; ".join([*notes, "run incomplete: sizing stays live"]), ctx
        )
    wrote = register_gate.register_writeback(ctx.root, plan_rel, {"criterion": criterion} if criterion else None, sha)
    if wrote.get("register_sizing"):
        _note_written(ctx, wrote["register_sizing"])
        notes.append("register rows written")
    elif wrote.get("register_writeback_error"):
        notes.append(f"register write-back refused: {wrote['register_writeback_error']}")
    try:
        reply = reentrant_dispatch("sizing.ship", {"sizing_path": sizing_rel}, repo_root=ctx.common_dir)
    except Exception as exc:  # noqa: BLE001 -- recorded; the landing stands
        return _leg("failed", f"sizing.ship raised {type(exc).__name__}", ctx)
    if not isinstance(reply, dict) or reply.get("exit_code") != 0:
        refusal = reply.get("error") if isinstance(reply, dict) else reply
        return _leg_full("failed", "; ".join([*notes, f"ship skipped: {refusal}"]), ctx)
    if not reply.get("applied"):
        return _leg_full("ran", "; ".join([*notes, "sizing already shipped"]), ctx)
    error = _write_resolution(ctx, sizing_rel, sha)
    _note_written(ctx, sizing_rel)
    ctx.trailers.append(f"Sizing-Shipped: {sizing_rel} {sha}")
    _record_resolution(ctx, sizing_rel, sha)
    notes.append(f"sizing {sizing_rel} shipped" + (f"; resolution not written: {error}" if error else ""))
    return _leg_full("failed" if error else "ran", "; ".join(notes), ctx)


def _deliverable_cascade(ctx: JobContext) -> LegResult:
    """Advance the sizing and baton records the run's own plan names, via ``cascade_run`` -- never the corpus."""
    plan_rel, sha = _plan_rel(ctx), ctx.terminal_sha
    if not plan_rel or not sha:
        return _leg("not-applicable", "run has no plan or terminal commit")
    if _not_opted_in(ctx):
        return _leg("not-applicable", _NOT_OPTED_IN)
    text = _read(ctx, plan_rel)
    fm = _plan_fm(text) if text is not None else {}
    if fm.get("status") != "implemented":
        return _leg("not-applicable", "plan is not implemented")
    sizing_rel, baton_rel = _cited(ctx, plan_rel)
    rels = list(dict.fromkeys(
        r for r in (sizing_rel, baton_rel, *(Path(b).as_posix() for b in ctx.claimed_batons)) if r
    ))
    from coordinator_core.ops.dispatch_emit.plan_linked_handoffs import linked_handoffs

    rels = list(dict.fromkeys([*rels, *linked_handoffs(ctx.root, plan_rel)]))
    if not rels:
        return _leg("not-applicable", "run names no sizing or baton record")
    from coordinator_core.ops.deliverable_cascade import cascade_run

    did = fm.get("deliverable_id")
    session = ctx.record.get("session_id")
    reply = cascade_run(
        ctx.root, did.strip() if isinstance(did, str) else "", rels, sha,
        repo_root=ctx.common_dir, plan_path=plan_rel, caller_session_id=session if isinstance(session, str) else "",
    )
    _note_written(ctx, *reply.get("mutated_paths", ()))
    return _leg(
        "ran",
        f"cascade over {len(rels)} record(s): {len(reply['advanced'])} advanced, "
        f"{len(reply['already_advanced'])} already, {len(reply['refused'])} refused, "
        f"{len(reply['unreadable'])} unreadable",
        ctx,
    )


def _completion_entry(ctx: JobContext) -> LegResult:
    plan_rel = _plan_rel(ctx)
    if not plan_rel:
        return _leg("not-applicable", "run has no plan")
    if _not_opted_in(ctx):
        return _leg("not-applicable", _NOT_OPTED_IN)
    from coordinator_core.ops.dispatch_emit.run_close_completion import completion_entry_leg

    ledger_met = _inputs(ctx).get("ledger_met")
    result, written = completion_entry_leg(
        ctx.root, ctx.run_id, plan_rel, _criterion(ctx), ledger_met=ledger_met if isinstance(ledger_met, str) else None
    )
    if result.status in ("ran", "discharged"):
        _note_written(ctx, *written)
    return result


def _baton_ship(ctx: JobContext) -> LegResult:
    if not ctx.claimed_batons:
        return _leg("not-applicable", "run claims no baton")
    if not ctx.terminal_sha:
        return _leg("failed", "no terminal commit to ship the batons at")
    criterion = _criterion(ctx)
    status = criterion.get("status") if criterion else None
    ledger_met = _inputs(ctx).get("ledger_met")
    if not (status == "met" or (bool(ledger_met) and status != "not_met")):
        return _leg("failed", f"criterion {status or 'absent'}: batons not shipped")
    from coordinator_core.ops.dispatch_emit.run_close_baton_ship import baton_ship_leg

    result = baton_ship_leg(ctx.root, ctx.run_id, list(ctx.claimed_batons), ctx.terminal_sha)
    if result.status == "ran":
        _note_written(ctx, *ctx.claimed_batons)
    return result


def _lessons(ctx: JobContext) -> LegResult:
    from coordinator_core.ops.dispatch_emit.run_close_lessons import lessons_leg

    return lessons_leg(ctx.root, ctx.run_id, _criterion(ctx))


def _run_closed_lines(ctx: JobContext) -> list:
    """``Checkpoint-Run-Closed`` lines for the run's bases when its terminal commit was a landing
    (not a halt, not a checkpoint) that does not already name them."""
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import (
        _HALT_SUBJECT_PREFIX,
        _LANDING_TRAILER_LINE,
        parse_checkpoint,
        run_closed_bases,
        run_closed_trailer_lines,
    )
    from coordinator_core.ops.dispatch_emit.landing_integrity import closing_bases

    message = (_meta(ctx, ctx.terminal_sha) or {}).get("message") or ""
    if message.startswith(_HALT_SUBJECT_PREFIX) or not _LANDING_TRAILER_LINE.search(message):
        return []
    if parse_checkpoint(message) is not None:
        return []
    closed = run_closed_bases(message)
    return run_closed_trailer_lines(b for b in closing_bases(ctx.root, ctx.run_id, ctx.record) if b not in closed)


def _close_commit(ctx: JobContext) -> LegResult:
    """Land exactly the paths the legs wrote in one Form 4 commit, never a peer's dirty file. A lost
    compare-and-swap re-seeds from the new HEAD (``CLOSE_COMMIT_CAS_RETRIES`` times), then the leg fails
    naming the paths still dirty."""
    from coordinator_core.ops.dispatch_emit import process_artifact_policy

    if not process_artifact_policy.hosts_coordinator_state(ctx.root):
        process_artifact_policy.exclude_locally(ctx.root, ctx.written)
        return _leg("not-applicable", "repo not opted into coordinator state")
    paths = list(dict.fromkeys(ctx.written))
    if not paths:
        return _leg("not-applicable", "no leg wrote a tracked path")
    from functools import partial

    from coordinator_core.git.commit import (
        CommitRefused,
        FilterUnsupported,
        NothingToCommit,
        commit_paths,
        hash_worktree_blobs_via_spawn,
    )
    from coordinator_core.git.commit_trailers import apply_missing_trailers
    from coordinator_core.git.index_write import IndexStaleAfterCommit, IndexWriteError
    from coordinator_core.ops.dispatch_emit.worktree_snapshot import worktree_snapshot

    snapshot = worktree_snapshot(ctx.root, paths, [], [])
    if snapshot is not None:
        ignored = set(snapshot.ignored_among(paths))
        paths = [p for p in paths if p in snapshot.changed and p not in ignored]
    if not paths:
        return _leg("ran", "no written path differs from HEAD")
    message = f"close: {len(paths)} path(s) for run {ctx.run_id}"
    trailers = [*ctx.trailers, *_run_closed_lines(ctx)]
    if trailers:
        message += "\n\n" + "\n".join(trailers)
    message = apply_missing_trailers(message, ctx.root, paths)
    reason = ""
    for _attempt in range(CLOSE_COMMIT_CAS_RETRIES + 1):
        try:
            outcome = commit_paths(
                ctx.root, paths, message, prefer_deliberate_stage=True,
                blob_fallback=partial(hash_worktree_blobs_via_spawn, cwd=ctx.root),
            )
        except NothingToCommit:
            return _leg("ran", "nothing differs from HEAD")
        except IndexStaleAfterCommit:
            return _leg("ran", f"{len(paths)} path(s) landed; index stale")
        except (FilterUnsupported, IndexWriteError) as exc:
            reason = str(exc)
            break
        except CommitRefused as exc:
            reason = str(exc)
            if "compare-and-swap failed" not in reason:
                break
            continue
        return _leg("ran", f"{len(paths)} path(s) landed at {outcome.sha[:10]}")
    return _leg("failed", f"close commit not landed; still dirty: {', '.join(paths)} ({reason})", ctx)


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
    """Flag structural-index staleness (never refresh), fire the scip launcher and the
    detached ``close.review`` job, then run ``baton-chain-closure`` for the claimed
    batons and record its outcome."""
    from coordinator_core.ops.ceremony import post_close_admission as admission

    launched: list[str] = []
    deferred: list[str] = []
    problems: list[str] = []
    avail_mb, trusted = admission.read_available()
    admitted_mb = 0
    flag_note = ""
    try:
        flag_note = _bin_script("structural-index-refresh").flag_staleness(ctx.root) or ""
    except Exception as exc:  # a flag read never stops the launchers
        problems.append(f"structural-index-flag {type(exc).__name__}")
    for label, call in (
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
    if flag_note:
        detail += f"; {flag_note}"
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


def _symbol_knowledge(ctx: JobContext) -> LegResult:
    from coordinator_core.symbol_knowledge.leg import symbol_knowledge_leg

    return symbol_knowledge_leg(ctx)


DEFAULT_RUNNERS: Mapping[str, Runner] = {
    "review-stamp": _review_stamp,
    "coded-stamp": _coded_stamp,
    "sizing-ship": _sizing_ship,
    "plan-reconciliation": _plan_flip,
    "deliverable-cascade": _deliverable_cascade,
    "completion-entry": _completion_entry,
    "baton-ship": _baton_ship,
    "lessons": _lessons,
    "close-commit": _close_commit,
    "baton-drain": _baton_drain,
    "orientation-refresh": _orientation_refresh,
    "memo-lifecycle": _memo_lifecycle,
    "post-close": _post_close,
    "symbol-knowledge": _symbol_knowledge,
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


def _sizing_unshipped(ctx: JobContext) -> bool:
    """True when the plan cites a sizing whose on-disk ``status`` is still ``sized`` or ``routed``."""
    plan_rel = _plan_rel(ctx)
    if not plan_rel or not ctx.terminal_sha or _inputs(ctx).get("incomplete"):
        return False
    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted

    sizing_rel, _ = _cited(ctx, plan_rel)
    text = _read(ctx, sizing_rel) if sizing_rel else None
    return text is not None and read_fm_field_unquoted(text, "status") in ("sized", "routed")


def _legs_owed(ctx: JobContext, record: Any) -> list[str]:
    """The queued legs, plus ``sizing-ship`` and the ``close-commit`` landing its write when the leg was
    recorded ``not-applicable`` or ``failed`` yet the cited sizing is still unshipped on disk."""
    owed = set(_queued_legs(ctx.root, ctx.run_id, record=record))
    legs = record.get("legs") if isinstance(record, dict) else None
    entry = legs.get("sizing-ship") if isinstance(legs, dict) else None
    if (
        isinstance(entry, dict)
        and entry.get("status") in ("not-applicable", "failed")
        and _sizing_unshipped(ctx)
    ):
        owed.update(("sizing-ship", "close-commit"))
    return [leg for leg in BACKGROUND_LEGS if leg in owed]


def _no_legs_cause(root: Path, run_id: str) -> str:
    """Why a record that exists owes nothing: each leg's recorded status, as one line."""
    record = _load_record(root, run_id)
    legs = record.get("legs") if isinstance(record, dict) else None
    if not isinstance(legs, dict) or not legs:
        return "close-job record lists no legs"
    statuses = ", ".join(
        f"{leg}={entry.get('status') if isinstance(entry, dict) else '?'}" for leg, entry in legs.items()
    )
    return f"no leg is queued and no sizing-ship is owed (cited sizing shipped or none cited): {statuses}"


def run_background(
    ctx: JobContext,
    runners: Optional[Mapping[str, Runner]] = None,
    budget_ms: float = JOB_BUDGET_MS,
    cpu_ms: Callable[[], float] = lambda: time.process_time() * 1000.0,
) -> dict[str, LegResult]:
    """Pay each owed background leg (``_legs_owed``) in ledger order and record its outcome.

    Raises FileNotFoundError when the run has no close-job record. Returns leg -> outcome.
    """
    table = DEFAULT_RUNNERS if runners is None else runners
    started = cpu_ms()
    outcomes: dict[str, LegResult] = {}
    record = _load_record(ctx.root, ctx.run_id)
    ctx = replace(ctx, record=record if isinstance(record, dict) else {})
    if ctx.terminal_sha is None:
        sha = record.get("terminal_sha") if isinstance(record, dict) else None
        if isinstance(sha, str) and sha:
            ctx = replace(ctx, terminal_sha=sha)
    ran_any = False
    deferred: list[str] = []
    for leg in _legs_owed(ctx, record):
        spent = cpu_ms() - started
        if leg not in UNBUDGETED_LEGS and spent >= budget_ms:
            reason = f"job process time {spent:.0f}ms reached {budget_ms:.0f}ms"
            count = _deferrals(record, leg) + 1
            if count >= MAX_STALLED_DEFERRALS and not ran_any:
                result = LegResult("failed", f"not started: {reason}; deferred {count}x with no progress")
            else:
                result = LegResult("queued", f"{_DEFER_TAG}{count}: {reason}")
                deferred.append(leg)
        else:
            try:
                result = table[leg](ctx)
            except Exception as exc:  # a raising leg is recorded; the others still run
                result = _failed("", exc)
            ran_any = True
        outcomes[leg] = result
        run_close.record_leg_outcome(ctx.root, ctx.run_id, leg, result.status, result.detail)
    commit = outcomes.get("close-commit")
    if deferred and commit is not None and commit.status != "queued" and any(
        BACKGROUND_LEGS.index(d) < BACKGROUND_LEGS.index("close-commit") for d in deferred if d in BACKGROUND_LEGS
    ):
        again = LegResult("queued", f"{_DEFER_TAG}1: lands the deferred legs' writes")
        run_close.record_leg_outcome(ctx.root, ctx.run_id, "close-commit", again.status, again.detail)
    _settle_unqueued_review(ctx, outcomes.get("review-stamp"))
    _release_landed_claims(ctx)
    return outcomes


def _settle_unqueued_review(ctx: JobContext, stamp: Optional[LegResult]) -> None:
    """The landing reply names ``review`` queued; when the ``review-stamp`` leg settled without queueing it,
    nothing would ever pay or time it out. Record it ``not-applicable`` with the stamp leg's reason."""
    if stamp is None or stamp.status == "queued":
        return
    try:
        record = _load_record(ctx.root, ctx.run_id)
        if "review" in (record.get("legs") or {}):
            return
        deadline = datetime.fromisoformat(str(record.get("deadline", "")))
        run_close.amend_close_job(ctx.root, ctx.run_id, queue=("review",), deadline=deadline)
        reason = stamp.detail or f"review-stamp {stamp.status}"
        run_close.record_leg_outcome(
            ctx.root, ctx.run_id, "review", "not-applicable", run_close.scrub_detail(f"review did not run: {reason}", ctx.root)
        )
    except Exception as exc:  # noqa: BLE001 -- the stamp leg's own outcome stands
        try:
            run_close.record_leg_outcome(
                ctx.root, ctx.run_id, "review", "failed", run_close.scrub_detail(f"review settle failed: {exc}", ctx.root)
            )
        except Exception:  # noqa: BLE001 -- no record to write to
            pass


def _deferrals(record: Any, leg: str) -> int:
    entry = record.get("legs", {}).get(leg) if isinstance(record, dict) else None
    match = re.match(rf"{_DEFER_TAG}(\d+):", str(entry.get("detail", ""))) if isinstance(entry, dict) else None
    return int(match.group(1)) if match else 0


def _launch_continuation(ctx: JobContext) -> bool:
    """Detach one new ``close_background.py`` job for the legs this job deferred."""
    from coordinator_core.ops.ceremony.detached_spawn import spawn_detached

    engine = Path(__file__).resolve().parents[3]
    params = json.dumps({"claimed_batons": list(ctx.claimed_batons), "source_memo": ctx.source_memo})
    return spawn_detached(
        str(engine), str(Path(__file__).resolve()), [ctx.run_id, "--root", str(ctx.root), "--params", params]
    )


def run_and_continue(
    ctx: JobContext, launch: Callable[[JobContext], bool] = _launch_continuation, **kwargs: Any
) -> dict[str, LegResult]:
    """``run_background``, then one detached continuation when it deferred legs after making progress."""
    outcomes = run_background(ctx, **kwargs)
    deferred = [leg for leg, r in outcomes.items() if r.status == "queued"]
    progressed = any(r.status != "queued" for r in outcomes.values())
    if deferred and progressed and not launch(ctx):
        for leg in deferred:
            reason = "continuation job did not start"
            run_close.record_leg_outcome(ctx.root, ctx.run_id, leg, "failed", reason)
            outcomes[leg] = LegResult("failed", reason)
    return outcomes


def _release_landed_claims(ctx: JobContext) -> None:
    """One in-process release of the landing session's claims on the paths the landing carried;
    a failure retains the claims and never touches a leg outcome."""
    record = ctx.record
    sid = record.get("session_id")
    landed = _inputs(ctx).get("landed_paths")
    if not isinstance(sid, str) or not isinstance(landed, list):
        return
    from coordinator_core.session.scope import release_committed_claims_in_process_or_retain

    release_committed_claims_in_process_or_retain(ctx.root, [p for p in landed if isinstance(p, str)], sid, "close_background")


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
        outcomes = run_and_continue(ctx)
        if not outcomes:
            cause = _no_legs_cause(ctx.root, ctx.run_id)
            return {"error": run_close.scrub_detail(f"no legs paid for run {ctx.run_id}: {cause}")}
        return _summary(ctx.run_id, outcomes)
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
        run_and_continue(_context(root, resolve_git_common_dir(root), params))
    except (ValueError, FileNotFoundError) as exc:
        print(run_close.scrub_detail(f"close.background: {type(exc).__name__}: {exc}"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
coordinator_core.ops.dispatch_emit.queue_emit — the queue-grind engine's
entrypoint: profile + appetite + queue dirs -> one `QueueEmission`.

Purpose: `emit_queue_script` is the ONE call DoE makes (docs/plans/2026-09-21-
bug-blitz-emitter-engine-leg.md § Design § Entrypoint, § Answers to DoE's
addendum questions #5). It composes the pipeline this plan built in the
depended-on chunks -- `grind_profile.load_profile`/`validate_graph`/
`resolve_appetite` (C3), `queue_select.select_rows` (C2), and
`grind_compose.compose_grind_script` (C7) -- into one fireable `.mjs` script
plus the receipt extras `dispatch.emit`'s queue route writes beside it. This
module owns none of that pipeline logic itself: it resolves a profile, folds
overrides into knobs, selects rows, composes the script, and reports what it
did. It does NOT touch disk for a write -- that is `op.py`'s job, same
division `emit.emit_script` already keeps from `op.py`'s own write leg.

Wire shape:
    `emit_queue_script(profile, appetite="standard", overrides=None, *,
    queue, profile_dir, repo_root, run_dir, session_id=None,
    agent_type_host=None) -> QueueEmission`. `overrides` keys are a subset of
    `{"where", "limit", "budget_tokens"}` (`grind_vocab.OVERRIDABLE_KNOBS`) --
    `resolve_appetite` refuses any other key (`UnoverridableKnobError`).
    `queue` is one or more queue directories; `run_dir` is the guarded output
    path's parent (the caller -- `op.py` -- derives it from the already
    path-guarded script path, never from a raw caller string).

`QueueEmission.receipt_extras` carries `queue`, `admitted_count` (rows admitted
into the script; top-level twin of `not_admitted.arithmetic.admitted_count`),
`profile`, `profile_digest`
(sha256 of the loaded profile's own source bytes -- a distinct fact from
`triage_policy_sha256`, which digests only the inlined `triage_policy` text,
not the whole profile), `appetite`, `resolved_knobs`, `manifest_digest`
(`Manifest.digest` of the admitted-rows manifest, never the manifest itself --
§ Design § Selector, "Frozen means frozen"), `not_admitted` (`{count, row_ids,
arithmetic}`: selected rows the emit-time ceilings (`grind_admission.admit`) did
not cover, left out of the manifest; distinct from `unselected`, which is rows
filtered by `where`/`limit`), `claimed` (`{count, rows: [{row_id,
holder_session}]}`: rows skipped because another live session holds their row
claim), `source` (the manifest's own `SourceOpResult`, JSON-shaped, or
`None`), and `reemit` -- the argv list a caller re-runs
(`emit-dispatch-workflow.py --queue ... --profile ... --appetite ... [...]`)
to re-derive this same script from the same queue/profile/appetite/overrides
inputs. `op.py :: _write_emission_receipt` writes `plan: null` itself; this
dict does NOT repeat that key.

Containment: every `queue` directory and `run_dir` are refused unless they
resolve under `repo_root` (`coordinator_core.ops._path_guard.contained_path`)
-- the same post-`.resolve()` symlink-safe containment shape every other
write-adjacent op in this package uses, applied here to read-side directories
because a queue dir named outside the repo is exactly the kind of fat-finger
the guard exists to catch (module docstring,
`coordinator_core/ops/_path_guard.py`).

Negative-spec:
  - Writes no script or receipt: `op.py` is that write leg; this module returns the
    script TEXT and the receipt extras dict. The only writes here are the ones
    `select_rows(write_handback_marks=True)` makes and, for a
    `mechanical_close.PROFILES` profile, the closures `mechanical_close.apply_closures`
    makes before any LLM sees a row (receipt key `mechanically_closed`).
  - Does NOT re-implement `select_rows`/`load_profile`/`validate_graph`/
    `resolve_appetite`/`compose_grind_script`. Every disk read, ledger
    fold-in, `where` evaluation, and script-body composition happens inside
    those functions; this module only sequences their calls and its own
    containment/digest/reemit bookkeeping.
  - Does NOT fall back to a plan-spine emission on missing queue/profile
    params -- `op.py`'s queue route and its plan route are mutually
    exclusive (`QueuePlanConflictError`), and queue input never falls back to
    the wave path (§ Design § Entrypoint).
  - Does NOT resolve or call a source op itself -- `source` is forwarded
    straight from the loaded profile's own (optional) `source` block to
    `select_rows`, which is the one module that resolves/calls it
    (`queue_select._call_source_op`, S1). A profile with no `source` block
    passes `None`, unchanged from before.
  - Does NOT derive `run_dir` from a raw caller string. The caller (`op.py`)
    is responsible for deriving it from an already-guarded script path before
    calling in -- this module only re-checks containment under `repo_root`,
    it does not resolve or default it.

Spec backlink: docs/plans/2026-09-21-bug-blitz-emitter-engine-leg.md § Design
§ Entrypoint, § Answers to DoE's addendum questions #5, Tasks § C8.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Optional, Sequence

from coordinator_core.git.git_state import head_sha
from coordinator_core.contract import run_budget
from coordinator_core.learn_lessons_pipeline import grind_cost, mechanical_close
from coordinator_core.ops import backlog_pass_ledger
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit.grind_admission import (
    NOT_ADMITTED_EXTRA_KEY,
    admit,
    claimed_entries,
    not_admitted_entries,
    receipt_block,
)
from coordinator_core.ops.dispatch_emit.grind_profile import (
    load_profile,
    resolve_appetite,
    validate_graph,
)
from coordinator_core.ops.dispatch_emit.queue_select import (
    Manifest,
    load_row_schema_gate,
    restrict_manifest,
    select_rows,
)
from coordinator_core.ops.dispatch_emit.receipt_extras_guard import refuse_colliding_receipt_extras
from coordinator_core.session import core

__all__ = ["QueueEmission", "QueuePathEscapeError", "emit_queue_script"]


class QueuePathEscapeError(ValueError):
    pass


class QueueEmission(NamedTuple):

    script: str
    receipt_extras: dict


def _resolved_batch_sizes(resolved_knobs: Mapping[str, Any]) -> dict:
    batch_size = resolved_knobs.get("batch_size", 4)
    if isinstance(batch_size, Mapping):
        return dict(batch_size)
    return {"default": batch_size}


def _reemit_argv(
    profile: str,
    appetite: str,
    profile_dir: Path,
    queue: Sequence[Path],
    overrides: Optional[Mapping[str, Any]],
    stage_models: Optional[Mapping[str, str]] = None,
) -> list:
    argv: list = [
        "emit-dispatch-workflow.py",
        "--profile", profile,
        "--appetite", appetite,
        "--profile-dir", str(profile_dir),
    ]
    for queue_dir in queue:
        argv += ["--queue", str(queue_dir)]
    overrides = overrides or {}
    if "where" in overrides:
        argv += ["--where", json.dumps(overrides["where"], sort_keys=True)]
    if "limit" in overrides:
        argv += ["--limit", str(overrides["limit"])]
    if "budget_tokens" in overrides:
        argv += ["--budget-tokens", str(overrides["budget_tokens"])]
    for kind, model in sorted((stage_models or {}).items()):
        argv += ["--stage-model", f"{kind}={model}"]
    return argv


def _fit_to_admitted_share(
    manifest: Manifest,
    kept: list,
    loaded_profile: Any,
    knobs: Mapping[str, Any],
    repo_root: Path,
    open_rows: int,
) -> int:
    """How many leading `kept` rows' triage batches stay within their share of the whole-backlog bar."""
    from coordinator_core.ops.dispatch_emit.grind_compose import _group_into_batches

    def batches_for(n: int) -> int:
        part = restrict_manifest(manifest, kept[:n])
        return len(_group_into_batches(part, knobs, loaded_profile, repo_root=repo_root))

    return run_budget.fit_to_share(
        run_budget.load(grind_cost.CEREMONY),
        grind_cost.profile_unit_cost(loaded_profile, knobs),
        batches_for,
        len(kept),
        open_rows,
    )


def _close_mechanically(
    manifest: Manifest,
    loaded_profile: Any,
    profile_dir: Path,
    repo_root: Path,
    limit: Optional[int],
    knobs: Optional[Mapping[str, Any]] = None,
) -> tuple[Manifest, list, int]:
    """Close what `mechanical_close` can with no LLM, drop those rows, then apply `limit` and the
    learn-lessons run budget to the survivors in queue order (the smaller wins). Returns the
    narrowed manifest, the closed rows' receipt entries and the survivor count before narrowing;
    rows left out stay in the queue for the next run."""
    verdicts = mechanical_close.close_candidates(manifest, repo_root)
    closed_ids = set(
        mechanical_close.apply_closures(
            verdicts,
            manifest,
            profile_dir=profile_dir,
            profile=loaded_profile.name,
            repo_root=repo_root,
            run_stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        )
    )
    survivors = [e.row_id for e in manifest.entries if e.row_id not in closed_ids]
    total_survivors = len(survivors)
    kept = survivors if limit is None else survivors[:limit]
    kept = grind_cost.fit_survivors(
        manifest, kept, loaded_profile, knobs or {}, repo_root, run_budget.load(grind_cost.CEREMONY)
    )
    if kept:
        kept = kept[: _fit_to_admitted_share(manifest, kept, loaded_profile, knobs or {}, repo_root, total_survivors)]
    narrowed = restrict_manifest(manifest, kept)
    narrowed = replace(
        narrowed,
        unselected=tuple(sorted(set(narrowed.unselected) | (set(survivors) - set(kept)))),
    )
    return narrowed, [
        {"row_id": v.row_id, "verdict": v.verdict, "covered_by": v.covered_by, "reason": v.reason}
        for v in verdicts
        if v.row_id in closed_ids
    ], total_survivors


def _budget_receipt(
    manifest: Manifest,
    loaded_profile: Any,
    knobs: Mapping[str, Any],
    repo_root: Path,
    run_id: str,
    total_survivors: int,
) -> dict:
    """The receipt's `budget` and `deferred`, and the backlog-pass ledger line for this run."""
    from coordinator_core.ops.dispatch_emit.grind_compose import _group_into_batches

    batches = len(_group_into_batches(manifest, knobs, loaded_profile, repo_root=repo_root))
    unit = grind_cost.profile_unit_cost(loaded_profile, knobs)
    prevet = int(getattr(getattr(loaded_profile, "prevet", None), "batch_size", 0) or 0)
    rates = grind_cost.role_rates(prevet > 0)
    rows = len(manifest.entries)
    has_verify = any(n.kind == "verify" for n in loaded_profile.graph.values())
    spawns = {
        "prevet": -(-rows // prevet) if prevet > 0 else 0,
        "triage": batches,
        "verify": batches if has_verify else 0,
    }
    projected = grind_cost.projected_cost(unit, batches, spawns, rates)
    budget = run_budget.scaled_budget(run_budget.load(grind_cost.CEREMONY), rows, total_survivors)
    deferred = total_survivors - len(manifest.entries)
    backlog_pass_ledger.append(
        str(repo_root), grind_cost.CEREMONY, run_id, projected.tokens, len(manifest.entries), deferred
    )
    return {"budget": run_budget.budget_report(budget, projected), "deferred": deferred}


def emit_queue_script(
    profile: str,
    appetite: str = "standard",
    overrides: Optional[Mapping[str, Any]] = None,
    *,
    queue: Sequence[Path],
    profile_dir: Path,
    repo_root: Path,
    run_dir: Path,
    session_id: Optional[str] = None,
    agent_type_host: Optional[str] = None,
    preamble: Optional[str] = None,
    commit_trailers: Sequence[str] = (),
    review_roster_fragment: Optional[dict] = None,
    review_stage_schemas: Optional[dict] = None,
    stage_models: Optional[Mapping[str, str]] = None,
) -> QueueEmission:
    """Both review kwargs thread to compose_grind_script; either being None
    raises NoReviewStageError."""
    # Deferred: grind_compose pulls the grind chain, which a --plan emit must not load.
    from coordinator_core.ops.dispatch_emit.grind_compose import _group_into_batches, compose_grind_script

    repo_root = Path(repo_root).resolve()

    guarded_run_dir = contained_path(Path(run_dir), [repo_root])
    if guarded_run_dir is None:
        raise QueuePathEscapeError(
            f"run_dir escapes repo_root: {run_dir!r} not under {repo_root!r}"
        )

    guarded_queue_dirs: list = []
    for queue_dir in queue:
        guarded = contained_path(Path(queue_dir), [repo_root])
        if guarded is None:
            raise QueuePathEscapeError(
                f"queue dir escapes repo_root: {queue_dir!r} not under {repo_root!r}"
            )
        guarded_queue_dirs.append(guarded)

    loaded_profile = load_profile(profile, profile_dir)
    validate_graph(loaded_profile)

    resolved_knobs = resolve_appetite(
        loaded_profile, appetite, dict(overrides) if overrides else None
    )

    # A mechanical-close profile selects its whole eligible set: the limit applies to the
    # survivors, so closing duplicates frees admission slots instead of shrinking the capped set.
    close_mechanically = loaded_profile.name in mechanical_close.PROFILES
    profile_limit = resolved_knobs.get("limit")

    manifest = select_rows(
        guarded_queue_dirs,
        where=resolved_knobs.get("where"),
        order=(loaded_profile.priority["field"], loaded_profile.priority["order"]),
        limit=None if close_mechanically else profile_limit,
        batch_key=list(loaded_profile.batch_key),
        batch_sizes=_resolved_batch_sizes(resolved_knobs),
        row_id_key=loaded_profile.row_id_key,
        profile=loaded_profile.name,
        repo_root=repo_root,
        source=loaded_profile.source,
        absent_sentinels=loaded_profile.absent_sentinels,
        source_row_dir=Path(guarded_run_dir) / "source-rows",
        claimant_sid=core.resolve_session_id(str(repo_root)) or None,
        row_schema_gate=load_row_schema_gate(loaded_profile, repo_root),
        write_handback_marks=True,
    )

    mechanically_closed: list = []
    total_survivors = len(manifest.entries)
    if close_mechanically:
        manifest, mechanically_closed, total_survivors = _close_mechanically(
            manifest, loaded_profile, Path(profile_dir), repo_root, profile_limit, resolved_knobs
        )

    run_order = [
        entry.row_id
        for _batch_id, chunk in _group_into_batches(manifest, resolved_knobs)
        for entry in chunk
    ]
    admission = admit(
        run_order,
        max_agent_calls=resolved_knobs.get("max_agent_calls"),
        budget_tokens=resolved_knobs.get("budget_tokens"),
        batch_size=int(_resolved_batch_sizes(resolved_knobs).get("default", 4)),
    )
    manifest = restrict_manifest(manifest, admission.admitted)

    budget_extra = None
    if close_mechanically:
        budget_extra = _budget_receipt(
            manifest, loaded_profile, resolved_knobs, repo_root, Path(guarded_run_dir).name, total_survivors
        )

    script = compose_grind_script(
        manifest,
        loaded_profile,
        resolved_knobs,
        run_dir=Path(os.path.relpath(guarded_run_dir, repo_root)).as_posix(),
        appetite=appetite,
        agent_type_host=agent_type_host,
        preamble=preamble,
        commit_trailers=commit_trailers,
        review_roster_fragment=review_roster_fragment,
        review_stage_schemas=review_stage_schemas,
        review_run_base_sha=head_sha(repo_root) or "",
        queue_dirs=[Path(os.path.relpath(q, repo_root)).as_posix() for q in guarded_queue_dirs],
        stage_models=stage_models,
        not_admitted=not_admitted_entries(admission) + claimed_entries(manifest.claimed),
        repo_root=repo_root,
    )

    profile_digest = hashlib.sha256(loaded_profile.source_path.read_bytes()).hexdigest()

    source_extra = None
    if manifest.source is not None:
        source_extra = {
            "op": manifest.source.op,
            "args": dict(manifest.source.args),
            "output_sha256": manifest.source.output_sha256,
        }

    declined = [{"id": d.row_id, "path": d.path, "reason": d.reason} for d in manifest.declined]
    for d in declined:
        print(f"declined {d['id']} ({d['path']}): {d['reason']}", file=sys.stderr)

    receipt_extras = {
        "declined": declined,
        "mechanically_closed": mechanically_closed,
        **(budget_extra or {}),
        "queue": [str(q) for q in queue],
        "profile": loaded_profile.name,
        "profile_digest": profile_digest,
        "appetite": appetite,
        "resolved_knobs": dict(resolved_knobs),
        "manifest_digest": manifest.digest,
        "admitted_count": len(admission.admitted),
        "unselected": {"count": len(manifest.unselected), "row_ids": list(manifest.unselected)},
        "claimed": {
            "count": len(manifest.claimed),
            "rows": [
                {"row_id": c.row_id, "holder_session": c.holder_session} for c in manifest.claimed
            ],
        },
        NOT_ADMITTED_EXTRA_KEY: receipt_block(admission),
        "source": source_extra,
        "reemit": _reemit_argv(profile, appetite, Path(profile_dir), queue, overrides, stage_models),
    }

    refuse_colliding_receipt_extras(receipt_extras)
    return QueueEmission(script=script, receipt_extras=receipt_extras)

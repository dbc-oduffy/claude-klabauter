"""
coordinator_core.ops.distill_curation_status — JSON-RPC "distill.curation_status"
operation (C11; DEC-1).

Purpose: thin op orchestrator (DEC-5 — ops compose the library, the library stays
pure) over `coordinator_core.distill.curation_status.compute_curation_status`. Answers
AC4's three questions from disk truth — unharvested-RIPE count, last-run id + age,
prunable set with reasons — and, when `params["emit"]` is truthy, additionally writes
the schema_version-pinned `state/ceremony/curation-status.json` fleet-visibility
artifact (DEC-1: cockpit/rag/siblings MAY consume it; claude-klabauter never depends on their
consumption).

`last_run_id`/`last_run_age_seconds` mean "the last DISTILL run recorded in the
canonical distillation log" (`_last_distill_run`, derived from
`state/distillation-log.md`), NOT "the last time this op itself emitted." The two
used to be conflated — see `_last_distill_run`'s docstring for the 2026-07-23 fix.

MUTATING (DR-208 fail-closed default; `artifact.emit` — classification.py:78-82 — is
the friendly existing precedent for exactly this "compute, optionally write one JSON
artifact" write shape). Sanctioned as a scratch-tier writer under
docs/decisions/DR-228-distill-disposal-substrate-writer-category.md § D6:
write-confined to the single fixed path `state/ceremony/curation-status.json`
(D6-i — a point-in-time derived snapshot, not run-scoped, unlike `distill.scope` /
`memo.fate_partition`'s per-run scratch directories), create-or-full-rewrite only via
`coordinator_core.locked_write.locked_rmw` (D6-ii — atomic mkstemp+replace under a
cross-process flock, never a partial in-place edit), no delete (D6-iii), no commit
(D6-iv — the artifact lands on disk uncommitted; committing it, if warranted, is the
calling EM/session's own job via the ordinary git surfaces), schema_version-pinned
(D6-v, via `manifest_schema.make_curation_status`).

Self-registration: importing this module calls register_op("distill.curation_status", ...)
as a side-effect (same pattern as ops/artifact_emit.py). coordinator_core.ops.__init__
imports it so the registration fires at start_server() time (AC14).

Spec backlink: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C11 (DEC-1)
Governing DR: docs/decisions/DR-228-distill-disposal-substrate-writer-category.md § D6
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Optional

from coordinator_core.distill import manifest_schema
from coordinator_core.distill._common import parse_distillation_log
from coordinator_core.distill.curation_status import compute_curation_status
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import locked_rmw
from coordinator_core.ops.fleet._common import main_worktree_root

CURATION_STATUS_REL_PATH = "state/ceremony/curation-status.json"
"""The single fixed emit target (DR-228 § D6-i) — not run-scoped, since this is a
point-in-time derived snapshot recomputed fresh on every call, not a per-run artifact."""

DISTILLATION_LOG_REL_PATH = "state/distillation-log.md"
"""The canonical distillation-log path (same default `compute_curation_status` itself
resolves to) — the freshness source of truth for `last_run_id`/`last_run_age_seconds`
(2026-07-23 the Staff Engineer post-execution review § 3; see `_last_distill_run`)."""


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.timezone.utc)


# Canonical distill run-id forms, most specific first. Both are minted by this
# subsystem: `YYYY-MM-DD-HHhMMmSSs` (distill.curation_status's own default) and
# `YYYY-MM-DD-HHhMM` (distill.scope / the ceremony run-ids in the canonical log).
_RUN_ID_FORMATS = ("%Y-%m-%d-%Hh%Mm%Ss", "%Y-%m-%d-%Hh%M")


def _run_id_to_utc(run_id: str) -> Optional[_dt.datetime]:
    """Parse a distill run-id into its UTC datetime, or None if unparseable.

    The run-id IS the timestamp — `2026-07-22-23h38` — so it is content-borne truth:
    it travels with the log's bytes, survives a clone, and cannot be perturbed by any
    writer that does not actually add a run.

    Deliberately NOT the log file's mtime, which was the first cut of this and is
    wrong in the one direction that matters: git does not preserve mtime, so on a
    fresh clone (or any checkout/rewrite) mtime becomes checkout time and the derived
    age collapses toward zero — reporting "we just distilled" when the last real run
    may be months old. `log_normalize` rewriting the file in place would do the same.
    For the cadence trigger M1 points at this field, a falsely-fresh age means the
    ceremony silently never fires and nothing complains — the exact silent-staleness
    failure the DEC-1 self-reference fix existed to close (the Staff Engineer post-execution
    review § 3), which mtime would have reintroduced through a different door.

    Run-ids carry no timezone; they are minted UTC by this subsystem and are read
    back as UTC here. Unparseable ids fail SOFT to None ("age unknown") rather than
    to a fabricated or zero age — a confidently wrong freshness reading is strictly
    worse than an absent one for a cadence consumer.
    """
    for fmt in _RUN_ID_FORMATS:
        try:
            return _dt.datetime.strptime(run_id, fmt).replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            continue
    return None


def _last_distill_run(worktree_root: Path) -> tuple[Optional[str], Optional[_dt.datetime]]:
    """Derive (last_run_id, last_run_at) from the canonical distillation log
    (state/distillation-log.md) — real disk truth, with NO self-reference to this op's
    own prior emission.

    Prior to 2026-07-23 this read `run_id`/`generated_at` back from the previously
    emitted `curation-status.json` itself — the ledger's own prior output was its
    input, which is precisely the seam through which "derived, never stored" (DEC-1)
    collapses into "stored": skip an emission and the age silently goes null; hand-edit
    or delete the prior emission and the age lies. The canonical log has neither
    failure mode — it is the actual record of "when did we last distill", and nothing
    about reading it depends on this op having ever run before.

    `last_run_id` is the last parsed row's `run_id` (rows are append-ordered, so the
    last row belongs to the most recent `## Run <id>` block); `last_run_at` is that
    run_id parsed as a timestamp via `_run_id_to_utc` — see there for why the id's own
    bytes, not the file's mtime, are the correct source.

    Returns (None, None) when the log is absent, unreadable, or has no parseable rows
    — "no distill run has ever landed" is a legitimate state, never an error. Returns
    `(run_id, None)` when the id itself will not parse: the run is known, its age is
    not, and a consumer must be able to tell those apart.
    """
    log_path = worktree_root / DISTILLATION_LOG_REL_PATH
    if not log_path.is_file():
        return None, None
    try:
        text = log_path.read_text(encoding="utf-8")
    except OSError:
        return None, None
    rows = parse_distillation_log(text)
    if not rows:
        return None, None
    last_run_id = rows[-1].run_id
    return last_run_id, _run_id_to_utc(last_run_id)


@register_op("distill.curation_status")
async def _distill_curation_status(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "distill.curation_status" handler.

    Params (all optional):
        run_id (str)  — identifier stamped into the computed artifact; defaults to
                         a UTC-timestamp-derived id (YYYY-MM-DD-HHhMMmSSs) when absent.
        emit   (bool) — when truthy, additionally writes the schema_version-pinned
                         JSON artifact to state/ceremony/curation-status.json
                         (DR-228 § D6). Defaults to False — a bare compute-and-return
                         call performs no write, which is what keeps this op usable
                         from a read-only diagnostic context without tripping the
                         MUTATING classification's write-confinement in practice
                         (the classification itself is still MUTATING unconditionally,
                         per DR-208's per-op — not per-call — classification granularity).

    repo_root (injected by ipc.dispatch_message): git_common_dir of the originating
    worktree; the handler derives the main-worktree root via main_worktree_root
    (a linked worktree's .git is a file, so the raw common_dir must not be used
    directly as the artifact-tree root — same seam artifact_emit.py uses).

    Returns the computed curation-status dict (schema_version-pinned via
    manifest_schema.make_curation_status), plus "emitted": bool and, when emitted,
    "out" (the absolute path written).
    """
    if repo_root is None:
        raise ValueError(
            "distill.curation_status requires a per-repo dispatch key "
            "(_origin_worktree); repo_root is None — op scope must be 'common_dir' "
            "and _origin_worktree must be present in the JSON-RPC envelope."
        )
    worktree_root = main_worktree_root(repo_root)

    now = _now_utc()
    # Exempt from the "run-id is a required param, never datetime.now()"
    # resume-safety rule `distill.scope` / `memo.fate_partition` both enforce
    # (2026-07-23 code review finding, resolved as a named exemption rather
    # than matching siblings' required-param posture): those two ops use
    # run_id as a WRITE-PATH KEY — it selects which
    # state/scratch/artifact-distillation/<run-id>/ directory a Workflow
    # resumes into, so minting one from wallclock would silently orphan a
    # resumed run's own artifacts. This op's target path is fixed
    # (CURATION_STATUS_REL_PATH, D6-i) regardless of run_id — run_id here is
    # pure metadata stamped INTO the one artifact, never used to locate
    # anything, so a wallclock default carries none of the resumption hazard
    # the rule guards against. See test_distill_curation_status_default_run_id_branch.
    run_id = params.get("run_id") or now.strftime("%Y-%m-%d-%Hh%Mm%Ss")
    emit = bool(params.get("emit", False))

    result = compute_curation_status(worktree_root)

    last_run_id, last_run_at = _last_distill_run(worktree_root)
    last_run_age_seconds: Optional[float] = None
    if last_run_at is not None:
        last_run_age_seconds = (now - last_run_at).total_seconds()

    artifact = manifest_schema.make_curation_status(
        run_id=run_id,
        generated_at=now.replace(microsecond=0).isoformat(),
        artifacts={path: entry.to_dict() for path, entry in result.artifacts.items()},
        unharvested_ripe_count=result.unharvested_ripe_count,
        last_run_id=last_run_id,
        last_run_age_seconds=last_run_age_seconds,
        prunable=result.prunable,
    )

    errors = manifest_schema.validate_curation_status(artifact)
    if errors:
        raise ValueError(f"distill.curation_status: computed artifact failed schema validation: {errors}")

    reply = dict(artifact)
    reply["emitted"] = False

    if emit:
        out_path = worktree_root / CURATION_STATUS_REL_PATH
        serialized = json.dumps(artifact, indent=2, ensure_ascii=False) + "\n"
        locked_rmw(out_path, lambda _old: serialized, repo_root=worktree_root, missing_ok=True)
        reply["emitted"] = True
        reply["out"] = str(out_path)

    return reply

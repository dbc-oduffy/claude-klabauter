"""
coordinator_core.ops.distill_disposal_manifest — JSON-RPC
"distill.assemble_disposal_manifest" operation (Phase 3d — C12).

Purpose: one call that turns a caller-supplied candidate cohort (the union of
scope-output cohorts and any specialist/LLM disposition, e.g. a reality-check
scout's EPHEMERAL verdict) into a C9-schema disposal-manifest: an eligible set
carrying FULL per-guard receipts, a retained set carrying the blocking guard(s)
plus a human retention_reason, pre-rendered (validate-only, never appended)
canonical-log rows, and a manifest-level scan_stats + mass-throttle flag (F2).

Guard composition (DEC-5 — thin orchestrator, no guard logic migrates up):
this module calls ``coordinator_core.distill.delete_guard.evaluate_candidate_detailed``
— the SAME class-keyed dispatch-order entry point ``evaluate_candidate`` itself
consumes (2026-07-23 architecture review § 2a fix) — for guard SELECTION and
ordering, and reuses that module's ``_cites_only_memory_pointer`` #12 exclusion
helper directly (a private-name import, not a reimplementation) rather than
re-deriving the memory-pointer rule inline. This module no longer reimplements
the dispatch order itself: prior to the § 2a fix, ``evaluate_candidate``'s
public return shape collapsed every guard's outcome to a bare ``blocked_by``
name list, discarding each PASSING guard's verdict and evidence — exactly the
per-guard receipt AC3 requires ("no bare `eligible: true` without receipts") —
so this module carried its own copy of the dispatch order to get at the full
per-guard results. ``evaluate_candidate_detailed`` now returns that full
``list[GuardResult]`` directly, so only the receipt-shaping (turning each
``GuardResult`` into a schema receipt dict) is this module's own concern; the
guard SELECTION and ordering logic is single-sourced in delete_guard.py, with
no duplicate authority left for distill.apply_disposal's act-time TOCTOU
re-verify to diverge from.

A candidate that resolves to no artifact class (``classify_artifact`` -> None —
e.g. a sidecar or a bare spec file, neither a handoff nor a memo shape) is,
correctly, always retained: delete_guard's own class-unresolved fail-closed
default applies unchanged. Sidecars are pre-routed by C10's ``sidecar_sweep``
cohort, not this op; a caller that hands this op a sidecar path gets a retained
row with reason ``artifact-class-unresolved``, which is the fail-closed-to-keep
posture (DR-228 § D2a-v), not a defect.

Write shape (D3 — "assemble_disposal_manifest ... do[es] not commit at all; ...
writes are scratch-tier-shaped (create, then in-place stamp) inside
state/scratch/artifact-distillation/<run-id>/, landed on disk only"): this
handler performs exactly one create-or-full-rewrite JSON emit
(``disposal-manifest.json``) into its own run-id's scratch subdirectory, never
deletes, never commits. MUTATING (DR-208 fail-closed: any file write disqualifies
COMPUTE_ONLY) — sanctioned as one of DR-228 § D1's three disposal-tier ops; see
``coordinator_core/authz/classification.py``'s DR-208 five-question affirmation
block for this op.

Grouping (>50 rows -> ``deletion_groups``, PIPELINE self-check parity): C9's
landed ``manifest_schema.py`` does not define a ``deletion_groups`` field —
that schema module is also outside this chunk's named surface. Rather than
add an undeclared REQUIRED field, this module adds ``deletion_groups`` as an
ADDITIVE top-level key (``validate_disposal_manifest`` only checks its
``_DISPOSAL_REQUIRED`` set is present; it does not reject unknown extra keys,
the same additive-extension posture the plan cites for C4/ripe_filter's
``to_dict``) — present only when eligible-row count exceeds the module's named
``GROUP_THRESHOLD`` constant, absent otherwise (graceful-absent).

Negative-spec: performs no LLM call, no PM disposal decision (the stamp is
C13's job, written only on explicit PM-approval-time params), no delete, no
commit, no git write of any kind — this op stops at guard-evaluation +
manifest-assembly (plan § Negative spec, AC12).

Spec backlink: pln-claude-klabauter-driven-ceremony-redesig-c7fe9a § C12
Governing DR: docs/decisions/DR-228-distill-disposal-substrate-writer-category.md § D1, D3
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coordinator_core.distill import log_append as _log_append
from coordinator_core.distill import manifest_render as _render
from coordinator_core.distill import manifest_schema as _schema
from coordinator_core.distill.delete_guard import (
    _cites_only_memory_pointer,
    evaluate_candidate_detailed,
)
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.wire_paths import rel_id

__all__ = [
    "MASS_THROTTLE_RATIO",
    "MASS_THROTTLE_ABSOLUTE",
    "MASS_THROTTLE_HARD_CAP",
    "GROUP_THRESHOLD",
    "DisposalManifestResult",
    "evaluate_candidate_receipts",
    "compute_disposal_manifest",
    "write_disposal_manifest",
]

MASS_THROTTLE_RATIO: float = 0.5

#: Named module-constant ABSOLUTE mass-throttle floor (2026-07-23 architecture
#: MASS_THROTTLE_RATIO at any corpus size where the bug doesn't also cross
MASS_THROTTLE_ABSOLUTE: int = 25

#: MASS_THROTTLE_RATIO/MASS_THROTTLE_ABSOLUTE are a SOFT band — a stamp note
#: carrying MASS_THROTTLE_ACK_MARKER authorizes an arbitrarily large batch
MASS_THROTTLE_HARD_CAP: int = 200

#: ``deletion_groups`` chunks (PIPELINE self-check parity — mirrors C10's
GROUP_THRESHOLD: int = 50


def evaluate_candidate_receipts(
    path: Path, repo_root: Path, basis_refs: tuple[str, ...] = ()
) -> dict[str, Any]:
    try:
        needle = rel_id(path, repo_root)
    except ValueError:
        needle = path.name

    artifact_class, guard_results = evaluate_candidate_detailed(path, repo_root)

    blocked_by = [r.guard for r in guard_results if not r.passed]
    if artifact_class is None:
        blocked_by.append("artifact-class-unresolved")
    if _cites_only_memory_pointer(basis_refs):
        blocked_by.append("memory-pointer-exclusion")

    guards_run = [
        _schema.make_guard_receipt(r.guard, "pass" if r.passed else "block", r.detail)
        for r in guard_results
    ]

    return {
        "path": needle,
        "artifact_class": artifact_class or "unresolved",
        "eligible": len(blocked_by) == 0,
        "blocked_by": blocked_by,
        "guards_run": guards_run,
    }


@dataclass(frozen=True)
class DisposalManifestResult:

    manifest: dict[str, Any]
    counts: dict[str, int]


def _normalize_candidate(entry: Any) -> tuple[str, tuple[str, ...]]:
    if isinstance(entry, str):
        return entry, ()
    if isinstance(entry, dict):
        rel_path = entry.get("path")
        if not rel_path:
            raise ValueError("candidate dict missing required 'path' key")
        basis_refs = tuple(entry.get("basis_refs", ()) or ())
        return rel_path, basis_refs
    raise ValueError(f"candidate must be a str or dict; got {type(entry).__name__}")


def compute_disposal_manifest(
    worktree_root: Path,
    *,
    run_id: str,
    candidates: list[Any],
    mass_throttle_ratio: float = MASS_THROTTLE_RATIO,
    mass_throttle_absolute: int = MASS_THROTTLE_ABSOLUTE,
    group_threshold: int = GROUP_THRESHOLD,
) -> DisposalManifestResult:
    rows: list[dict[str, Any]] = []
    eligible_paths: list[str] = []

    for entry in candidates:
        rel_path, basis_refs = _normalize_candidate(entry)
        abs_path = worktree_root / rel_path

        if not abs_path.exists():
            rows.append(
                _schema.make_disposal_row(
                    path=rel_path,
                    artifact_class="unresolved",
                    guards_run=[
                        _schema.make_guard_receipt(
                            "path-exists", "block", "candidate path absent on disk"
                        )
                    ],
                    eligible=False,
                    retention_reason="candidate path absent on disk at assemble time",
                    log_row="",
                )
            )
            continue

        receipt = evaluate_candidate_receipts(abs_path, worktree_root, basis_refs)

        log_row = ""
        row_kwargs: dict[str, Any] = {
            "path": receipt["path"],
            "artifact_class": receipt["artifact_class"],
            "guards_run": receipt["guards_run"],
            "eligible": receipt["eligible"],
        }
        if receipt["eligible"]:
            log_row = _log_append.render_row(
                receipt["path"],
                "EPHEMERAL",
                f"disposed ({receipt['artifact_class']})",
                run_id,
            )
            eligible_paths.append(receipt["path"])
        else:
            row_kwargs["retention_reason"] = "blocked by: " + ", ".join(
                receipt["blocked_by"]
            )
        row_kwargs["log_row"] = log_row
        rows.append(_schema.make_disposal_row(**row_kwargs))

    total_scanned = len(rows)
    eligible_count = len(eligible_paths)
    retained_count = total_scanned - eligible_count

    mass_throttle = eligible_count > mass_throttle_absolute or (
        total_scanned > 0 and (eligible_count / total_scanned) > mass_throttle_ratio
    )

    manifest = _schema.make_disposal_manifest(
        run_id=run_id,
        rows=rows,
        scan_stats=_schema.make_scan_stats(total_scanned, eligible_count, retained_count),
        mass_throttle=mass_throttle,
    )

    if total_scanned > group_threshold and eligible_paths:
        manifest["deletion_groups"] = [
            eligible_paths[i : i + group_threshold]
            for i in range(0, len(eligible_paths), group_threshold)
        ]

    counts = {
        "total_scanned": total_scanned,
        "eligible_count": eligible_count,
        "retained_count": retained_count,
    }
    return DisposalManifestResult(manifest=manifest, counts=counts)


def write_disposal_manifest(worktree_root: Path, manifest: dict[str, Any]) -> Path:
    run_dir = worktree_root / "state" / "scratch" / "artifact-distillation" / manifest["run_id"]
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / "disposal-manifest.json"

    body = json.dumps(manifest, indent=2).encode("utf-8")
    fd, tmp_path = tempfile.mkstemp(dir=str(run_dir), suffix=".tmp")
    try:
        try:
            os.write(fd, body)
        finally:
            os.close(fd)
        os.replace(tmp_path, str(target))
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return target


@register_op("distill.assemble_disposal_manifest")
def _handler(params: dict, repo_root: Path | None = None) -> dict:
    """distill.assemble_disposal_manifest handler.

    Params:
        run_id (str, REQUIRED) — the Workflow-minted run id (or any caller-supplied
            stable identifier); never minted from wallclock inside this handler.
        candidates (list, REQUIRED) — each entry a worktree-root-relative path
            string, or a {"path": str, "basis_refs": list[str]} dict.
        mass_throttle_ratio (float, optional) — override MASS_THROTTLE_RATIO.
        mass_throttle_absolute (int, optional) — override MASS_THROTTLE_ABSOLUTE.
        group_threshold (int, optional) — override GROUP_THRESHOLD.

    repo_root (injected by ipc.dispatch_message): git_common_dir of the originating
    worktree (_OP_KEY_SCOPE="common_dir"). Derives main_worktree_root(repo_root).
    Fails loud when repo_root is None (no silent meta-repo fallback, matching
    distill.scope's / artifact.emit's AC5 precedent).
    """
    if repo_root is None:
        raise ValueError(
            "distill.assemble_disposal_manifest requires a per-repo dispatch key "
            "(_origin_worktree); repo_root is None — op scope must be 'common_dir' "
            "and _origin_worktree must be present in the JSON-RPC envelope. No "
            "silent fallback to meta-repo."
        )
    run_id = params.get("run_id")
    if not run_id:
        raise ValueError(
            "distill.assemble_disposal_manifest requires an explicit run_id param — "
            "never minted from wallclock inside the handler."
        )
    candidates = params.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError(
            "distill.assemble_disposal_manifest requires a 'candidates' list param."
        )

    worktree_root = main_worktree_root(repo_root)
    kwargs: dict[str, Any] = {"run_id": run_id, "candidates": candidates}
    if "mass_throttle_ratio" in params:
        kwargs["mass_throttle_ratio"] = params["mass_throttle_ratio"]
    if "mass_throttle_absolute" in params:
        kwargs["mass_throttle_absolute"] = params["mass_throttle_absolute"]
    if "group_threshold" in params:
        kwargs["group_threshold"] = params["group_threshold"]

    result = compute_disposal_manifest(worktree_root, **kwargs)
    written_path = write_disposal_manifest(worktree_root, result.manifest)

    return {
        "run_id": run_id,
        "disposal_manifest_path": rel_id(written_path, worktree_root),
        "counts": result.counts,
        "mass_throttle": result.manifest["mass_throttle"],
        "rendered_manifest": _render.render_disposal_manifest(result.manifest),
    }

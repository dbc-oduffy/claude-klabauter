"""
coordinator_core.ops.memo_fate_partition — JSON-RPC "memo.fate_partition" operation.

Purpose: mechanical prefilter over the actioned-memo corpus (``cross-repo/archive/*.md``)
that partitions every memo into exactly one of three buckets:

  - ``prefiled``        — ``distill_fate`` is present and, for ``ratification``, the
                           ``in_repo_capture`` target is well-formed AND exists on disk.
                           Mechanically dispositioned; no LLM read required.
  - ``residue``          — ``distill_fate`` absent (unstamped legacy memo) or an unknown
                           enum value. Routed to the LLM specialist wave for a real read.
  - ``capture_missing``  — ``distill_fate == "ratification"`` but ``in_repo_capture`` is
                           malformed (schema_validate's well-formed-path rule) or points at
                           a target absent from disk. Surfaced, NEVER silently prefiled —
                           a ratification stamp whose claimed capture doesn't exist is a
                           correctness gap, not a disposition.

This op is MUTATING (DR-208 default; DR-228 scratch-tier admission, D6): it writes exactly
one JSON shard, whole, to
``state/scratch/artifact-distillation/<run_id>/fate-partition.json`` and stops — no delete,
no in-place mutation of an existing file, no commit (DR-228 § D6(ii)-(iv)). ``run_id`` is a
caller-supplied path segment (never op-generated), validated via ``_path_guard.safe_id``
before being interpolated into the write path (DR-228 § D6(i) write-confinement — the shard
is written ONLY under its own run-id subdirectory, never a sibling run's).

Well-formed-path reuse (F1): the ratification/in_repo_capture format check is NOT
reimplemented here — it composes
``coordinator_core.frontmatter.schema_validate._memo_cf_distill_fate``, the same rule
``schema.validate`` enforces on memo frontmatter, so the two surfaces can never silently
diverge on what counts as a well-formed capture pointer. This module adds exactly one thing
schema_validate does not check: on-disk EXISTENCE of a well-formed capture target (format
validity alone does not mean the file is actually there).

Shard-row/log_row alignment (C9): a ``prefiled`` row that resolves to a known disposition
carries a pre-rendered ``log_row`` string via ``coordinator_core.distill.log_append.render_row``
(validate-only — this op never appends to the canonical log itself, per DEC-5: ops compose
the library, the library stays pure) so a downstream log-append pass can consume the row
mechanically rather than re-deriving fate->disposition. Fate->disposition mapping:
``ratification`` -> ``DISTILLED`` (already captured durably), ``commitment`` -> ``PRESERVE``
(open commitment, not yet actionable), ``ephemeral`` -> ``EPHEMERAL``.

Self-registration: importing this module calls register_op("memo.fate_partition", _handler)
as a side-effect (same pattern as ops/memo_triage.py). This module IS imported by
coordinator_core/ops/__init__.py's _EAGER_OP_MODULES and memo.fate_partition IS wired into
authz/classification.py's OP_CLASSIFICATION (MUTATING) and op_scopes.py's _OP_KEY_SCOPE
("common_dir") — an op absent from either silently misbehaves (registry-drift lesson
2026-07-06-compute-only-op-registration-needs-an-op generalizes to any missing seam, not
just the COMPUTE_ONLY case it was written for).

Negative-spec:
  - Does NOT call any LLM — deterministic partition only.
  - Does NOT judge whether a ``residue`` memo's eventual fate SHOULD be ratification/
    commitment/ephemeral — that judgment belongs to the LLM specialist wave this op's
    ``residue`` bucket feeds (memo.triage / DoE's C6 cascade), not this mechanical prefilter.
  - Does NOT delete, mutate-in-place, or commit anything — one whole-JSON create-or-
    overwrite of its own run-scoped shard, full stop (DR-228 § D6).
  - Does NOT write outside ``state/scratch/artifact-distillation/<run_id>/`` — a sibling
    run-id's directory, or any other ``state/`` path, is out of scope by construction
    (``safe_id`` gate on ``run_id`` before path interpolation).
  - Does NOT re-derive the ratification/in_repo_capture format rule — composes
    ``schema_validate._memo_cf_distill_fate`` (F1 reuse) rather than re-implementing it.

Spec backlink: pln-claude-klabauter-driven-ceremony-redesig-c7fe9a § C16
Authority:     docs/decisions/DR-228-distill-disposal-substrate-writer-category.md § D6
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

import yaml

from coordinator_core.distill.log_append import render_row
from coordinator_core.distill.manifest_schema import SCHEMA_VERSION
from coordinator_core.frontmatter.schema_validate import _memo_cf_distill_fate
from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path, safe_id

__all__ = [
    "partition_memos",
    "collect_memo_records",
]

# writes only state/scratch/artifact-distillation/<run_id>/fate-partition.json,
# which matches the repo's own .gitignore "scratch/" pattern (untracked)
GENERATES = []

# fate -> log_append disposition, for the "prefiled" bucket's pre-rendered log_row
# (C9 alignment — see module docstring). Only fates with a well-defined disposition
# get a log_row; an unrecognized fate never reaches this map (routed to residue).
_FATE_DISPOSITION = {
    "ratification": "DISTILLED",
    "commitment": "PRESERVE",
    "ephemeral": "EPHEMERAL",
}


def _parse_frontmatter(raw: str) -> Optional[dict]:
    """Extract + parse the YAML frontmatter block of a memo file.

    Returns None (quarantine) on missing/malformed frontmatter — mirrors the
    quarantine convention in memo_triage.py / plan_match.py / goals_match.py.
    """
    if not raw.startswith("---\n"):
        return None
    parts = raw.split("---\n", 2)
    if len(parts) < 2:
        return None
    try:
        fm = yaml.safe_load(parts[1])
    except Exception:  # noqa: BLE001 — quarantine parity with sibling ops
        return None
    return fm if isinstance(fm, dict) else None


def collect_memo_records(archive_dir: Path, worktree_root: Path) -> Tuple[List[dict], bool]:
    """Enumerate cross-repo/archive/*.md as parsed memo records.

    Each record: {memo_id, path (rel-posix to worktree_root), fm}. Files with
    missing/malformed frontmatter are quarantined (skipped).

    Returns `(records, degraded)`. `degraded` is True ONLY when `archive_dir`
    itself could not be listed — uses `os.scandir()`, NOT `Path.glob("*.md")`,
    which silently swallows `PermissionError` while walking (same rationale as
    memo_triage.py's `_collect_memo_records`). An empty-but-degraded record
    list must never be read as "genuinely no memos to partition".
    """
    records: List[dict] = []
    if not archive_dir.is_dir():
        return records, False

    try:
        entries = sorted(os.scandir(archive_dir), key=lambda e: e.name)
    except OSError as exc:
        print(
            f"memo.fate_partition: cannot scan {archive_dir} — {exc}; memo corpus "
            "is degraded (NOT the same as \"no memos to partition\")",
            file=sys.stderr,
        )
        return records, True

    for entry in entries:
        if not entry.name.endswith(".md"):
            continue
        fpath = Path(entry.path)
        try:
            raw = fpath.read_text(encoding="utf-8").replace("\r\n", "\n")
        except OSError as exc:
            print(f"memo.fate_partition: skipping {fpath.name} — read error: {exc}", file=sys.stderr)
            continue

        fm = _parse_frontmatter(raw)
        if fm is None:
            print(
                f"memo.fate_partition: skipping {fpath.name} — missing/malformed frontmatter",
                file=sys.stderr,
            )
            continue

        try:
            rel_path = fpath.resolve().relative_to(worktree_root.resolve()).as_posix()
        except ValueError:
            # fpath is not under worktree_root (e.g. an archive_dir override
            # pointing outside the tree, test isolation) — fall back to the
            # file's own posix path so the row still carries a forward-slash
            # needle (Windows portability — never a backslash path in JSON).
            rel_path = fpath.as_posix()

        records.append({"memo_id": fpath.stem, "path": rel_path, "fm": fm})

    return records, False


def _capture_target_exists(worktree_root: Path, capture: str) -> bool:
    """True iff a well-formed in_repo_capture path exists on disk under worktree_root.

    Format validity (schema_validate's well-formed-path rule) says nothing about
    on-disk existence — this is the one check this op adds beyond that rule.

    Containment (2026-07-23 code review, F2): ``_memo_cf_distill_fate``'s
    "well-formed" check is a literal string-prefix test (``capture.startswith(
    "docs/decisions/")`` etc.) — it does NOT normalize ``..`` segments, so a
    capture value like ``"docs/decisions/../../../etc/passwd"`` passes format
    validity while still escaping ``worktree_root`` once resolved (confirmed
    reachable on the well-formed branch; a bare absolute path like
    ``"/etc/passwd"`` is separately rejected by that same prefix test, since it
    never starts with an allowed prefix — that part of the original pathlib-
    discards-absolute-left-operand concern does NOT reproduce). Guarded here via
    ``_path_guard.contained_path`` rather than trusting the prefix check alone.
    """
    candidate = worktree_root / capture.strip()
    resolved = contained_path(candidate, [worktree_root])
    if resolved is None:
        return False
    return resolved.exists()


def partition_memos(
    records: Iterable[dict],
    *,
    worktree_root: Path,
    run_id: str,
    degraded: bool = False,
) -> dict:
    """Pure classification core — given parsed memo records, returns the
    fate-partition outcome.

    Args:
        degraded: True when the caller's I/O layer could not fully scan the
            memo corpus (e.g. a permission-denied archive_dir) — surfaced
            verbatim so callers do NOT mistake an empty/incomplete scan for a
            genuinely clean partition.

    Returns:
        {
            "schema_version": int,
            "run_id": str,
            "prefiled": [{"memo_id", "path", "distill_fate", "in_repo_capture",
                          "log_row"}, ...],
            "residue": [{"memo_id", "path", "distill_fate", "in_repo_capture"}, ...],
            "capture_missing": [{"memo_id", "path", "distill_fate",
                                  "in_repo_capture", "reason"}, ...],
            "counts": {"total": int, "prefiled": int, "residue": int,
                       "capture_missing": int},
            "degraded": bool,
        }

    Invariant (AC6): counts["prefiled"] + counts["residue"] +
    counts["capture_missing"] == counts["total"] == len(list(records)), always —
    every memo lands in exactly one bucket, never zero, never two.
    """
    prefiled: List[dict] = []
    residue: List[dict] = []
    capture_missing: List[dict] = []

    for rec in records:
        fm = rec["fm"]
        fate = fm.get("distill_fate")
        capture = fm.get("in_repo_capture")
        base = {
            "memo_id": rec["memo_id"],
            "path": rec["path"],
            "distill_fate": fate,
            "in_repo_capture": capture,
        }

        if fate is None:
            residue.append(base)
            continue

        error = _memo_cf_distill_fate(fm)
        if error is not None and error.get("field") == "distill_fate":
            # Unknown/invalid distill_fate enum value — not a trusted signal;
            # route to residue for an LLM read rather than guessing.
            residue.append(base)
            continue

        if fate not in _FATE_DISPOSITION:
            # Known-valid-per-schema but this module's disposition map doesn't
            # (yet) cover it — same conservative routing as an invalid enum.
            residue.append(base)
            continue

        if fate != "ratification":
            row = dict(base)
            row["log_row"] = render_row(rec["path"], _FATE_DISPOSITION[fate], fate, run_id)
            prefiled.append(row)
            continue

        # ratification: format-checked above (error is None here); still need
        # the on-disk existence check schema_validate's rule does not perform.
        if error is not None:
            capture_missing.append({**base, "reason": error["error"]})
            continue
        if not isinstance(capture, str) or not _capture_target_exists(worktree_root, capture):
            capture_missing.append(
                {**base, "reason": f"in_repo_capture target absent on disk: {capture!r}"}
            )
            continue

        row = dict(base)
        row["log_row"] = render_row(rec["path"], _FATE_DISPOSITION[fate], fate, run_id)
        prefiled.append(row)

    total = len(prefiled) + len(residue) + len(capture_missing)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "prefiled": prefiled,
        "residue": residue,
        "capture_missing": capture_missing,
        "counts": {
            "total": total,
            "prefiled": len(prefiled),
            "residue": len(residue),
            "capture_missing": len(capture_missing),
        },
        "degraded": degraded,
    }


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Full-rebuild atomic write: mkstemp + os.replace (DR-228 § D6(ii) —
    create-or-full-rewrite only, never partial in-place mutation).

    ``os.replace`` is atomic-on-same-filesystem on Windows too (unlike a bare
    ``os.rename`` there) — same convention as
    coordinator_core/ops/session_hierarchy_derive.py's `_atomic_write_json`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@register_op("memo.fate_partition")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "memo.fate_partition" handler.

    Params:
        run_id (str, required): caller-supplied run identifier — a safe single
            path segment (coordinator_core.ops._path_guard.safe_id), NEVER
            op-generated. Confines the shard write to
            state/scratch/artifact-distillation/<run_id>/fate-partition.json
            (DR-228 § D6(i)).
        archive_dir (str, optional): override for cross-repo/archive/ — test
            isolation, mirrors memo_triage.py's convention.

    Returns: the ``partition_memos`` outcome dict (see docstring above), with
        an additional ``"shard_path"`` key (rel-posix to worktree_root) naming
        where the shard JSON was written.

    Worktree resolution mirrors memo_triage.py / plan_match.py / goals_match.py:
      - repo_root (router-supplied git common dir) → main_worktree_root(repo_root)
      - None → structured error (no silent meta-repo fallback; this is a
        MUTATING op — a resolved write target is mandatory, not best-effort).
    """
    from coordinator_core.ops.fleet._common import main_worktree_root

    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("memo.fate_partition requires a non-empty string param: run_id")
    if not safe_id(run_id):
        raise ValueError(
            f"memo.fate_partition: run_id is not a safe path segment: {run_id!r}"
        )

    if repo_root is None:
        raise ValueError(
            "memo.fate_partition requires a per-repo dispatch key (_origin_worktree); "
            "repo_root is None — op scope must be 'common_dir'. No silent fallback to "
            "meta-repo."
        )

    worktree_root = main_worktree_root(repo_root)

    archive_override = params.get("archive_dir")
    archive_dir = (
        Path(archive_override)
        if isinstance(archive_override, str) and archive_override
        else worktree_root / "cross-repo" / "archive"
    )

    records, degraded = collect_memo_records(archive_dir, worktree_root)
    outcome = partition_memos(
        records, worktree_root=worktree_root, run_id=run_id, degraded=degraded
    )

    shard_dir = worktree_root / "state" / "scratch" / "artifact-distillation" / run_id
    shard_path = shard_dir / "fate-partition.json"
    _atomic_write_json(shard_path, outcome)

    result: dict[str, Any] = dict(outcome)
    result["shard_path"] = shard_path.resolve().relative_to(worktree_root.resolve()).as_posix()
    return result

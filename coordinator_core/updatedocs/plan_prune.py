"""
coordinator_core.updatedocs.plan_prune — plans ripeness-safety composite predicate.

Purpose: pure, read-only compute backing the audit's B1 row
(`artifact-pruning.md` § Step 1 "Plans" ripeness-safety guard). Emits
prune CANDIDATES with per-leg evidence; deletes nothing, constructs no
`GateResult` (the gate layer at `coordinator_core.ops.updatedocs_gates`
owns verdict mapping) and registers no op.

Three legs, ALL must hold for `prunable`:
  1. age         -- mtime older than `age_days` (a parameter, default 14)
  2. status      -- frontmatter `status:` is one of TERMINAL_PLAN_STATUSES
  3. unreferenced -- plan's basename/plan_id not found in REFERENCE_FIELDS
                     across `state/handoffs/` and `tasks/`

THREE-STATE, not boolean. A plan with no `status:` key at all is
`indeterminate` -- never `prunable` -- regardless of the other two legs.
Measured on the real corpus (2026-09-02): 358 plans, 6 terminal-status,
38 with no `status:` key. Collapsing indeterminate into either other
bucket is the exact failure this row exists to prevent.

Negative spec: the "unreferenced" leg is a BOUNDED grep over the named,
closed `REFERENCE_FIELDS` set -- never a general ancestry/edge-traversal
query. `handoff.has_live_children` was deleted for exceeding the DR-344
200ms bar and must not return in the same shape under a new name. If this
leg starts needing edge traversal (e.g. following `blocked_by` chains),
stop and surface that rather than building it.

Spec backlink: pln-bucket-2-extraction-four-deter-e121fa (chunk C3)
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter

# Resolved from coordinator_core/frontmatter/schemas/plan.schema.json's
# `status` enum: {draft, reviewed, approved, executing, landed, implemented,
# deferred, abandoned, superseded}. That schema's own description is explicit
# that "landed" sits between "executing" and "implemented" and is NOT
# terminal (a landed plan still has outstanding row-level work). The
# remaining four values are the ones a plan cannot move on from.
TERMINAL_PLAN_STATUSES = frozenset({"implemented", "deferred", "abandoned", "superseded"})

DEFAULT_AGE_FLOOR_DAYS = 14

# Closed set of frontmatter fields searched for a live reference to a plan,
# across state/handoffs/*.md and tasks/**/*.md. Do not grow this set into a
# general corpus-wide text search -- see module negative spec above.
#   - governing_plan  -- stores the referencing doc's `docs/plans/<file>.md` path
#   - origin_plan_id  -- stores the plan's own `plan_id:` frontmatter value
class PlanPruneTargetMissing(Exception):
    """Raised when `docs/plans/` does not exist.

    Carries `missing_path` so the gate layer can report exactly which path was
    absent. Sibling of `ReadmeIndexUnavailable` / `DirectoryMdUnavailable` /
    `MemoPruneTargetMissing`, and load-bearing for the same reason: an absent
    corpus must reach the caller as UNAVAILABLE, never as a CLEAN empty result.
    "We looked at nothing" and "we found nothing" are different answers, and
    collapsing them is the defect this package was built to remove.
    """

    def __init__(self, missing_path: Path):
        self.missing_path = missing_path
        super().__init__(f"required path does not exist: {missing_path}")


REFERENCE_FIELDS = ("governing_plan", "origin_plan_id")

# Bytes read from the head of each file when looking for its frontmatter
# block, and the growth ceiling applied when the closing `---` hasn't
# appeared yet (a long HTML-comment preamble can push the real frontmatter
# past the first chunk -- growing avoids misreading a genuine status as
# absent, which would wrongly inflate `indeterminate`).
_HEAD_READ_BYTES = 8192
_HEAD_READ_MAX_BYTES = 65536


@dataclass(frozen=True)
class PlanPruneCandidate:
    """One plan and the per-leg evidence behind its classification."""

    path: str
    age_days: float
    status: str | None
    is_terminal_status: bool
    referenced: bool
    reference_hits: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PlanPruneResult:
    prunable: list[PlanPruneCandidate]
    retained: list[PlanPruneCandidate]
    indeterminate: list[PlanPruneCandidate]


# A frontmatter delimiter line, standalone on its own line -- distinct from
# an incidental run of dashes inside a markdown table/rule that can appear
# well before the real closing delimiter and falsely look like "found it".
_FM_DELIMITER_LINE = re.compile(rb"(?m)^---[ \t]*\r?$")


def _read_head(p: Path) -> str:
    try:
        with p.open("rb") as fh:
            raw = fh.read(_HEAD_READ_BYTES)
            # Fewer than two standalone `---` lines means the closing
            # delimiter (or its preamble) is longer than the first chunk --
            # grow once, bounded, rather than misreading a real block as
            # absent.
            if len(_FM_DELIMITER_LINE.findall(raw)) < 2 and len(raw) == _HEAD_READ_BYTES:
                fh.seek(0)
                raw = fh.read(_HEAD_READ_MAX_BYTES)
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")


def _plan_status_and_id(p: Path) -> tuple[str | None, str | None]:
    text = _read_head(p)
    split = split_frontmatter(text)
    if split is None:
        return None, None
    status = read_fm_field(split.fm_text, "status")
    plan_id = read_fm_field(split.fm_text, "plan_id")
    status = status.strip('"\'') if status else status
    plan_id = plan_id.strip('"\'') if plan_id else plan_id
    return (status or None), (plan_id or None)


def _collect_reference_values(repo_root: Path) -> list[str]:
    """Return every value found under REFERENCE_FIELDS in the live corpus."""
    values: list[str] = []
    for dirname in ("state/handoffs", "tasks"):
        base = repo_root / dirname
        if not base.exists():
            continue
        for md_path in base.rglob("*.md"):
            text = _read_head(md_path)
            split = split_frontmatter(text)
            if split is None:
                continue
            for fname in REFERENCE_FIELDS:
                v = read_fm_field(split.fm_text, fname)
                if v:
                    values.append(v.strip('"\''))
    return values


def compute_plan_prune_candidates(
    repo_root: str | Path,
    age_days: float = DEFAULT_AGE_FLOOR_DAYS,
) -> PlanPruneResult:
    """Compute the three-state plan-prune classification over docs/plans/*.md.

    Pure and read-only: no writes, no deletion, no `GateResult` construction.
    """
    root = Path(repo_root)
    plans_dir = root / "docs" / "plans"

    prunable: list[PlanPruneCandidate] = []
    retained: list[PlanPruneCandidate] = []
    indeterminate: list[PlanPruneCandidate] = []

    if not plans_dir.is_dir():
        raise PlanPruneTargetMissing(plans_dir)

    reference_values = _collect_reference_values(root)
    now = time.time()

    for plan_path in sorted(plans_dir.glob("*.md")):
        try:
            mtime = plan_path.stat().st_mtime
        except OSError:
            continue
        age = (now - mtime) / 86400.0
        status, plan_id = _plan_status_and_id(plan_path)

        rel = plan_path.relative_to(root).as_posix()
        basename = plan_path.name

        hits = tuple(
            v
            for v in reference_values
            if (plan_id and v == plan_id) or v.endswith(basename) or v == rel
        )
        referenced = bool(hits)

        if status is None:
            indeterminate.append(
                PlanPruneCandidate(
                    path=rel,
                    age_days=age,
                    status=None,
                    is_terminal_status=False,
                    referenced=referenced,
                    reference_hits=hits,
                )
            )
            continue

        is_terminal = status in TERMINAL_PLAN_STATUSES
        candidate = PlanPruneCandidate(
            path=rel,
            age_days=age,
            status=status,
            is_terminal_status=is_terminal,
            referenced=referenced,
            reference_hits=hits,
        )

        if age >= age_days and is_terminal and not referenced:
            prunable.append(candidate)
        else:
            retained.append(candidate)

    return PlanPruneResult(
        prunable=sorted(prunable, key=lambda c: c.path),
        retained=sorted(retained, key=lambda c: c.path),
        indeterminate=sorted(indeterminate, key=lambda c: c.path),
    )

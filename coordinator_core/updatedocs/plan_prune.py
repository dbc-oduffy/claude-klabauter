"""
coordinator_core.updatedocs.plan_prune — plans ripeness-safety composite predicate.

Purpose: pure, read-only compute backing the audit's B1 row
(`artifact-pruning.md` § Step 1 "Plans" ripeness-safety guard). Emits three
`list[str]` path partitions; deletes nothing, constructs no `GateResult` (the
gate layer at `coordinator_core.ops.updatedocs_gates` owns verdict mapping)
and registers no op.

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

`prunable`/`retained`/`indeterminate` are sorted path lists -- no
per-candidate evidence object. Nothing outside this module's own
classification loop ever read a per-leg field independently of the bucket
it fed into; the gate layer emits paths and counts. Re-add a field the day
a consumer names it.

Spec backlink: pln-bucket-2-extraction-four-deter-e121fa (chunk C3)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from coordinator_core.frontmatter import primitives as _frontmatter_primitives
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.updatedocs import _common, _stamp_cache
from coordinator_core.updatedocs._common import UpdatedocsTargetMissing, read_head
from coordinator_core.updatedocs._stamp_cache import MISS, StampCache

TERMINAL_PLAN_STATUSES = frozenset({"implemented", "deferred", "abandoned", "superseded", "closed"})

DEFAULT_AGE_FLOOR_DAYS = 14

REFERENCE_FIELDS = ("governing_plan", "origin_plan_id")


@dataclass(frozen=True)
class PlanPruneResult:
    prunable: list[str] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)
    indeterminate: list[str] = field(default_factory=list)


def _plan_status_and_id(text: str) -> tuple[str | None, str | None]:
    split = split_frontmatter(text)
    if split is None:
        return None, None
    status = read_fm_field(split.fm_text, "status")
    plan_id = read_fm_field(split.fm_text, "plan_id")
    status = status.strip('"\'') if status else status
    plan_id = plan_id.strip('"\'') if plan_id else plan_id
    return (status or None), (plan_id or None)


def _reference_values_of(text: str) -> list[str]:
    split = split_frontmatter(text)
    if split is None:
        return []
    values: list[str] = []
    for fname in REFERENCE_FIELDS:
        v = read_fm_field(split.fm_text, fname)
        if v:
            values.append(v.strip("\"'"))
    return values


def _collect_reference_values(repo_root: Path, cache: StampCache) -> list[str]:
    """Return every value found under REFERENCE_FIELDS in the live corpus."""
    values: list[str] = []
    live: set[str] = set()
    for dirname in ("state/handoffs", "tasks"):
        base = repo_root / dirname
        if not base.exists():
            continue
        for path, stamp in _stamp_cache.walk_md(base):
            key = "r:" + path
            live.add(key)
            found = cache.get(key, stamp)
            if found is MISS:
                text = read_head(Path(path))
                found = _reference_values_of(text)
                # An empty head of a non-empty file is a failed read, not a verdict.
                if text or stamp[0] == 0:
                    cache.put(key, stamp, found)
            values.extend(found)
    cache.retain("r:", live)
    return values


def compute_plan_prune_candidates(
    repo_root: str | Path,
    age_days: float = DEFAULT_AGE_FLOOR_DAYS,
) -> PlanPruneResult:
    root = Path(repo_root)
    plans_dir = root / "docs" / "plans"

    prunable: list[str] = []
    retained: list[str] = []
    indeterminate: list[str] = []

    if not plans_dir.is_dir():
        raise UpdatedocsTargetMissing(plans_dir)

    cache = StampCache(
        root, "plan-prune",
        (__file__, _common.__file__, _stamp_cache.__file__, _frontmatter_primitives.__file__),
    )
    reference_values = _collect_reference_values(root, cache)
    now = time.time()

    try:
        listing = sorted(_stamp_cache.list_md(plans_dir))
    except OSError:
        listing = []
    rel_prefix = plans_dir.relative_to(root).as_posix() + "/"
    for name, stamp in listing:
        plan_path = plans_dir / name
        rel = rel_prefix + name
        if stamp is None:
            # Present at listing time, gone or unreadable by the stat. INDETERMINATE,
            # never a silent `continue`: the three lists must account for every
            # file the glob returned, or a caller reconciling totals finds a gap
            # with nothing explaining it. "We looked and it was not there to tell"
            # belongs with "we looked and could not tell", not with neither.
            indeterminate.append(rel)
            continue
        age = (now - stamp[1] / 1e9) / 86400.0
        found = cache.get("p:" + rel, stamp)
        if found is MISS:
            text = read_head(plan_path)
            found = list(_plan_status_and_id(text))
            if text or stamp[0] == 0:
                cache.put("p:" + rel, stamp, found)
        status, plan_id = found

        if status is None:
            indeterminate.append(rel)
            continue

        basename = plan_path.name
        is_terminal = status in TERMINAL_PLAN_STATUSES

        # The reference scan only decides a plan that is already old and terminal.
        if age >= age_days and is_terminal and not any(
            (plan_id and v == plan_id) or v.endswith(basename) or v == rel
            for v in reference_values
        ):
            prunable.append(rel)
        else:
            retained.append(rel)

    cache.retain("p:", {"p:" + rel_prefix + name for name, _ in listing})
    cache.flush()
    return PlanPruneResult(
        prunable=sorted(prunable),
        retained=sorted(retained),
        indeterminate=sorted(indeterminate),
    )

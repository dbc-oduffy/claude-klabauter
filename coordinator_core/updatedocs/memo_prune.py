"""
coordinator_core.updatedocs.memo_prune — the cross-repo archive memo prune predicate.

Purpose: pure compute over `<memo_corpus_root>/archive/*.md`, implementing the audit's B6
row from `artifact-pruning.md` § Step 1: a memo is prunable when its frontmatter
`status:` reads `actioned` AND its mtime is older than `age_days`. Returns
`MemoPruneResult` with `prunable`/`retained`/`indeterminate` as sorted path
lists -- no per-candidate evidence object, matching `plan_prune`'s shape.

Negative spec: a memo with no `status:` key at all is INDETERMINATE, never
prunable — collapsing that into either other bucket is the failure this module
exists to prevent (mirrors C3's plan-prune three-state contract).

Perf: reads only the bounded frontmatter head of each file (via the shared
`_common.read_head`), not the whole body -- the predicate never needs the
memo's body text. Status is still read UNCONDITIONALLY, for every file,
including ones that already fail the age leg: an earlier revision stat-ed
first and skipped the head-read for young files, which quietly broke the
contract this module exists to hold (a young no-status memo folded into
`retained`, indistinguishable from one whose status was read and found
non-actioned). Indeterminate is age-independent, full stop.

No writes, no deletion — this emits candidate paths; the disposal
decision stays with the ceremony and its existing guards (see plan's Out of
scope). No `@register_op`, no `GateResult` — the gate layer (C5,
`coordinator_core.ops.updatedocs_gates`) owns verdict mapping.

Spec backlink: pln-bucket-2-extraction-four-deter-e121fa (chunk C4)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from coordinator_core.frontmatter.primitives import (
    read_fm_field_unquoted,
    split_frontmatter,
)
from coordinator_core.memo_corpus import memo_corpus_root
from coordinator_core.updatedocs._common import UpdatedocsTargetMissing, read_head

DEFAULT_AGE_DAYS = 90


@dataclass(frozen=True)
class MemoPruneResult:

    prunable: list[str] = field(default_factory=list)
    retained: list[str] = field(default_factory=list)
    indeterminate: list[str] = field(default_factory=list)


def compute_memo_prune_candidates(
    repo_root: Path | str, age_days: int = DEFAULT_AGE_DAYS
) -> MemoPruneResult:
    repo_root = Path(repo_root)
    archive_dir = Path(memo_corpus_root(str(repo_root))) / "archive"
    if not archive_dir.is_dir():
        raise UpdatedocsTargetMissing(archive_dir)

    now = time.time()
    age_floor_days = age_days

    prunable: list[str] = []
    retained: list[str] = []
    indeterminate: list[str] = []

    for file_path in archive_dir.glob("*.md"):
        rel = file_path.relative_to(repo_root).as_posix()

        try:
            mtime = file_path.stat().st_mtime
        except OSError:
            # not look" is INDETERMINATE, never a silent drop: the three lists
            indeterminate.append(rel)
            continue

        age_days_actual = (now - mtime) / 86400

        text = read_head(file_path)
        if not text:
            indeterminate.append(rel)
            continue

        split = split_frontmatter(text)
        status = read_fm_field_unquoted(split.fm_text, "status") if split else None

        if status is None:
            indeterminate.append(rel)
            continue

        status_leg = status == "actioned"
        age_leg = age_days_actual >= age_floor_days

        if status_leg and age_leg:
            prunable.append(rel)
        else:
            retained.append(rel)

    return MemoPruneResult(
        prunable=sorted(prunable),
        retained=sorted(retained),
        indeterminate=sorted(indeterminate),
    )

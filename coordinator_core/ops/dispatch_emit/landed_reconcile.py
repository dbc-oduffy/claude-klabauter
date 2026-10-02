"""coordinator_core.ops.dispatch_emit.landed_reconcile -- flip the plan rows of
an inventory item routed out as already landed at HEAD.

Purpose: a Chunk-table row whose disposition says the work is already in the
tree never reaches the minted spine, so nothing else ever marks its plan's
rows. This closes that gap at emit time: each such item's `open` rows go to
`coded` with `disposition_ref: <HEAD sha>` through
`terminal_commit._flip_rows_coded`, the same line-level stamper the terminal
commit uses. A row already in any other disposition is left alone.

Writes plan documents and commits nothing; the caller reports the files.

Negative-spec:
  - Does NOT touch a plan outside the repo the inventory lives in.
  - Does NOT flip a row of a live item's plan; only items whose own
    disposition names a landing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.frontmatter.body_blocks import LocateStatus
from coordinator_core.frontmatter.schema_validate import check_plan_tasks_source
from coordinator_core.git.git_state import head_sha
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.dispatch_emit.inventory_mint import (
    _bare_plan_row_id,
    _resolve_spec_plan_path,
    _strip_backtick,
    parse_chunk_table,
)
from coordinator_core.ops.dispatch_emit.terminal_commit import _flip_rows_coded
from coordinator_core.ops.plan_tasks_render import load_rows
from coordinator_core.session.claimed_write import replace_text


def _names_a_landing(disposition: str) -> bool:
    text = disposition.strip().lower()
    return text.startswith(("landed", "already-fixed", "already fixed")) or (
        text.startswith(("routed out", "routed-out")) and "landed" in text
    )


def reconcile_landed(inventory_path: Path) -> Dict[str, List[str]]:
    """`{plan path (repo-relative): [row ids flipped]}` for every plan an
    already-landed item names; `{}` when nothing was open or HEAD is unreadable."""
    inventory = Path(inventory_path).resolve()
    root = inventory.parents[2]
    sha = head_sha(root)
    if not sha:
        return {}
    wanted: Dict[Path, Optional[set]] = {}
    for row in parse_chunk_table(inventory.read_text(encoding="utf-8")):
        if not _names_a_landing(row["disposition"]):
            continue
        plan = contained_path(
            _resolve_spec_plan_path(inventory, _strip_backtick(row["spec path"])), [root]
        )
        if plan is None or not plan.is_file():
            continue
        item = _strip_backtick(row["id"])
        wanted.setdefault(plan, set()).add(item)

    flipped_by_plan: Dict[str, List[str]] = {}
    for plan, items in wanted.items():
        text = plan.read_text(encoding="utf-8")
        loaded = load_rows(text)
        if loaded.status is not LocateStatus.LOCATED:
            continue
        ids = {r["id"] for r in loaded.rows if isinstance(r, dict) and isinstance(r.get("id"), str)}
        targets: set = set()
        for item in items:
            single = item if item in ids else _bare_plan_row_id(item)
            if single in ids:
                targets.add(single)
            else:
                targets |= ids
        updated, flipped = _flip_rows_coded(text, targets, sha)
        if not flipped:
            continue
        if check_plan_tasks_source(updated) is not None and check_plan_tasks_source(text) is None:
            continue
        replace_text(plan, updated)
        flipped_by_plan[plan.relative_to(root).as_posix()] = flipped
    return flipped_by_plan

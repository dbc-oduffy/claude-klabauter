"""coordinator_core.ops.dispatch_emit.landed_reconcile -- flip the plan rows of
an inventory item routed out as already landed at HEAD.

Purpose: a Chunk-table row whose disposition says the work is already in the
tree never reaches the minted spine, so nothing else ever marks its plan's
rows. This closes that gap at emit time: each such item's `open` rows go to
`coded` with `disposition_ref: <HEAD sha>` through
`plan_row_coding.flip_rows_coded`, the same line-level stamper the terminal
commit uses. A row already in any other disposition is left alone.

`reconcile_done_noop` does the same for chunks the caller classified as DONE
with no declared write dirty, recording the executor report in the detail.

Writes plan documents and commits nothing; the caller reports the files.

Negative-spec:
  - `reconcile_done_noop` flips only rows the caller classified no-op.
  - Does NOT touch a plan outside the repo the inventory lives in.
  - Does NOT flip a row of a live item's plan; only items whose own
    disposition names a landing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Mapping, Optional

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
from coordinator_core.ops.dispatch_emit.plan_row_coding import flip_rows_coded
from coordinator_core.ops.plan_tasks_render import load_rows
from coordinator_core.session.claimed_write import replace_text

# Flips plan rows in place via replace_text; edits authored plans, emits no generated artifact.
GENERATES = []


def _names_a_landing(disposition: str) -> bool:
    text = disposition.strip().lower()
    return text.startswith(("landed", "already-fixed", "already fixed")) or (
        text.startswith(("routed out", "routed-out")) and "landed" in text
    )


def _has_uncleared_gate(row: dict) -> bool:
    """True when the row carries an `external_gate`/`external_gates` entry not explicitly `cleared: true`."""
    for key in ("external_gate", "external_gates"):
        gates = row.get(key)
        if gates is None:
            continue
        for gate in gates if isinstance(gates, list) else [gates]:
            if not isinstance(gate, dict) or gate.get("cleared") is not True:
                return True
    return False


def reconcile_landed(inventory_path: Path) -> Dict[str, List[str]]:
    """`{plan path (repo-relative): [row ids flipped]}` for every plan an
    already-landed item names; `{}` when nothing was open or HEAD is unreadable."""
    return reconcile_landed_detail(inventory_path)[0]


def reconcile_landed_detail(inventory_path: Path) -> tuple:
    """`(flipped, withheld)`, each `{plan path: [row ids]}`.

    A row behind an uncleared external gate is never flipped. An item naming a
    whole plan flips only the open rows whose declared writes a commit already
    landed (`rows_landed_outside_executor`); the other open rows are `withheld`.
    An item naming one row is trusted, gate permitting."""
    from coordinator_core.ops.dispatch_emit.plan_row_coding import rows_landed_outside_executor

    inventory = Path(inventory_path).resolve()
    root = inventory.parents[2]
    sha = head_sha(root)
    if not sha:
        return {}, {}
    withheld_by_plan: Dict[str, List[str]] = {}
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
        rel = plan.relative_to(root).as_posix()
        gated = {r["id"] for r in loaded.rows if isinstance(r, dict) and isinstance(r.get("id"), str) and _has_uncleared_gate(r)}
        targets: set = set()
        whole: set = set()
        for item in items:
            single = item if item in ids else _bare_plan_row_id(item)
            if single in ids:
                targets.add(single)
            else:
                whole |= ids
        if whole:
            evidenced = set(rows_landed_outside_executor(root, rel, loaded.rows))
            targets |= whole & evidenced
            lacking = sorted(
                (whole - evidenced - targets) | (whole & gated),
                key=lambda i: [r.get("id") for r in loaded.rows].index(i),
            )
        else:
            lacking = []
        targets -= gated
        updated, flipped = flip_rows_coded(text, targets, sha)
        open_ids = _open_ids(loaded.rows)
        withheld = [i for i in lacking if i in open_ids]
        if withheld:
            withheld_by_plan[rel] = withheld
        if not flipped:
            continue
        if check_plan_tasks_source(updated) is not None and check_plan_tasks_source(text) is None:
            continue
        replace_text(plan, updated)
        flipped_by_plan[rel] = flipped
    return flipped_by_plan, withheld_by_plan


def _open_ids(rows: list) -> set:
    from coordinator_core.execute_plan_assemble.row_spans import _row_disposition

    return {r["id"] for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str) and _row_disposition(r) == "open"}


def reconcile_done_noop(
    inventory_path: Path, noop: Mapping[str, Mapping[str, str]]
) -> Dict[str, List[str]]:
    """`{plan path (repo-relative): [row ids flipped]}` for each `noop` entry
    (`{plan rel: {chunk id: report rel}}`); `{}` when HEAD is unreadable."""
    root = Path(inventory_path).resolve().parents[2]
    sha = head_sha(root)
    if not sha:
        return {}
    flipped_by_plan: Dict[str, List[str]] = {}
    for plan_rel, chunks in noop.items():
        plan = contained_path(root / plan_rel, [root])
        if plan is None or not plan.is_file():
            continue
        text = plan.read_text(encoding="utf-8")
        details = {
            chunk: f"done-no-op: executor report {report} DONE, no declared write dirty"
            for chunk, report in chunks.items()
        }
        updated, flipped = flip_rows_coded(text, set(chunks), sha, details=details)
        if not flipped:
            continue
        if check_plan_tasks_source(updated) is not None and check_plan_tasks_source(text) is None:
            continue
        replace_text(plan, updated)
        flipped_by_plan[plan.relative_to(root).as_posix()] = flipped
    return flipped_by_plan

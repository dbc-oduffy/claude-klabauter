"""coordinator_core.ops.dispatch_emit.stranded_part_rebuild -- write a landing marker into the
emission receipt of a split part whose script is gone and whose receipt predates
``landing_markers``.

``python -m coordinator_core.ops.dispatch_emit.stranded_part_rebuild --script <rel>
--task-output <path> --inventory <tranche record>`` is the command a ``script-missing``
refusal names. The marker is rebuilt from the tranche record's rows as they read now, filtered
to the chunk ids the part's task output reports; ``rebuilt_from`` records that.

Negative-spec:
  - Does NOT cut a tranche or a part, and does NOT hold rows out for a dirty write set: the
    part's own uncommitted product would otherwise hold its rows out.
  - Does NOT touch a receipt that already carries ``landing_markers`` or ``landed``.
  - Does NOT write outside ``worktree_root``, and does not commit.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from coordinator_core.ops.dispatch_emit.emission_receipt import emission_receipt_path

# Generator-provenance: writes the receipt and part spine beside a caller-named script --
# no fixed target, purely caller-named (same disposition as emission_receipt.py).
GENERATES = []


class StrandedPartRebuildError(ValueError):
    """The part cannot be rebuilt; the message names why."""


def _task_output_digest(task_output_path: Path) -> dict:
    try:
        data = json.loads(task_output_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StrandedPartRebuildError(f"cannot read task output {str(task_output_path)!r}: {exc}") from exc
    if isinstance(data, dict) and "next_action" not in data and isinstance(data.get("result"), dict):
        data = data["result"]
    if not isinstance(data, dict):
        raise StrandedPartRebuildError(f"task output {str(task_output_path)!r} is not a digest")
    return data


def _contained(worktree_root: Path, rel: str) -> Path:
    root = worktree_root.resolve()
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise StrandedPartRebuildError(f"{rel!r} resolves outside {worktree_root}") from exc
    return path


def _spine_text(run_id: str, deliverable_id: Optional[str], inventory_rel: str, rows: list) -> str:
    from coordinator_core.ops.dispatch_emit.inventory_mint import _dump_rows

    front = [f"run_id: {run_id}", "derived_from: mise inventory record"]
    if deliverable_id:
        front.append(f"deliverable_id: {deliverable_id}")
    return (
        "---\n" + "\n".join(front) + "\n---\n\n"
        + f"# Minted dispatch spine — {run_id}\n\n"
        + f"Derived from `{inventory_rel}` by `stranded_part_rebuild`.\n"
        + "**Do not hand-edit** — the next mint overwrites this file in place.\n"
        + "Coordination facts belong in the inventory record.\n\n"
        + "## Tasks\n\n```yaml plan-tasks\n"
        + _dump_rows(rows)
        + "```\n"
    )


def _wave_row(raw: dict):
    from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED
    from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

    writes = raw.get("writes")
    return WaveRow(
        id=raw["id"],
        title=raw.get("title", ""),
        surface=raw.get("surface", ""),
        writes=list(writes) if isinstance(writes, list) and writes else UNDECLARED,
        reads=[],
        depends_on=[],
        agent_type=raw.get("agent_type"),
        agent_model=raw.get("agent_model") or raw.get("model"),
        body=raw.get("body") or "",
        writes_under=tuple(p for p in (raw.get("writes_under") or ()) if isinstance(p, str)),
        change_kind=raw.get("change_kind"),
    )


def rebuild_stranded_part(
    worktree_root: Path, script_rel: str, task_output_path: Path, inventory_path: Path
) -> dict:
    """Add ``landing_markers`` and ``rebuilt_from`` to the receipt beside ``script_rel`` and
    restore the part spine at the receipt's ``plan_relpath`` when absent.

    Returns ``{"receipt", "spine_written", "chunks", "marker"}``. Raises
    ``StrandedPartRebuildError`` on a missing or already-marked receipt, a task output naming
    another script, a chunk id the tranche record does not resolve, or a marker with nothing
    to commit.
    """
    from coordinator_core.ops.dispatch_emit.emit import _terminal_commit_marker
    from coordinator_core.ops.dispatch_emit.emit import _widen_with_test_candidates
    from coordinator_core.ops.dispatch_emit.inventory_mint import mint_rows, parse_chunk_table
    from coordinator_core.ops.dispatch_emit.pathspec import commit_pathspec_or_none

    root = Path(worktree_root)
    script = _contained(root, script_rel)
    receipt_path = emission_receipt_path(script)
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StrandedPartRebuildError(f"no readable receipt beside {script_rel}: {exc}") from exc
    if not isinstance(receipt, dict):
        raise StrandedPartRebuildError(f"receipt {receipt_path.name} is not an object")
    for key in ("landing_markers", "landed"):
        if receipt.get(key):
            raise StrandedPartRebuildError(
                f"receipt {receipt_path.name} already carries {key}; nothing to rebuild"
            )

    digest = _task_output_digest(Path(task_output_path))
    action = digest.get("next_action")
    params = action.get("params") if isinstance(action, dict) else None
    named = params.get("script_path") if isinstance(params, dict) else None
    if not isinstance(named, str) or Path(named).as_posix() != Path(script_rel).as_posix():
        raise StrandedPartRebuildError(
            f"task output names script {named!r}, not {script_rel!r}"
        )
    chunk_ids = digest.get("chunks")
    if (
        not isinstance(chunk_ids, list)
        or not chunk_ids
        or not all(isinstance(c, str) for c in chunk_ids)
        or len(set(chunk_ids)) != len(chunk_ids)
    ):
        raise StrandedPartRebuildError(f"task output {str(task_output_path)!r} carries no chunks")

    inventory = Path(inventory_path)
    try:
        chunk_table = parse_chunk_table(inventory.read_text(encoding="utf-8"))
    except OSError as exc:
        raise StrandedPartRebuildError(f"cannot read inventory {str(inventory)!r}: {exc}") from exc
    by_id = {row["id"]: row for row in mint_rows(chunk_table, inventory_path=inventory)}
    missing = [cid for cid in chunk_ids if cid not in by_id]
    if missing:
        raise StrandedPartRebuildError(
            f"chunk id(s) {', '.join(map(str, missing))} do not resolve in {inventory.name}"
        )
    kept = set(chunk_ids)
    rows = []
    for cid in chunk_ids:
        row = dict(by_id[cid])
        if row.get("depends_on"):
            row["depends_on"] = [e for e in row["depends_on"] if e.get("chunk") in kept]
        rows.append(row)

    wave_rows = [_wave_row(r) for r in rows]
    pathspecs = {
        w.id: _widen_with_test_candidates(commit_pathspec_or_none([w]) or [], root)
        for w in wave_rows
    }
    plan = digest.get("plan")
    plan_rel = receipt.get("plan_relpath")
    marker = _terminal_commit_marker(
        wave_rows,
        pathspecs,
        plan_path=plan_rel,
        deliverable_id=plan.get("deliverable_id") if isinstance(plan, dict) else None,
        session_id=receipt.get("session_id"),
        row_prefixes={w.id: w.writes_under for w in wave_rows},
        run_base_sha=digest.get("run_base_sha"),
    )
    if marker is None:
        raise StrandedPartRebuildError("the rebuilt rows declare no path to commit")

    spine_written = False
    if isinstance(plan_rel, str) and plan_rel:
        spine = _contained(root, plan_rel)
        if not spine.exists():
            deliverable = plan.get("deliverable_id") if isinstance(plan, dict) else None
            spine.parent.mkdir(parents=True, exist_ok=True)
            spine.write_text(
                _spine_text(spine.name.removesuffix(".spine.md"), deliverable, inventory.name, rows),
                encoding="utf-8",
                newline="\n",
            )
            spine_written = True

    receipt["landing_markers"] = [marker]
    receipt["rebuilt_from"] = {
        "inventory": Path(inventory).name,
        "task_output": Path(task_output_path).name,
        "at": datetime.now().isoformat(timespec="seconds"),
    }
    receipt_path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    return {
        "receipt": str(receipt_path),
        "spine_written": spine_written,
        "chunks": list(chunk_ids),
        "marker": marker,
    }


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Write a landing marker into a pre-landing_markers receipt of a stranded split part."
    )
    ap.add_argument("--script", required=True, help="repo-relative path of the missing part script")
    ap.add_argument("--task-output", required=True, help="the part's Workflow task-output file")
    ap.add_argument("--inventory", required=True, help="the part's tranche inventory record")
    ap.add_argument("--worktree-root", default=".", help="worktree holding the script's receipt")
    args = ap.parse_args(argv)
    try:
        out = rebuild_stranded_part(
            Path(args.worktree_root), args.script, Path(args.task_output), Path(args.inventory)
        )
    except StrandedPartRebuildError as exc:
        print(f"stranded_part_rebuild: refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({k: v for k, v in out.items() if k != "marker"}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

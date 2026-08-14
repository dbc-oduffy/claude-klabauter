"""
bin/sweep-actioned-memos.py — thin CLI over the cross-repo actioned-memo sweep.

Purpose: give ``fleet.archive_actioned_memos`` a claude-klabauter-owned, Windows-first,
naked-Python entry point.  The op itself has always worked; until this CLI
landed its ONLY caller was the composite ``session.boot_sweep``, which in turn
was only ever invoked by DoE's bash ``session-init.sh`` SessionStart hook.  That
hook is not registered on this machine (``~/.claude/settings.json`` carries an
empty ``hooks`` object), so the sweep last fired on 2026-07-14 (``2bad853c``)
and actioned memos accreted in ``cross-repo/inbox/`` from then on — making the
inbound backlog read at roughly three times its real size.

Invoking the composite ``session.boot_sweep`` is NOT an acceptable substitute:
it also archives handoffs and terminal plans and reaps review-findings
sidecars, i.e. it has a blast radius far wider than the memo channel.  This CLI
runs the memo family and nothing else.

Usage:
    python bin/sweep-actioned-memos.py [--dry-run] [--json] [--repo-root DIR]

    --dry-run    List sweepable memos; mutate nothing.
    --json       Emit the raw result dict as JSON instead of human lines.
    --repo-root  Repo to sweep (default: cwd; resolved to the MAIN worktree).

Idempotent: a second consecutive run archives nothing and exits 0.

Naked Python per CLAUDE.md § Runtime conventions (DR-047/DR-059) — no bash, no
shell wrapper, no ``python3`` shebang dependency on Windows.

Spec backlinks:
  - Op: coordinator_core/ops/fleet/archive_actioned_memos.py
  - Channel doctrine: cross-repo/README.md ("Actioned memos auto-sweep to
    cross-repo/archive/ once resolved.")
  - DR-211: docs/decisions/DR-211-fleet-op-substrate-write-boundary.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coordinator_core.lifecycle import git_common_dir  # noqa: E402
from coordinator_core.ops.fleet._common import main_worktree_root  # noqa: E402
from coordinator_core.ops.fleet.archive_actioned_memos import (  # noqa: E402
    _REASON_DEST_CONFLICT,
    _archive_dest,
    _collect_inbox_memo_paths,
    _is_identical_duplicate,
    _is_terminal,
    archive_actioned_memos_internal,
)


async def _preview(worktree_root: Path, common_dir: Path) -> tuple[list[str], list[str]]:
    """Return (ids this sweep would archive, blocked-memo descriptions).

    The second element is load-bearing: a memo whose archive destination is occupied by a
    DIFFERENT file is permanently stuck, and dropping it silently made an empty candidate
    list read as "inbox converged" when it actually meant "everything is wedged".
    """
    out: list[str] = []
    blocked: list[str] = []
    for memo_path in _collect_inbox_memo_paths(worktree_root):
        rel = memo_path.relative_to(worktree_root).as_posix()
        is_terminal, _reason = await _is_terminal(memo_path, common_dir)
        if not is_terminal:
            continue
        dst = _archive_dest(worktree_root, memo_path)
        if dst.exists() and not _is_identical_duplicate(memo_path, dst):
            blocked.append(
                f"{rel}: archive destination {dst.relative_to(worktree_root).as_posix()} "
                f"holds a DIFFERENT file — reconcile the two copies by hand"
            )
            continue
        out.append(rel)
    return out, blocked


def sweep(repo_root: Path, *, dry_run: bool) -> dict:
    """Resolve roots and run the memo sweep. Returns a plain result dict."""
    common_dir = Path(git_common_dir(str(repo_root)))
    worktree_root = main_worktree_root(common_dir)

    if dry_run:
        candidates, blocked = asyncio.run(_preview(worktree_root, common_dir))
        return {"dry_run": True, "candidates": candidates, "blocked": blocked}

    acted, skipped, failed = asyncio.run(
        archive_actioned_memos_internal(worktree_root, common_dir)
    )
    return {
        "dry_run": False,
        "archived": [a["id"] for a in acted],
        # Carry the full skip records, not just a count: the internal op distinguishes a
        # benign not-yet-terminal skip from a wedged archive-dest-conflict, and collapsing
        # that to len() threw away the only signal that a memo is permanently stuck.
        "skipped": skipped,
        "failed": failed,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sweep actioned cross-repo memos to archive/.")
    parser.add_argument("--dry-run", action="store_true", help="Preview only; mutate nothing.")
    parser.add_argument("--json", action="store_true", help="Emit raw JSON.")
    parser.add_argument(
        "--repo-root", type=Path, default=Path.cwd(), help="Repo to sweep (default: cwd)."
    )
    args = parser.parse_args(argv)

    try:
        result = sweep(args.repo_root.resolve(), dry_run=args.dry_run)
    except Exception as exc:  # fail loud, but with a readable message
        print(f"sweep-actioned-memos: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
        if result.get("failed") or result.get("blocked"):
            return 1
        wedged_json = [
            s for s in result.get("skipped", []) if s.get("reason") == _REASON_DEST_CONFLICT
        ]
        return 1 if wedged_json else 0

    if args.dry_run:
        cands = result["candidates"]
        blocked = result["blocked"]
        if not cands and not blocked:
            print("sweep-actioned-memos: nothing to archive (inbox already converged).")
        elif not cands:
            print("sweep-actioned-memos: no memo can be archived — all candidates BLOCKED.")
        else:
            print(f"sweep-actioned-memos: {len(cands)} memo(s) would be archived:")
            for c in cands:
                print(f"  {c}")
        for b in blocked:
            print(f"sweep-actioned-memos: BLOCKED {b}", file=sys.stderr)
        return 1 if blocked else 0

    archived = result["archived"]
    if archived:
        print(f"sweep-actioned-memos: archived {len(archived)} memo(s) inbox -> archive:")
        for a in archived:
            print(f"  {a}")
    else:
        print("sweep-actioned-memos: no-op (no actioned memos in inbox).")

    wedged = [s for s in result["skipped"] if s.get("reason") == _REASON_DEST_CONFLICT]
    for s in wedged:
        print(
            f"sweep-actioned-memos: BLOCKED {s['id']}: {s['reason']} — this memo cannot be "
            "archived and every future sweep will skip it until reconciled by hand",
            file=sys.stderr,
        )

    if result["failed"]:
        for f in result["failed"]:
            print(f"sweep-actioned-memos: FAILED {f['id']}: {f['reason']}", file=sys.stderr)
        return 1
    return 1 if wedged else 0


if __name__ == "__main__":
    sys.exit(main())

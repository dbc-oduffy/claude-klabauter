"""
coordinator_core.ops.fleet.tests.test_prune_bugs_registration_smoke

Split out of C4's `test_prune_bugs.py` for fixture machinery, not tidiness
(docs/plans/2026-09-07-fleet-prune-closed-bugs-v2-rebuild.md, C5): this is
the ONE module that drives a live `coordinator_core.ops._eager_import_all()`
registry rather than `patched_disposition_seam`'s direct module import, and
it is gated on C1 (SUSPENDED_OPS row removed) while C4 is not. Nothing else
from C4's coverage moves here.

Proves the op is discoverable at start_server()-equivalent registration time
(`coordinator_core.ops._eager_import_all()`) and completes a dry_run:true ->
dry_run:false round trip through the LIVE `coordinator_core.ipc._REGISTRY`
entry, with the returned candidate_ids each D1-re-verified on the act leg.

The mover (`archive_and_commit`) is still patched via the family's mandated
git-free seam (`archive_git_free_seam.patched_disposition_seam`) — the fact
under test here is registration/dispatchability, not the mover's own git
mechanics, which is squarely the seam's documented first class (patch
`archive_and_commit` on the module object; the registry-resolved handler is
the same function object, so the patch is visible on both paths).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from coordinator_core.ops.fleet.tests.archive_git_free_seam import (
    patched_disposition_seam,
)


def _run(coro):
    return asyncio.run(coro)


def _make_bug(worktree: Path, name: str, body: str) -> Path:
    bug_dir = worktree / "state" / "bug-backlog"
    bug_dir.mkdir(parents=True, exist_ok=True)
    path = bug_dir / name
    path.write_text(body, encoding="utf-8")
    return path


def test_op_registers_at_eager_import_and_round_trips_dry_run_then_act(tmp_path: Path) -> None:
    # Force full registration exactly like the SAFE FALLBACK / warm-server
    from coordinator_core.ops import _eager_import_all
    from coordinator_core.ipc import _REGISTRY

    _eager_import_all()

    assert "fleet.prune_closed_bugs" in _REGISTRY, (
        "fleet.prune_closed_bugs missing from the live registry after "
        "_eager_import_all() — coordinator_core/ops/__init__.py's "
        "_EAGER_OP_MODULES table is missing the prune_bugs entry"
    )

    handler = _REGISTRY["fleet.prune_closed_bugs"]

    from coordinator_core.ops.fleet import prune_bugs as m

    assert handler is m._handler

    worktree = tmp_path / "repo"
    _make_bug(worktree, "2026-04-01-closed.yaml", "status: closed\ntitle: end to end\n")
    _make_bug(worktree, "2026-04-02-open.yaml", "status: open\ntitle: still open\n")

    with patched_disposition_seam(m, worktree=worktree) as mover:
        preview = _run(
            handler(
                {"mode": "already-terminal", "dry_run": True, "candidate_ids": None},
                repo_root=str(worktree),
            )
        )

        assert preview["exit_code"] == 0
        candidate_ids = [c["id"] for c in preview["candidates"]]
        assert candidate_ids == ["state/bug-backlog/2026-04-01-closed.yaml"]

        result = _run(
            handler(
                {
                    "mode": "already-terminal",
                    "dry_run": False,
                    "candidate_ids": candidate_ids,
                },
                repo_root=str(worktree),
            )
        )

    assert result["exit_code"] == 0
    assert result["acted"] == [{"id": candidate_ids[0], "archived": True}]
    assert result["skipped"] == []
    assert result["failed"] == []
    assert mover.captured is not None
    assert len(mover.captured) == 1
    assert mover.captured[0].candidate_id == candidate_ids[0]

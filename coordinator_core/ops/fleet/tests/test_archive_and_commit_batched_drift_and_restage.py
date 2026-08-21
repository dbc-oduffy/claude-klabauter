"""
coordinator_core.ops.fleet.tests.test_archive_and_commit_batched_drift_and_restage

Was: pinned the batched `git diff --name-only` (disk/HEAD drift) and
`git add --` (op-authored-content restage) spawns amplification burn-down C4
(2026-08-19) introduced ahead of archive_and_commit's per-move `git mv` loop.

F-5 swap (2026-08-21, docs/plans/2026-08-20-the-close-ceremony-stops-paying-
for-the-join.md C5) removed both: `git mv` is no longer the mover (see
_common.archive_and_commit's docstring), so there is no stale private-index
blob for the drift guard to detect or restage_src to route around --
os.replace always carries current on-disk content. What survives from C4's
batching discipline is the ONE remaining git spawn per archive_and_commit
call that scales with the move count: the batched `git add -- <every acted
src AND dst>` staging call. This module now pins THAT batch: one spawn
covers an arbitrarily large move set, per-item attribution on a batch
failure is exact (every acted move is reversed and reclassified, not just
some), and an unrelated already-failed move (a pre-existing dst) is
unaffected by a staging failure among the OTHER moves in the same call.

Real git is load-bearing: the private index staging behaviour this module
pins needs a real index, not a mocked git with none to diverge from --
same argument test_archive_and_commit_disk_head_drift.py's docstring makes.

Governed real-git pattern (state/audits/2026-08-07-spawn-heavy-test-excision-
ledger.md): each test below gets its own throwaway repo; no module-scope
hoist.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.ops.fleet._common import Move, archive_and_commit

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    # popup-intentional-last-resort — test-only real-git spawn, mirrors the
    # governed real_git.py fixture's own unguarded pattern; no console window
    # risk on the CI/dev platforms this suite runs on.
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _run(coro):
    return asyncio.run(coro)


def _init_repo(root: Path) -> None:
    root.mkdir()
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "test@example.invalid"], root)
    _git(["config", "user.name", "test"], root)


def _make_spawn_counter():
    """Build (counts, counting_spawn): counting_spawn wraps the REAL
    asyncio.create_subprocess_exec, counting calls by argv[:3] (`git add --`
    / `git diff --name-only`, the shapes this module's history cares about)
    or argv[:2] otherwise, so a test can assert "one spawn for the whole
    batch" directly rather than inferring it from final state alone.

    Deliberately a bare `async def` closure, not a callable class instance:
    `patch("asyncio.create_subprocess_exec", side_effect=...)` auto-detects
    the target as a coroutine function and installs an AsyncMock, whose
    side_effect dispatch only recognises `inspect.iscoroutinefunction` —
    true for a plain async function, false for an object's `async def
    __call__` — so a class-based counter would leave the returned coroutine
    un-awaited and produce a bogus `Process`.
    """
    real = asyncio.create_subprocess_exec
    counts: dict = {}

    async def counting_spawn(*argv, **kwargs):
        if len(argv) >= 3 and argv[1] in ("diff", "add") and argv[2] in ("--name-only", "--"):
            key = tuple(argv[:3])
        else:
            key = tuple(argv[:2])
        counts[key] = counts.get(key, 0) + 1
        return await real(*argv, **kwargs)

    return counts, counting_spawn


def test_batched_stage_spawns_once_for_a_multi_move_batch(tmp_path: Path) -> None:
    """3 moves, all clean -- ONE `git add --` spawn stages every acted
    move's src+dst pair; there is no per-item `git mv` left to count."""
    root = tmp_path / "repo"
    _init_repo(root)
    handoffs = root / "state" / "handoffs"
    handoffs.mkdir(parents=True)

    def _seed(name: str, content: str) -> Path:
        p = handoffs / name
        p.write_text(content, encoding="utf-8")
        return p

    srcs = [
        _seed(f"2026-08-{i:02d}-clean{i}.md", f"---\nstatus: claimed\n---\n\n{i}.\n")
        for i in range(1, 4)
    ]
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed: three handoffs"], root)

    def _dst(src: Path) -> Path:
        return root / "archive" / "handoffs" / "2026-08" / src.name

    moves = [
        Move(src=s, dst=_dst(s), candidate_id=f"state/handoffs/{s.name}")
        for s in srcs
    ]

    counts, counting_spawn = _make_spawn_counter()
    with patch("asyncio.create_subprocess_exec", side_effect=counting_spawn):
        acted, failed = _run(
            archive_and_commit(worktree_root=root, moves=moves, subject="fleet: archive 3 shipped handoff(s)")
        )

    add_calls = counts.get(("git", "add", "--"), 0)
    assert add_calls == 1, f"stage must spawn ONCE for the whole batch; counts={counts}"
    mv_calls = counts.get(("git", "mv"), 0)
    assert mv_calls == 0, "git mv must never be spawned -- os.replace is the mover"

    assert failed == []
    assert {a["id"] for a in acted} == {m.candidate_id for m in moves}
    for s in srcs:
        assert not s.exists()
        assert _dst(s).exists()


def test_stage_batch_failure_reverses_every_acted_move(tmp_path: Path) -> None:
    """A batched `git add -- <src, dst>` failure (mocked -- a generic
    staging failure, not an index/worktree divergence) reverses EVERY acted
    move's os.replace on disk and reclassifies all of them to failed[] --
    per-item attribution on a whole-batch stage failure is "all or
    nothing", unlike the per-move `git mv` failure loop it replaced."""
    root = tmp_path / "repo"
    _init_repo(root)
    handoffs = root / "state" / "handoffs"
    handoffs.mkdir(parents=True)

    def _seed(name: str, content: str) -> Path:
        p = handoffs / name
        p.write_text(content, encoding="utf-8")
        return p

    src1 = _seed("2026-08-01-a.md", "---\nstatus: claimed\n---\n\nA.\n")
    src2 = _seed("2026-08-02-b.md", "---\nstatus: claimed\n---\n\nB.\n")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed: two handoffs"], root)

    def _dst(src: Path) -> Path:
        return root / "archive" / "handoffs" / "2026-08" / src.name

    moves = [
        Move(src=src1, dst=_dst(src1), candidate_id="state/handoffs/2026-08-01-a.md"),
        Move(src=src2, dst=_dst(src2), candidate_id="state/handoffs/2026-08-02-b.md"),
    ]

    real_spawn = asyncio.create_subprocess_exec

    async def _fail_add(*argv, **kwargs):
        if len(argv) >= 2 and argv[0] == "git" and argv[1] == "add":
            class _FakeProc:
                returncode = 1

                async def communicate(self):
                    return b"", b"synthetic stage failure"

            return _FakeProc()
        return await real_spawn(*argv, **kwargs)

    with patch("asyncio.create_subprocess_exec", side_effect=_fail_add):
        acted, failed = _run(
            archive_and_commit(worktree_root=root, moves=moves, subject="fleet: archive 2 shipped handoff(s)")
        )

    assert acted == []
    failed_ids = {f["id"] for f in failed}
    assert failed_ids == {m.candidate_id for m in moves}
    for item in failed:
        assert "stage-failed" in item["reason"]

    # Every move's os.replace is reversed -- both srcs restored, neither dst survives.
    assert src1.exists() and src1.read_text(encoding="utf-8") == "---\nstatus: claimed\n---\n\nA.\n"
    assert src2.exists() and src2.read_text(encoding="utf-8") == "---\nstatus: claimed\n---\n\nB.\n"
    assert not _dst(src1).exists()
    assert not _dst(src2).exists()


def test_dst_exists_refusal_is_independent_of_a_sibling_stage_failure(tmp_path: Path) -> None:
    """A move refused up front (dst pre-exists, force=False) never reaches
    os.replace or the batched stage call -- it fails for its own reason
    regardless of what a sibling move's staging outcome is."""
    root = tmp_path / "repo"
    _init_repo(root)
    handoffs = root / "state" / "handoffs"
    handoffs.mkdir(parents=True)

    def _seed(name: str, content: str) -> Path:
        p = handoffs / name
        p.write_text(content, encoding="utf-8")
        return p

    clean = _seed("2026-08-01-clean.md", "---\nstatus: claimed\n---\n\nA.\n")

    def _dst(src: Path) -> Path:
        return root / "archive" / "handoffs" / "2026-08" / src.name

    # Occupied destination -- tracked, real archived history.
    occupied_dst = _dst(handoffs / "2026-08-02-occupied.md")
    occupied_dst.parent.mkdir(parents=True)
    occupied_dst.write_text("different content\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed: clean + occupied dst"], root)

    moves = [
        Move(src=clean, dst=_dst(clean), candidate_id="state/handoffs/2026-08-01-clean.md"),
        Move(
            src=handoffs / "2026-08-02-occupied.md",  # never created -- dst is what's occupied
            dst=occupied_dst,
            candidate_id="state/handoffs/2026-08-02-occupied.md",
        ),
    ]
    # The second move's own src doesn't exist either, but the dst-exists
    # check fires first (checked before os.replace is attempted).
    (handoffs / "2026-08-02-occupied.md").write_text("src content\n", encoding="utf-8")

    acted, failed = _run(
        archive_and_commit(worktree_root=root, moves=moves, subject="fleet: archive mixed batch")
    )

    assert {a["id"] for a in acted} == {moves[0].candidate_id}
    assert len(failed) == 1
    assert failed[0]["id"] == moves[1].candidate_id
    assert "dst-exists" in failed[0]["reason"]

    assert not clean.exists()
    assert _dst(clean).exists()
    assert occupied_dst.read_text(encoding="utf-8") == "different content\n"

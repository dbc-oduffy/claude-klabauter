"""Oracle for the `archive_and_commit` spawn FLOOR
(`coordinator_core/ops/fleet/_common.py::archive_and_commit`).

The floor is independent of the move count M. The archival seam renames in process
(`os.replace`), builds the `{path: (mode, sha) | _ABSENT}` tree-delta in process, and
lands it through `_commit_via_head_spine` (locked ref CAS, no git process). Two spawns
survive, each one call per batch regardless of M:

  * `git hash-object -w --stdin-paths` -- ONE batched call over the `restage_src=True`
    subset, issued through git_native's `subprocess.run` (`_hash_object_stdin_paths`,
    offloaded with `asyncio.to_thread`). Not issued at all when no Move opts into
    restage_src.
  * `_resync_main_index_for_moves`'s single `git restore --staged` -- issued through
    `asyncio.create_subprocess_exec`, one call over every acted src/dst.

So the pinned totals (asyncio + `subprocess.run`, counted together) are:

    all-plain batch          -> 1   (resync only)
    any batch with restage   -> 2   (hash-object + resync)

A per-Move drift-check, restage, `git mv`, or staging spawn creeping back in makes the
total scale with M and fails here. Both counters are needed: an asyncio-only count
misses the hash-object call, which runs on the `subprocess.run` path.

Real git throughout (Governed real-git pattern, state/audits/2026-08-07-spawn-heavy-
test-excision-ledger.md): a mocked git has no HEAD tree to read or blob to hash, and
this oracle's claim is about the ACTUAL spawn count against a real repo. Every fixture
lives under pytest's own `tmp_path`; nothing here touches the shared working tree.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.ops.fleet._common import Move, archive_and_commit
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    # popup-intentional-last-resort -- test-only real-git spawn, mirrors the governed
    # real_git.py fixture's own unguarded pattern (see the sibling
    # test_archive_and_commit_batched_drift_and_restage.py, which this oracle's
    # fixtures are deliberately shaped to match).
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def _init_repo(root: Path) -> None:
    root.mkdir()
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "test@example.invalid"], root)
    _git(["config", "user.name", "test"], root)


def _make_total_spawn_counter():
    """Build (total, async_spawn, sync_run): the two wrappers around the REAL
    `asyncio.create_subprocess_exec` and `subprocess.run` both increment `total[0]`,
    counting every call regardless of argv -- this oracle's claim is the total spawn
    count, not any one call's shape (the finer per-key claim is
    `test_archive_and_commit_batched_drift_and_restage.py`'s job).

    `async_spawn` is a bare `async def` closure, not a callable class -- `patch("asyncio.
    create_subprocess_exec", side_effect=...)` installs an AsyncMock whose side_effect
    dispatch requires `inspect.iscoroutinefunction`, which a class's `async def
    __call__` fails (the returned coroutine is left un-awaited).
    """
    real_async = asyncio.create_subprocess_exec
    real_run = subprocess.run
    total = [0]

    async def async_spawn(*argv, **kwargs):
        total[0] += 1
        return await real_async(*argv, **kwargs)

    def sync_run(*args, **kwargs):
        total[0] += 1
        return real_run(*args, **kwargs)

    return total, async_spawn, sync_run


def _seed_moves(root: Path, n_plain: int, n_restage: int) -> list[Move]:
    """Seed `n_plain` clean (non-restage) handoffs and `n_restage` op-authored-stamp
    (restage_src=True) handoffs, all committed clean so every Move lands in acted[] --
    this oracle measures the all-succeed spawn floor, not any failure path (the sibling
    per-key module already covers drift/restage failure attribution)."""
    handoffs = root / "state" / "handoffs"
    handoffs.mkdir(parents=True)

    plain_paths: list[Path] = []
    for i in range(n_plain):
        p = handoffs / f"2026-08-{i + 1:02d}-plain{i}.md"
        p.write_text(
            f"---\nstatus: claimed\ndeployment_state: shipped\n---\n\nplain {i}.\n",
            encoding="utf-8",
        )
        plain_paths.append(p)

    restage_paths: list[Path] = []
    for i in range(n_restage):
        p = handoffs / f"2026-08-{n_plain + i + 1:02d}-restage{i}.md"
        p.write_text(
            f"---\nstatus: claimed\ndeployment_state: in_flight\n---\n\nrestage {i}.\n",
            encoding="utf-8",
        )
        restage_paths.append(p)

    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed: floor-oracle fixture"], root)

    # Op-authored pre-move stamp on the restage set only, written AFTER the commit
    # above and never staged -- exactly the shape restage_src=True exists for.
    for p in restage_paths:
        text = p.read_text(encoding="utf-8").replace("in_flight", "shipped")
        p.write_text(text, encoding="utf-8")

    def _dst(src: Path) -> Path:
        return root / "archive" / "handoffs" / "2026-08" / src.name

    moves = [
        Move(src=p, dst=_dst(p), candidate_id=f"state/handoffs/{p.name}")
        for p in plain_paths
    ] + [
        Move(src=p, dst=_dst(p), candidate_id=f"state/handoffs/{p.name}", restage_src=True)
        for p in restage_paths
    ]
    return moves


def _run_mixed_batch(tmp_path: Path, n_plain: int, n_restage: int) -> int:
    """Seed and archive a batch of `n_plain` + `n_restage` Moves against a fresh
    throwaway repo, returning the total spawn count (`asyncio.create_subprocess_exec`
    plus `subprocess.run`) issued by `archive_and_commit` alone."""
    root = tmp_path / "repo"
    _init_repo(root)
    moves = _seed_moves(root, n_plain, n_restage)

    total, async_spawn, sync_run = _make_total_spawn_counter()
    with patch("asyncio.create_subprocess_exec", side_effect=async_spawn), \
            patch("subprocess.run", side_effect=sync_run):
        acted, failed = asyncio.run(
            archive_and_commit(
                worktree_root=root, moves=moves, subject="fleet: floor-oracle archive",
            )
        )

    assert failed == [], f"fixture must all-succeed for the floor measurement; failed={failed}"
    assert len(acted) == n_plain + n_restage

    return total[0]


@pytest.mark.parametrize("n_plain,n_restage", [(2, 1), (4, 2)])
def test_mixed_batch_spawns_a_constant_two(tmp_path: Path, n_plain: int, n_restage: int) -> None:
    """A mixed batch issues exactly 2 spawns -- batched hash-object + resync -- at both
    M=3 and M=6. Two sizes prove the floor does not scale with M: a reintroduced
    per-Move spawn would make the larger pair diverge further from 2 than the smaller."""
    total = _run_mixed_batch(tmp_path, n_plain, n_restage)
    assert total == 2, (
        f"archive_and_commit issued {total} spawns for a mixed batch of {n_plain} plain + "
        f"{n_restage} restage_src move(s) (M={n_plain + n_restage}) -- the pinned floor is 2 "
        "(one batched hash-object, one resync); a larger count means a per-Move or staging "
        "spawn reappeared. Re-verify against _common.py::archive_and_commit."
    )


def test_all_plain_batch_spawns_only_the_resync(tmp_path: Path) -> None:
    """No restage_src Move -> `_hash_object_stdin_paths` is skipped outright (not run
    with an empty path list); the resync is the only spawn."""
    n_plain = 3
    total = _run_mixed_batch(tmp_path, n_plain, 0)
    assert total == 1, (
        f"an all-plain batch of {n_plain} move(s) issued {total} spawns -- expected 1 "
        "(the resync `git restore --staged`; no hash-object call when no Move opts into "
        "restage_src)"
    )


def test_all_restage_batch_spawns_hash_object_and_resync(tmp_path: Path) -> None:
    """Every Move restage_src -> still ONE batched hash-object plus the resync, not one
    hash-object per Move."""
    n_restage = 3
    total = _run_mixed_batch(tmp_path, 0, n_restage)
    assert total == 2, (
        f"an all-restage batch of {n_restage} move(s) issued {total} spawns -- expected 2 "
        "(one batched hash-object over all restage dsts, one resync)"
    )


def test_oracle_is_not_vacuous_against_a_simulated_per_move_regression(tmp_path: Path) -> None:
    """Proof the floor assertion can fail: replay the mixed-batch scenario but inject one
    EXTRA real spawn per Move immediately after the resync call -- the shape a regression
    back to per-Move git calls would add. The injected total diverges from 2 and the same
    equality check this module asserts elsewhere raises AssertionError."""
    n_plain, n_restage = 2, 1
    m = n_plain + n_restage
    root = tmp_path / "repo"
    _init_repo(root)
    moves = _seed_moves(root, n_plain, n_restage)

    real = asyncio.create_subprocess_exec
    real_run = subprocess.run
    total = [0]

    async def regressed_spawn(*argv, **kwargs):
        total[0] += 1
        proc = await real(*argv, **kwargs)
        if argv[:3] == ("git", "restore", "--staged"):
            for _ in range(m):
                total[0] += 1
                await real(
                    "git", "status", "--porcelain",
                    cwd=str(root), env=kwargs.get("env"),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
        return proc

    def counted_run(*args, **kwargs):
        total[0] += 1
        return real_run(*args, **kwargs)

    with patch("asyncio.create_subprocess_exec", side_effect=regressed_spawn), \
            patch("subprocess.run", side_effect=counted_run):
        acted, failed = asyncio.run(
            archive_and_commit(
                worktree_root=root, moves=moves, subject="fleet: floor-oracle regression probe",
            )
        )
    assert failed == []
    assert len(acted) == m
    assert total[0] == 2 + m, "fixture must have injected exactly one extra spawn per Move"

    with pytest.raises(AssertionError):
        assert total[0] == 2

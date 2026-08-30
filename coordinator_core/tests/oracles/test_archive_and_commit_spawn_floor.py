"""Oracle for the `archive_and_commit` git-mv exemption's spawn FLOOR
(`coordinator_core/ops/fleet/_common.py::archive_and_commit::create_subprocess_exec`).

The 2026-08-19 amplification burn-down batched two of this function's four per-Move
spawns (drift-check `git diff --name-only`, op-authored-content restage `git add`) into
one call each, ahead of the per-Move loop -- see `tasks/amp-census/fix-fleet-archive.md`
and the "Batched drift check + batched restage" comment block in `_common.py` above the
loop. `git mv` (and its failure-path `git reset`) stay genuinely per-Move on
failure-isolation grounds, so the exemption register key survives -- but nothing pinned
the FLOOR the batching bought, which is the whole point of doing it: a later edit could
silently reintroduce a per-Move drift or restage spawn and nothing would go red.

THE CONSTANT, verified against the CURRENT source rather than trusted from the fix
report (which undercounted it by one): for a batch with at least one non-restage Move
and at least one restage_src Move, no failures, `archive_and_commit` issues exactly

    M + 6

`asyncio.create_subprocess_exec` calls, where M is the move count and the constant-6
overhead is:
  1. `git read-tree HEAD`                          -- seed the private index
  2. `git diff --name-only -- <plain srcs>`         -- ONE call, batched drift check
  3. `git add -- <restage srcs>`                    -- ONE call, batched restage
  4. `git write-tree`                               -- `_empty_private_index_breach`,
                                                        unconditional pre-commit refusal
                                                        check. THE FIX REPORT'S OWN "C=5"
                                                        FIGURE OMITTED THIS SPAWN entirely
                                                        -- it is a real, unconditional
                                                        call on every commit path, not a
                                                        failure-path extra. Read directly
                                                        off `_common.py::archive_and_commit`
                                                        (the `index_breach = await
                                                        _empty_private_index_breach(...)`
                                                        line runs before every commit
                                                        attempt) rather than off the report.
  5. `git -c commit.gpgsign=false commit -m <subj>` -- the batch commit
  6. `_resync_main_index_for_moves`'s single `git restore --staged` call (via the
     default `_update_index_with_retry` `run_git`, one attempt on the success path)

`git mv` contributes exactly M of the total and is excluded from the constant --
pinning it at M (not M-1 or M+1) is itself part of what this oracle proves: `git mv`
must stay genuinely per-item, one call per Move, no more and no less.

When the batch is not mixed (all-plain or all-restage), the constant drops by one
(the drift-check or restage-add call is skipped outright, not run with an empty
pathspec) -- see `test_all_plain_batch_drops_the_restage_constant` and
`test_all_restage_batch_drops_the_diff_constant`. The mixed-batch M+6 floor is the
one the team-lead brief asked this oracle to pin, since it is the shape that exercises
every constant-overhead spawn in one call.

Real git throughout (Governed real-git pattern, state/audits/2026-08-07-spawn-heavy-
test-excision-ledger.md): a mocked git has no index to seed/diff/restore against, and
this oracle's whole claim is about the ACTUAL spawn count `archive_and_commit` issues
against a real repo, not a description of it. Every fixture lives under pytest's own
`tmp_path`; nothing here touches the shared working tree.
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
    """Build (total, counting_spawn): counting_spawn wraps the REAL
    asyncio.create_subprocess_exec and counts EVERY call regardless of argv -- this
    oracle's claim is about the total spawn count, not any one call's shape (that finer
    per-key claim is `test_archive_and_commit_batched_drift_and_restage.py`'s job).

    Bare `async def` closure, not a callable class -- `patch("asyncio.
    create_subprocess_exec", side_effect=...)` installs an AsyncMock whose side_effect
    dispatch requires `inspect.iscoroutinefunction`, which a class's `async def
    __call__` fails (verified in the sibling module: the class-based counter left the
    returned coroutine un-awaited). See that module's `_make_spawn_counter` docstring
    for the full failure signature.
    """
    real = asyncio.create_subprocess_exec
    total = [0]

    async def counting_spawn(*argv, **kwargs):
        total[0] += 1
        return await real(*argv, **kwargs)

    return total, counting_spawn


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
    """Seed and archive a mixed batch of `n_plain` + `n_restage` Moves against a fresh
    throwaway repo, returning the total `asyncio.create_subprocess_exec` call count."""
    root = tmp_path / "repo"
    _init_repo(root)
    moves = _seed_moves(root, n_plain, n_restage)

    total, counting_spawn = _make_total_spawn_counter()
    with patch("asyncio.create_subprocess_exec", side_effect=counting_spawn):
        acted, failed = asyncio.run(
            archive_and_commit(
                worktree_root=root, moves=moves, subject="fleet: floor-oracle archive",
            )
        )

    assert failed == [], f"fixture must all-succeed for the floor measurement; failed={failed}"
    assert len(acted) == n_plain + n_restage

    return total[0]


@pytest.mark.parametrize("n_plain,n_restage", [(2, 1), (4, 2)])
def test_mixed_batch_spawns_exactly_m_plus_six(tmp_path: Path, n_plain: int, n_restage: int) -> None:
    """The floor: a mixed batch (both a plain and a restage_src Move present) issues
    exactly M + 6 spawns. Two (n_plain, n_restage) pairs at different M -- (2, 1) giving
    M=3, and (4, 2) giving M=6 -- prove the constant-6 overhead does NOT scale with M:
    if a per-Move drift or restage spawn crept back in, the larger pair's actual count
    would diverge further from its own `M + 6` prediction than the smaller pair's does
    (the extra spawns scale with M, the floor does not), and this assertion would catch
    it on whichever pair exercises the regressed path."""
    m = n_plain + n_restage
    total = _run_mixed_batch(tmp_path, n_plain, n_restage)
    assert total == m + 6, (
        f"archive_and_commit issued {total} spawns for a mixed batch of {n_plain} plain + "
        f"{n_restage} restage_src move(s) (M={m}) -- the pinned floor is M + 6 (read-tree, "
        "batched diff, batched add, write-tree, commit, resync); a count that no longer "
        "matches means either a per-Move drift/restage spawn reappeared in the loop or one "
        "of the six constant-overhead calls changed shape. Re-verify against "
        "_common.py::archive_and_commit rather than this comment."
    )


def test_all_plain_batch_drops_the_restage_constant(tmp_path: Path) -> None:
    """No restage_src Move in the batch -> the batched `git add` is skipped outright
    (not run with an empty pathspec) -- the floor drops to M + 5."""
    n_plain = 3
    total = _run_mixed_batch(tmp_path, n_plain, 0)
    assert total == n_plain + 5, (
        f"an all-plain batch of {n_plain} move(s) issued {total} spawns -- expected "
        f"{n_plain + 5} (M + 5: read-tree, diff, write-tree, commit, resync -- no restage "
        "add call at all when no Move opts into restage_src)"
    )


def test_all_restage_batch_drops_the_diff_constant(tmp_path: Path) -> None:
    """No plain (non-restage_src) Move in the batch -> the batched drift-check `git
    diff` is skipped outright -- the floor drops to M + 5, the mirror case of the test
    above."""
    n_restage = 3
    total = _run_mixed_batch(tmp_path, 0, n_restage)
    assert total == n_restage + 5, (
        f"an all-restage batch of {n_restage} move(s) issued {total} spawns -- expected "
        f"{n_restage + 5} (M + 5: read-tree, add, write-tree, commit, resync -- no drift "
        "diff call at all when every Move opts into restage_src)"
    )


def test_oracle_is_not_vacuous_against_a_simulated_per_move_regression(tmp_path: Path) -> None:
    """Proof the M + 6 assertion above can actually fail: replay the same mixed-batch
    scenario as `test_mixed_batch_spawns_exactly_m_plus_six`, but inject one EXTRA real
    spawn per plain Move immediately after the batched drift-check call returns -- the
    exact shape a regression back to "one drift-check spawn per non-restage Move" would
    add on top of (not instead of) the still-present batched call, e.g. a half-finished
    revert that re-added the per-Move check without removing the pre-loop batch. If the
    M + 6 assertion could not distinguish this from the real batched floor, it would be
    worthless; it cannot -- the injected total diverges from M + 6 and the same equality
    check this module asserts elsewhere raises AssertionError here."""
    n_plain, n_restage = 2, 1
    m = n_plain + n_restage
    root = tmp_path / "repo"
    _init_repo(root)
    moves = _seed_moves(root, n_plain, n_restage)

    real = asyncio.create_subprocess_exec
    total = [0]

    async def regressed_spawn(*argv, **kwargs):
        total[0] += 1
        proc = await real(*argv, **kwargs)
        if len(argv) >= 3 and argv[:3] == ("git", "diff", "--name-only"):
            # Simulate a reintroduced per-Move drift-check spawn: one extra real
            # (harmless, read-only) git call for every plain Move in the batch, run
            # immediately after the legitimate batched call this test also counts.
            for _ in range(n_plain):
                total[0] += 1
                await real(
                    "git", "status", "--porcelain",
                    cwd=str(root), env=kwargs.get("env"),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
        return proc

    with patch("asyncio.create_subprocess_exec", side_effect=regressed_spawn):
        acted, failed = asyncio.run(
            archive_and_commit(
                worktree_root=root, moves=moves, subject="fleet: floor-oracle regression probe",
            )
        )
    assert failed == []
    assert len(acted) == m

    with pytest.raises(AssertionError):
        assert total[0] == m + 6, (
            f"archive_and_commit issued {total[0]} spawns for a mixed batch of {n_plain} plain + "
            f"{n_restage} restage_src move(s) (M={m}) -- the pinned floor is M + 6"
        )

"""
coordinator_core.ops.fleet.tests.test_archive_and_commit_batched_drift_and_restage

Amplification burn-down C4 (2026-08-19): archive_and_commit's disk/HEAD drift
check (`git diff --name-only`) and op-authored-content restage (`git add`)
were each spawning ONE subprocess per Move in the loop — the exemption
register's "git mv one src/dst pair" justification covered only the mv
itself (state/audits/2026-08-19-amplification-register-remaining-fourteen-
dispositions.md). Both are now spawned ONCE per archive_and_commit call,
covering every relevant move's src in a single argv, with per-move
attribution recovered by membership test (drift) or a stored batch-outcome
flag (restage) — see _common.py's "Batched drift check + batched restage"
comment block above the loop.

This module pins BOTH halves of the change: the batching itself (spawn
count, via a counting wrapper around asyncio.create_subprocess_exec — the
functional assertions alone would not catch a regression back to
one-spawn-per-move that happened to still produce the right acted/failed
split) and the correctness it must not have traded away (per-item
attribution, one-item-fails-others-proceed, `git mv` staying genuinely
per-item).

Real git is load-bearing throughout: drift is an index/worktree divergence a
mocked git has no index to diverge from (same argument
test_archive_and_commit_disk_head_drift.py's docstring makes), and restage's
op-authored-content re-keying needs a real private index to observe.

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
    asyncio.create_subprocess_exec, counting calls by argv shape, so a test
    can assert "one spawn for the whole batch" directly rather than
    inferring it from final state alone.

    Key is argv[:3] for `git diff --name-only` / `git add --` (the two
    batched calls this module exists to pin — the third token disambiguates
    them from any other two-token `git diff`/`git add` invocation this
    function might grow later) and argv[:2] for everything else (`git mv`
    must stay genuinely per-item, so its key deliberately does NOT include
    src/dst — counting by ("git", "mv") alone is the point).

    Deliberately a bare `async def` closure, not a callable class instance:
    `patch("asyncio.create_subprocess_exec", side_effect=...)` auto-detects
    the target as a coroutine function and installs an AsyncMock, whose
    side_effect dispatch only recognises `inspect.iscoroutinefunction` —
    true for a plain async function, false for an object's `async def
    __call__` — so a class-based counter would leave the returned coroutine
    un-awaited and produce a bogus `Process` (verified: the callable-class
    version failed every test in this module with `AttributeError:
    'coroutine' object has no attribute 'communicate'`).
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


def test_batched_drift_check_attributes_per_item_and_spawns_once(tmp_path: Path) -> None:
    """3 non-restage moves, one drifted — one `git diff --name-only` spawn
    covers all three; the drifted one alone lands in failed[] with the drift
    reason, the other two are acted on and committed. `git mv` still spawns
    once per successfully-drift-checked move (2), never once per candidate (3)."""
    root = tmp_path / "repo"
    _init_repo(root)
    handoffs = root / "state" / "handoffs"
    handoffs.mkdir(parents=True)

    def _seed(name: str, content: str) -> Path:
        p = handoffs / name
        p.write_text(content, encoding="utf-8")
        return p

    clean1 = _seed(
        "2026-08-01-clean1.md",
        "---\nstatus: claimed\ndeployment_state: shipped\n---\n\nA.\n",
    )
    clean2 = _seed(
        "2026-08-02-clean2.md",
        "---\nstatus: claimed\ndeployment_state: shipped\n---\n\nB.\n",
    )
    drifted = _seed(
        "2026-08-03-drifted.md",
        "---\nstatus: claimed\ndeployment_state: in_flight\n---\n\nC.\n",
    )
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed: three handoffs"], root)

    # Drift: uncommitted disk-only edit, never staged/committed.
    drifted.write_text(
        "---\nstatus: claimed\ndeployment_state: shipped\n---\n\nC.\n", encoding="utf-8",
    )

    def _dst(src: Path) -> Path:
        return root / "archive" / "handoffs" / "2026-08" / src.name

    moves = [
        Move(src=clean1, dst=_dst(clean1), candidate_id="state/handoffs/2026-08-01-clean1.md"),
        Move(src=clean2, dst=_dst(clean2), candidate_id="state/handoffs/2026-08-02-clean2.md"),
        Move(src=drifted, dst=_dst(drifted), candidate_id="state/handoffs/2026-08-03-drifted.md"),
    ]

    counts, counting_spawn = _make_spawn_counter()
    with patch("asyncio.create_subprocess_exec", side_effect=counting_spawn):
        acted, failed = _run(
            archive_and_commit(worktree_root=root, moves=moves, subject="fleet: archive 3 shipped handoff(s)")
        )

    diff_calls = counts.get(("git", "diff", "--name-only"), 0)
    assert diff_calls == 1, (
        f"drift check must spawn ONCE for the whole batch, not per-move; counts={counts}"
    )
    mv_calls = counts.get(("git", "mv"), 0)
    assert mv_calls == 2, f"mv stays genuinely per-item (2 of 3 moves reach it); counts={counts}"

    acted_ids = {a["id"] for a in acted}
    assert acted_ids == {moves[0].candidate_id, moves[1].candidate_id}
    assert len(failed) == 1
    assert failed[0]["id"] == moves[2].candidate_id
    assert "drift" in failed[0]["reason"].lower()

    assert _dst(clean1).exists()
    assert _dst(clean2).exists()
    assert not _dst(drifted).exists()
    assert drifted.exists()


def test_batched_restage_spawns_once_and_stages_all(tmp_path: Path) -> None:
    """2 restage_src=True moves — one `git add --` spawn covers both srcs;
    both land in acted[] and the ARCHIVED content is each src's fresh
    on-disk (post-stamp) content, not the stale read-tree-HEAD blob — proof
    the batched add actually ran before its move's `git mv`."""
    root = tmp_path / "repo"
    _init_repo(root)
    handoffs = root / "state" / "handoffs"
    handoffs.mkdir(parents=True)

    def _seed(name: str, content: str) -> Path:
        p = handoffs / name
        p.write_text(content, encoding="utf-8")
        return p

    heir1 = _seed(
        "2026-08-01-heir1.md",
        "---\nstatus: claimed\ndeployment_state: in_flight\n---\n\nA.\n",
    )
    heir2 = _seed(
        "2026-08-02-heir2.md",
        "---\nstatus: claimed\ndeployment_state: in_flight\n---\n\nB.\n",
    )
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed: two in-flight handoffs"], root)

    # Op-authored pre-move stamp: written to src's OWN file moments before
    # queuing the Move — exactly _stamp_heir_shipped's shape — never staged.
    heir1_stamped = "---\nstatus: claimed\ndeployment_state: shipped\n---\n\nA.\n"
    heir2_stamped = "---\nstatus: claimed\ndeployment_state: shipped\n---\n\nB.\n"
    heir1.write_text(heir1_stamped, encoding="utf-8")
    heir2.write_text(heir2_stamped, encoding="utf-8")

    def _dst(src: Path) -> Path:
        return root / "archive" / "handoffs" / "2026-08" / src.name

    moves = [
        Move(
            src=heir1, dst=_dst(heir1),
            candidate_id="state/handoffs/2026-08-01-heir1.md", restage_src=True,
        ),
        Move(
            src=heir2, dst=_dst(heir2),
            candidate_id="state/handoffs/2026-08-02-heir2.md", restage_src=True,
        ),
    ]

    counts, counting_spawn = _make_spawn_counter()
    with patch("asyncio.create_subprocess_exec", side_effect=counting_spawn):
        acted, failed = _run(
            archive_and_commit(worktree_root=root, moves=moves, subject="fleet: archive 2 shipped handoff(s)")
        )

    add_calls = counts.get(("git", "add", "--"), 0)
    assert add_calls == 1, f"restage add must spawn ONCE for the whole batch; counts={counts}"

    assert failed == []
    assert {a["id"] for a in acted} == {moves[0].candidate_id, moves[1].candidate_id}

    assert _dst(heir1).read_text(encoding="utf-8") == heir1_stamped
    assert _dst(heir2).read_text(encoding="utf-8") == heir2_stamped


def test_restage_batch_failure_fails_all_restage_moves_but_not_unrelated_moves(
    tmp_path: Path,
) -> None:
    """A restage_src move whose src does not exist at all makes the batched
    `git add` fail outright — every restage_src move in THAT call lands in
    failed[] (batch-scoped attribution, same "reason echoed per item" shape
    as the module's existing resync-failure helpers), but an unrelated
    non-restage move in the SAME archive_and_commit call is unaffected."""
    root = tmp_path / "repo"
    _init_repo(root)
    handoffs = root / "state" / "handoffs"
    handoffs.mkdir(parents=True)

    def _seed(name: str, content: str) -> Path:
        p = handoffs / name
        p.write_text(content, encoding="utf-8")
        return p

    heir_ok = _seed(
        "2026-08-01-heir-ok.md",
        "---\nstatus: claimed\ndeployment_state: in_flight\n---\n\nA.\n",
    )
    clean = _seed(
        "2026-08-02-clean.md",
        "---\nstatus: claimed\ndeployment_state: shipped\n---\n\nB.\n",
    )
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed: two handoffs"], root)

    heir_ok_stamped = "---\nstatus: claimed\ndeployment_state: shipped\n---\n\nA.\n"
    heir_ok.write_text(heir_ok_stamped, encoding="utf-8")

    # Never created on disk — `git add -- <this> <heir_ok>` refuses the whole
    # pathspec set before staging anything.
    missing_src = handoffs / "2026-08-03-missing.md"

    def _dst(src: Path) -> Path:
        return root / "archive" / "handoffs" / "2026-08" / src.name

    moves = [
        Move(
            src=heir_ok, dst=_dst(heir_ok),
            candidate_id="state/handoffs/2026-08-01-heir-ok.md", restage_src=True,
        ),
        Move(
            src=missing_src, dst=_dst(missing_src),
            candidate_id="state/handoffs/2026-08-03-missing.md", restage_src=True,
        ),
        Move(src=clean, dst=_dst(clean), candidate_id="state/handoffs/2026-08-02-clean.md"),
    ]

    acted, failed = _run(
        archive_and_commit(worktree_root=root, moves=moves, subject="fleet: archive mixed batch")
    )

    failed_ids = {f["id"] for f in failed}
    assert failed_ids == {moves[0].candidate_id, moves[1].candidate_id}
    for item in failed:
        assert "restage-src-failed" in item["reason"]

    # The unrelated non-restage move in the same call is unaffected.
    assert {a["id"] for a in acted} == {moves[2].candidate_id}
    assert _dst(clean).exists()

    # Nothing from the failed restage batch was archived; the ok heir's
    # stamped content sits untouched on disk (never git-mv'd). Its
    # private-index staged entry (from the batch add, before it errored on
    # the missing sibling) lived only in the temp GIT_INDEX_FILE, which was
    # discarded whole — the MAIN index (what `git status` reads) was never
    # touched by any of these private-index calls, so heir_ok shows only the
    # ordinary unstaged worktree edit its own stamp made, nothing staged.
    assert not _dst(heir_ok).exists()
    assert heir_ok.read_text(encoding="utf-8") == heir_ok_stamped
    status = _git(["status", "--porcelain", "--", "state/handoffs"], root).stdout
    heir_ok_line = next(
        (line for line in status.splitlines() if "2026-08-01-heir-ok.md" in line), None,
    )
    assert heir_ok_line is not None and heir_ok_line.startswith(" M"), (
        f"heir_ok must show only an unstaged worktree edit, no staged residue "
        f"from the failed batch add; status={status!r}"
    )

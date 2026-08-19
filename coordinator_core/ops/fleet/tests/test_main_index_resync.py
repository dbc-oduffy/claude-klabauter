"""
coordinator_core.ops.fleet.tests.test_main_index_resync

Purpose: minimal regression floor for AC5 (docs/plans/2026-08-11-resync-leaves-
a-bare-staged-deletion-whe.md) — coverage for `_resync_main_index_for_moves`
and `_resync_main_index_for_reaps` (coordinator_core/ops/fleet/_common.py,
extracted C4a) that was lost in the 2026-08-07 cull (1d4e686a9, 1440 tests /
77 files deleted after test-spawned git was crashing live EM sessions).

Mostly spawn-free: `run_git` is the seam C4a made injectable, so every argv
assertion here fakes it and never spawns a subprocess. Exactly ONE test uses
the governed `real_git_repo` fixture (coordinator_core/ops/ceremony/tests/
fixtures/real_git.py) to validate the one assumption a fake structurally
cannot: that `git restore --staged` on a path absent from HEAD removes the
index entry rather than erroring.

Spec backlinks:
  - docs/plans/2026-08-11-resync-leaves-a-bare-staged-deletion-whe.md AC2,
    AC3, AC5, AC6
  - state/lessons/2026-08-03-an-interrupted-git-mv-leaves-the-shared-
    907008cbcb3c.yaml (the manual remedy this resync now automates)

Negative-spec:
  - Does NOT restore the culled test_fleet_common.py / test_common_rm_and_
    commit.py / fleet conftest.py / test_index_residue_reproduction.py
    corpus — explicitly out of scope for this chunk (C4b).
  - Does NOT re-export real_git.py through a conftest.py — imported
    explicitly per that module's own docstring.
  - Does NOT spawn git anywhere except the one governed real-git test below.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import List, Optional

import pytest

from coordinator_core.ops.ceremony.tests.fixtures.real_git import real_git_repo
from coordinator_core.ops.fleet._common import (
    Move,
    _resync_main_index_for_moves,
    _resync_main_index_for_reaps,
    _update_index_with_retry,
)

# Real-git spawn is load-bearing, but confined to ONE test (see Part 2's
# own docstring): the one assumption a fake `run_git` structurally cannot
# validate is whether `git restore --staged` on a HEAD-absent path removes
# the index entry rather than erroring -- that needs a real object
# database/index. Every other test above fakes the `run_git` seam and
# spawns nothing; the module-level marker is required because Rule 2 fires
# per-FILE, not per-test. The spawn ratchet's `_BASELINE` is shrink-only
# pre-existing residue and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


class _FakeRunGit:
    """Records every argv it is called with; returns a scripted result per call.

    `results` is consumed in call order (one entry per invocation); when
    exhausted, further calls return None (success). This lets a test script
    "fail on the Nth call" without needing a real subprocess.
    """

    def __init__(self, results: Optional[List[Optional[str]]] = None) -> None:
        self._results = list(results) if results is not None else []
        self.calls: List[dict] = []

    async def __call__(self, argv: List[str], *, cwd: Path, env: dict) -> Optional[str]:
        self.calls.append({"argv": argv, "cwd": cwd, "env": env})
        if self._results:
            return self._results.pop(0)
        return None


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Part 1 — spawn-free fakes: _resync_main_index_for_moves
# ---------------------------------------------------------------------------


def test_moves_resync_issues_one_call_covering_both_paths():
    """AC2 — one path-scoped, index-only restore covering both src and dst."""
    worktree_root = Path("/repo")
    src = worktree_root / "state" / "handoffs" / "a.md"
    dst = worktree_root / "archive" / "handoffs" / "2026-08" / "a.md"
    move = Move(src=src, dst=dst, candidate_id="state/handoffs/a.md")
    acted_by_id = {move.candidate_id: {"id": move.candidate_id, "archived": True}}
    fake = _FakeRunGit()

    _run(
        _resync_main_index_for_moves(
            [move], acted_by_id, worktree_root=worktree_root, env={}, run_git=fake,
        )
    )

    assert len(fake.calls) == 1
    argv = fake.calls[0]["argv"]
    assert argv == ["git", "restore", "--staged", "--", str(src), str(dst)]
    assert fake.calls[0]["cwd"] == worktree_root
    assert "index_resync_failed" not in acted_by_id[move.candidate_id]


def test_moves_resync_skips_candidates_not_in_acted_by_id():
    """A move whose candidate_id is not in acted_by_id issues no call at all."""
    worktree_root = Path("/repo")
    move = Move(
        src=worktree_root / "src.md",
        dst=worktree_root / "dst.md",
        candidate_id="not-tracked.md",
    )
    fake = _FakeRunGit()

    _run(
        _resync_main_index_for_moves(
            [move], {}, worktree_root=worktree_root, env={}, run_git=fake,
        )
    )

    assert fake.calls == []


def test_moves_resync_single_call_shape_means_no_independent_lookup_failure():
    """AC3 — no separate HEAD-blob lookup step exists that can fail
    independently of the removal: the resync issues exactly ONE call over
    both paths, so a lookup-style failure cannot strand a stale src entry
    (the shape that makes AC3 structurally true, not a comment asserting it).
    """
    worktree_root = Path("/repo")
    move = Move(
        src=worktree_root / "src.md",
        dst=worktree_root / "dst.md",
        candidate_id="src.md",
    )
    acted_by_id = {move.candidate_id: {"id": move.candidate_id, "archived": True}}
    fake = _FakeRunGit()

    _run(
        _resync_main_index_for_moves(
            [move], acted_by_id, worktree_root=worktree_root, env={}, run_git=fake,
        )
    )

    # Exactly one call — there is no second, independent call whose failure
    # could strand src's removal while dst's re-add landed (or vice versa).
    assert len(fake.calls) == 1


def test_moves_resync_annotates_index_resync_failed_on_persistent_failure():
    """AC6 — a persistent run_git failure still annotates the acted[] item."""
    worktree_root = Path("/repo")
    move = Move(
        src=worktree_root / "src.md",
        dst=worktree_root / "dst.md",
        candidate_id="src.md",
    )
    acted_by_id = {move.candidate_id: {"id": move.candidate_id, "archived": True}}
    fake = _FakeRunGit(results=["index.lock still held after retries"])

    _run(
        _resync_main_index_for_moves(
            [move], acted_by_id, worktree_root=worktree_root, env={}, run_git=fake,
        )
    )

    item = acted_by_id[move.candidate_id]
    assert item["index_resync_failed"] == (
        "restore-staged-failed: index.lock still held after retries"
    )


def test_moves_resync_argv_is_index_only_never_worktree_writing():
    """Negative assertion: the argv must never contain a worktree-writing
    form — no bare `git restore` without `--staged`, no `git checkout --`,
    no `git read-tree`.
    """
    worktree_root = Path("/repo")
    move = Move(
        src=worktree_root / "src.md",
        dst=worktree_root / "dst.md",
        candidate_id="src.md",
    )
    acted_by_id = {move.candidate_id: {"id": move.candidate_id, "archived": True}}
    fake = _FakeRunGit()

    _run(
        _resync_main_index_for_moves(
            [move], acted_by_id, worktree_root=worktree_root, env={}, run_git=fake,
        )
    )

    argv = fake.calls[0]["argv"]
    assert "--staged" in argv
    assert "checkout" not in argv
    assert "read-tree" not in argv


def test_moves_resync_batches_multiple_items_into_one_call():
    """Review finding 1 (2026-08-19) — N>1 argv-shape coverage. The
    pre-batch implementation issued one `git restore --staged -- src dst`
    call PER move (3 calls for 3 moves here); this asserts exactly ONE call
    whose argv interleaves each move's src/dst pair in input order. Fails
    against the pre-batch loop, which would produce `len(fake.calls) == 3`
    with each argv holding a single src/dst pair.
    """
    worktree_root = Path("/repo")
    moves = [
        Move(src=worktree_root / "a-src.md", dst=worktree_root / "a-dst.md", candidate_id="a.md"),
        Move(src=worktree_root / "b-src.md", dst=worktree_root / "b-dst.md", candidate_id="b.md"),
        Move(src=worktree_root / "c-src.md", dst=worktree_root / "c-dst.md", candidate_id="c.md"),
    ]
    acted_by_id = {
        m.candidate_id: {"id": m.candidate_id, "archived": True} for m in moves
    }
    fake = _FakeRunGit()

    _run(
        _resync_main_index_for_moves(
            moves, acted_by_id, worktree_root=worktree_root, env={}, run_git=fake,
        )
    )

    assert len(fake.calls) == 1
    argv = fake.calls[0]["argv"]
    expected = ["git", "restore", "--staged", "--"]
    for m in moves:
        expected.append(str(m.src))
        expected.append(str(m.dst))
    assert argv == expected
    for m in moves:
        assert "index_resync_failed" not in acted_by_id[m.candidate_id]


def test_moves_resync_batch_failure_annotates_every_item():
    """Review finding 1 (2026-08-19) — pins the docstring's claim ("A batch
    failure is reported against every item in the batch"). With a
    `_FakeRunGit` scripted to fail exactly ONE call, the pre-batch per-move
    loop would fail only the FIRST move's call and succeed the rest (the
    fake's results list is consumed one entry per invocation, returning
    success once exhausted) — so under the pre-batch code only a.md would
    carry `index_resync_failed`. The batched implementation issues one call
    for the whole batch, so that one failure must annotate all three.
    """
    worktree_root = Path("/repo")
    moves = [
        Move(src=worktree_root / "a-src.md", dst=worktree_root / "a-dst.md", candidate_id="a.md"),
        Move(src=worktree_root / "b-src.md", dst=worktree_root / "b-dst.md", candidate_id="b.md"),
        Move(src=worktree_root / "c-src.md", dst=worktree_root / "c-dst.md", candidate_id="c.md"),
    ]
    acted_by_id = {
        m.candidate_id: {"id": m.candidate_id, "archived": True} for m in moves
    }
    fake = _FakeRunGit(results=["index.lock still held after retries"])

    _run(
        _resync_main_index_for_moves(
            moves, acted_by_id, worktree_root=worktree_root, env={}, run_git=fake,
        )
    )

    assert len(fake.calls) == 1
    for m in moves:
        assert acted_by_id[m.candidate_id]["index_resync_failed"] == (
            "restore-staged-failed: index.lock still held after retries"
        )


def test_moves_resync_rename_chain_within_one_batch_not_deduped():
    """Review finding 1 (2026-08-19) — duplicate/rename-chain coverage: one
    move's dst equals another move's src within the SAME batch (a -> b,
    b -> c). Asserts both occurrences of the shared path `b` survive into
    the single batched argv (as move1's dst and move2's src) rather than
    being collapsed/deduped, and that the call is still issued as ONE
    invocation covering both moves. Fails against the pre-batch loop, which
    would issue two separate single-pair calls instead of one call
    containing both occurrences of `b`.
    """
    worktree_root = Path("/repo")
    path_a = worktree_root / "a.md"
    path_b = worktree_root / "b.md"
    path_c = worktree_root / "c.md"
    move1 = Move(src=path_a, dst=path_b, candidate_id="chain-1")
    move2 = Move(src=path_b, dst=path_c, candidate_id="chain-2")
    acted_by_id = {
        move1.candidate_id: {"id": move1.candidate_id, "archived": True},
        move2.candidate_id: {"id": move2.candidate_id, "archived": True},
    }
    fake = _FakeRunGit()

    _run(
        _resync_main_index_for_moves(
            [move1, move2], acted_by_id, worktree_root=worktree_root, env={}, run_git=fake,
        )
    )

    assert len(fake.calls) == 1
    argv = fake.calls[0]["argv"]
    assert argv == ["git", "restore", "--staged", "--", str(path_a), str(path_b), str(path_b), str(path_c)]
    assert argv.count(str(path_b)) == 2
    for cid in acted_by_id:
        assert "index_resync_failed" not in acted_by_id[cid]


# ---------------------------------------------------------------------------
# Part 1 — spawn-free fakes: _resync_main_index_for_reaps
# ---------------------------------------------------------------------------


def test_reaps_resync_single_path_issues_one_remove_call():
    """Single-item case only. Renamed from
    `test_reaps_resync_issues_one_remove_call_per_path` (review finding 1,
    2026-08-19 integration): the old name implied per-path call-issuing
    behavior, but this test's assertion (len(calls) == 1 for a ONE-path
    input) is true both before and after the C4 batching change — it does
    not exercise the batching this module now does. See
    `test_reaps_resync_batches_multiple_paths_into_one_call` below for the
    N>1 coverage that actually distinguishes batched from per-item.
    """
    worktree_root = Path("/repo")
    path = worktree_root / "state" / "handoffs" / "b.md"
    candidate_id = "state/handoffs/b.md"
    reaped_by_id = {candidate_id: {"id": candidate_id, "reaped": True}}
    fake = _FakeRunGit()

    _run(
        _resync_main_index_for_reaps(
            [path], reaped_by_id, worktree_root=worktree_root, env={}, run_git=fake,
        )
    )

    assert len(fake.calls) == 1
    argv = fake.calls[0]["argv"]
    assert argv == ["git", "update-index", "--remove", "--", str(path)]
    assert "index_resync_failed" not in reaped_by_id[candidate_id]


def test_reaps_resync_batches_multiple_paths_into_one_call():
    """Review finding 1 (2026-08-19) — N>1 argv-shape coverage. The
    pre-batch implementation issued one `git update-index --remove --
    <path>` call PER path (3 calls for 3 paths here); this asserts exactly
    ONE call whose argv is the concatenated path list in input order. Fails
    against the pre-batch loop, which would produce `len(fake.calls) == 3`
    with each argv holding a single path.
    """
    worktree_root = Path("/repo")
    paths = [
        worktree_root / "state" / "handoffs" / "b1.md",
        worktree_root / "state" / "handoffs" / "b2.md",
        worktree_root / "state" / "handoffs" / "b3.md",
    ]
    reaped_by_id = {
        "state/handoffs/b1.md": {"id": "state/handoffs/b1.md", "reaped": True},
        "state/handoffs/b2.md": {"id": "state/handoffs/b2.md", "reaped": True},
        "state/handoffs/b3.md": {"id": "state/handoffs/b3.md", "reaped": True},
    }
    fake = _FakeRunGit()

    _run(
        _resync_main_index_for_reaps(
            paths, reaped_by_id, worktree_root=worktree_root, env={}, run_git=fake,
        )
    )

    assert len(fake.calls) == 1
    argv = fake.calls[0]["argv"]
    assert argv == ["git", "update-index", "--remove", "--"] + [str(p) for p in paths]
    for candidate_id in reaped_by_id:
        assert "index_resync_failed" not in reaped_by_id[candidate_id]


def test_reaps_resync_batch_failure_annotates_every_item():
    """Review finding 1 (2026-08-19) — pins the docstring's claim ("A batch
    failure is reported against every relevant item") for the reaps side.
    With a `_FakeRunGit` scripted to fail exactly ONE call, the pre-batch
    per-path loop would fail only the FIRST path's call and succeed the
    rest (results list is consumed one entry per invocation, and the fake
    returns success once exhausted) — so under the pre-batch code only
    b1.md would carry `index_resync_failed`. The batched implementation
    issues one call for the whole batch, so that one failure must annotate
    all three.
    """
    worktree_root = Path("/repo")
    paths = [
        worktree_root / "state" / "handoffs" / "b1.md",
        worktree_root / "state" / "handoffs" / "b2.md",
        worktree_root / "state" / "handoffs" / "b3.md",
    ]
    reaped_by_id = {
        "state/handoffs/b1.md": {"id": "state/handoffs/b1.md", "reaped": True},
        "state/handoffs/b2.md": {"id": "state/handoffs/b2.md", "reaped": True},
        "state/handoffs/b3.md": {"id": "state/handoffs/b3.md", "reaped": True},
    }
    fake = _FakeRunGit(results=["index.lock still held after retries"])

    _run(
        _resync_main_index_for_reaps(
            paths, reaped_by_id, worktree_root=worktree_root, env={}, run_git=fake,
        )
    )

    assert len(fake.calls) == 1
    for candidate_id in reaped_by_id:
        assert reaped_by_id[candidate_id]["index_resync_failed"] == (
            "remove-failed: index.lock still held after retries"
        )


def test_reaps_resync_annotates_index_resync_failed_on_persistent_failure():
    """AC6 — same annotation guarantee on the reaps side."""
    worktree_root = Path("/repo")
    path = worktree_root / "state" / "handoffs" / "b.md"
    candidate_id = "state/handoffs/b.md"
    reaped_by_id = {candidate_id: {"id": candidate_id, "reaped": True}}
    fake = _FakeRunGit(results=["index.lock still held after retries"])

    _run(
        _resync_main_index_for_reaps(
            [path], reaped_by_id, worktree_root=worktree_root, env={}, run_git=fake,
        )
    )

    item = reaped_by_id[candidate_id]
    assert item["index_resync_failed"] == (
        "remove-failed: index.lock still held after retries"
    )


def test_reaps_resync_skips_path_outside_worktree_root():
    """The rel_id ValueError guard for a path outside worktree_root — dropped
    during extraction and restored by the EM; a change that drops it again
    must fail this test, not silently pass.
    """
    worktree_root = Path("/repo")
    outside_path = Path("/elsewhere/c.md")
    reaped_by_id = {"c.md": {"id": "c.md", "reaped": True}}
    fake = _FakeRunGit()

    _run(
        _resync_main_index_for_reaps(
            [outside_path], reaped_by_id, worktree_root=worktree_root, env={}, run_git=fake,
        )
    )

    assert fake.calls == []
    assert "index_resync_failed" not in reaped_by_id["c.md"]


def test_reaps_resync_skips_candidates_not_in_reaped_by_id():
    worktree_root = Path("/repo")
    path = worktree_root / "untracked.md"
    fake = _FakeRunGit()

    _run(
        _resync_main_index_for_reaps(
            [path], {}, worktree_root=worktree_root, env={}, run_git=fake,
        )
    )

    assert fake.calls == []


# ---------------------------------------------------------------------------
# Part 1b — _update_index_with_retry: Windows console-suppression kwarg
# ---------------------------------------------------------------------------
#
# Review: code-reviewer P2 (2026-08-13, distill.apply_disposal integration) —
# _update_index_with_retry is the ONE shared spawn point behind
# archive_and_commit's, rm_and_commit's, AND distill_apply_disposal's
# main-index resync; the fix belongs here, not per call site. Spawn-free:
# monkeypatches asyncio.create_subprocess_exec itself to capture kwargs, no
# real subprocess.


def test_update_index_with_retry_passes_no_console_creationflags(monkeypatch):
    """The subprocess spawn inside _update_index_with_retry must splat
    win_portability.no_console_creationflags(). On real POSIX that mapping is
    {} (inert splat, so a naive assertion would pass trivially with or
    without the fix) — so this test forces the win32 branch via
    win_portability's own documented monkeypatch seam (`_is_windows`) to make
    the assertion load-bearing: it fails on the pre-fix code (no kwarg
    splatted at all) even though it runs on a POSIX CI host.
    """
    import coordinator_core.win_portability as win_portability_mod

    monkeypatch.setattr(win_portability_mod, "_is_windows", lambda: True)

    captured_kwargs: dict = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def _fake_create_subprocess_exec(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create_subprocess_exec
    )

    err = _run(
        _update_index_with_retry(
            ["git", "update-index", "--remove", "--", "x.md"],
            cwd=Path("/repo"),
            env={},
        )
    )

    assert err is None
    assert "creationflags" in captured_kwargs


# ---------------------------------------------------------------------------
# Part 2 — the ONE governed real-git test
# ---------------------------------------------------------------------------


def test_restore_staged_on_head_absent_path_removes_index_entry_real_git(tmp_path):
    """AC5 — the one assumption a fake structurally cannot validate: a
    path-scoped `git restore --staged` of a path ABSENT from HEAD removes
    the index entry rather than erroring. This is exactly the state `src`
    is in after the archival commit lands, and exactly what the C2 ad-hoc
    throwaway-repo check (referenced by the plan) is now a permanent test
    for, rather than a re-discovered answer.

    This is the first of TWO tests in this module that spawn real git (see
    `test_restore_staged_batch_aborts_atomically_on_one_bad_pathspec_real_git`
    below, added 2026-08-19 for review finding 2 — the AC5 coverage this
    chunk builds is unchanged, the batch-atomicity coverage is additional).
    Every other test above fakes the `run_git` seam.
    """
    import subprocess

    root = real_git_repo(tmp_path)

    tracked = root / "tracked.md"
    tracked.write_text("tracked content\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "tracked.md"], cwd=str(root), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add tracked.md"], cwd=str(root), check=True,
    )

    # Remove tracked.md from HEAD via a second commit, so it is now a path
    # genuinely absent from HEAD (the post-archival `src` shape).
    subprocess.run(["git", "rm", "-q", "--", "tracked.md"], cwd=str(root), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "remove tracked.md"], cwd=str(root), check=True,
    )

    # Re-stage tracked.md by hand to simulate the residue: index disagrees
    # with HEAD (HEAD has no entry, index has one) — the exact bare staged
    # deletion this plan exists to close.
    tracked.write_text("resurrected on disk\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "tracked.md"], cwd=str(root), check=True)

    status_before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(root), capture_output=True, text=True, check=True,
    ).stdout
    assert "tracked.md" in status_before

    result = subprocess.run(
        ["git", "restore", "--staged", "--", "tracked.md"],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    status_after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(root), capture_output=True, text=True, check=True,
    ).stdout
    # The worktree file (untracked now that HEAD holds no entry for it) may
    # still show as "??" — but there must be no staged ("A "/"M "/"D ") entry
    # for tracked.md left disagreeing with HEAD.
    for line in status_after.splitlines():
        if line.endswith("tracked.md") and len(line) > 2:
            staged_marker = line[0]
            assert staged_marker in (" ", "?"), (
                f"expected no staged marker for tracked.md, got {line!r}"
            )


def test_restore_staged_batch_aborts_atomically_on_one_bad_pathspec_real_git(tmp_path):
    """Review finding 2 (2026-08-19) — pins the atomicity assumption the
    accepted tradeoff (batch failure marks every item `index_resync_failed`
    even though only one candidate was actually malformed) rests on, with
    REAL git rather than an inferred lockfile-based-write argument. Builds
    ONE genuinely-residue path (`tracked.md`, same shape as the test above)
    plus one pathspec that matches nothing in HEAD or the index at all
    (`nonexistent.md` — never added, never committed: not the normal
    "just-archived, absent-from-HEAD" case that restores cleanly). Runs a
    SINGLE `git restore --staged -- tracked.md nonexistent.md` call — the
    batched shape `_resync_main_index_for_moves` issues — and asserts BOTH:
    (a) the call fails (nonzero rc, "did not match any file(s)" in stderr),
    and (b) tracked.md's staged residue is UNCHANGED afterward — i.e. the
    one bad pathspec aborted the WHOLE batch rather than the good pathspec
    still landing. This is the real-git evidence Finding 2 says was
    previously only inferred from restore/update-index being lockfile-based
    atomic writes.
    """
    import subprocess

    root = real_git_repo(tmp_path)

    tracked = root / "tracked.md"
    tracked.write_text("tracked content\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "tracked.md"], cwd=str(root), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add tracked.md"], cwd=str(root), check=True,
    )
    subprocess.run(["git", "rm", "-q", "--", "tracked.md"], cwd=str(root), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "remove tracked.md"], cwd=str(root), check=True,
    )
    tracked.write_text("resurrected on disk\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "tracked.md"], cwd=str(root), check=True)

    status_before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(root), capture_output=True, text=True, check=True,
    ).stdout
    assert "tracked.md" in status_before

    result = subprocess.run(
        ["git", "restore", "--staged", "--", "tracked.md", "nonexistent.md"],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "did not match any file" in result.stderr

    status_after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(root), capture_output=True, text=True, check=True,
    ).stdout
    # The batch aborted BEFORE writing anything: tracked.md's staged residue
    # from before the call is still present, byte-for-byte, rather than
    # having been (correctly) resolved by the half of the batch that was
    # well-formed. This is the N-1-healthy-items-stranded shape Finding 2
    # names — verified here, not just argued.
    assert status_before == status_after


# ---------------------------------------------------------------------------
# Part 3 — durable sink (tasks/2026-08-11-resync-annotation-sink/SPEC.md AC2, AC4)
# ---------------------------------------------------------------------------
#
# Spawn-free: `run_git` is still the injected fake seam from Part 1. The sink
# itself (`_persist_index_resync_failure`, called via `asyncio.to_thread`) is
# a plain in-process file write (`append_queue_entry`, project-scoped to a
# real `tmp_path` — no git repo required for that call, see `_output_path`'s
# "project scope → caller_worktree" branch) — no subprocess anywhere in
# either test below.


def test_moves_resync_failure_is_recoverable_after_process_exit(tmp_path):
    """AC2 — a resync failure is recoverable after the op process has exited.

    Simulated here by writing the sink to `tmp_path` (a real, on-disk
    location, not an in-memory fake) and then reading it back via a plain
    `Path.read_text()` — i.e. through nothing but the filesystem, the same
    channel a later, separate process would use.
    """
    worktree_root = tmp_path
    move = Move(
        src=worktree_root / "src.md",
        dst=worktree_root / "dst.md",
        candidate_id="src.md",
    )
    acted_by_id = {move.candidate_id: {"id": move.candidate_id, "archived": True}}
    fake = _FakeRunGit(results=["index.lock still held after retries"])

    _run(
        _resync_main_index_for_moves(
            [move], acted_by_id, worktree_root=worktree_root, env={}, run_git=fake,
        )
    )

    bug_backlog_dir = worktree_root / "state" / "bug-backlog"
    assert bug_backlog_dir.is_dir(), (
        f"expected {bug_backlog_dir} to exist after a persistent resync failure"
    )
    written = list(bug_backlog_dir.glob("*.yaml"))
    assert len(written) == 1, f"expected exactly one bug-backlog record, found {written}"

    record_text = written[0].read_text(encoding="utf-8")
    assert "src.md" in record_text
    assert "index.lock still held after retries" in record_text
    assert "index-resync-failed" in record_text


def test_reap_sink_write_failure_does_not_fail_the_op(monkeypatch):
    """AC4 — a sink-write failure must never fail the (already-committed,
    authoritative) archival op. Forces `append_queue_entry` to raise and
    asserts the resync helper still completes normally and still leaves the
    `index_resync_failed` annotation on the wire item.
    """
    import coordinator_core.ops.queue_append as queue_append_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated sink outage")

    monkeypatch.setattr(queue_append_mod, "append_queue_entry", _boom)

    worktree_root = Path("/repo")
    path = worktree_root / "state" / "handoffs" / "b.md"
    candidate_id = "state/handoffs/b.md"
    reaped_by_id = {candidate_id: {"id": candidate_id, "reaped": True}}
    fake = _FakeRunGit(results=["index.lock still held after retries"])

    _run(
        _resync_main_index_for_reaps(
            [path], reaped_by_id, worktree_root=worktree_root, env={}, run_git=fake,
        )
    )

    item = reaped_by_id[candidate_id]
    assert item["index_resync_failed"] == (
        "remove-failed: index.lock still held after retries"
    )

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

from coordinator_core.ops.ceremony.tests.fixtures.real_git import real_git_repo
from coordinator_core.ops.fleet._common import (
    Move,
    _resync_main_index_for_moves,
    _resync_main_index_for_reaps,
)


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


# ---------------------------------------------------------------------------
# Part 1 — spawn-free fakes: _resync_main_index_for_reaps
# ---------------------------------------------------------------------------


def test_reaps_resync_issues_one_remove_call_per_path():
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
# Part 2 — the ONE governed real-git test
# ---------------------------------------------------------------------------


def test_restore_staged_on_head_absent_path_removes_index_entry_real_git(tmp_path):
    """AC5 — the one assumption a fake structurally cannot validate: a
    path-scoped `git restore --staged` of a path ABSENT from HEAD removes
    the index entry rather than erroring. This is exactly the state `src`
    is in after the archival commit lands, and exactly what the C2 ad-hoc
    throwaway-repo check (referenced by the plan) is now a permanent test
    for, rather than a re-discovered answer.

    This is the ONLY test in this module — and the only one in the AC5
    coverage this chunk builds — that spawns real git. Every other test
    above fakes the `run_git` seam.
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

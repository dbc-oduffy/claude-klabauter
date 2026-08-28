"""
coordinator_core.ops.tests.test_check3_succession_exemption

Pins C4 (docs/plans/2026-08-18-supersede-stamps-and-archives-atomically.md):
Check 3 (`archival.reverse_membership`, live children — applied UNCONDITIONALLY
to both terminal-qualifying branches in `archive_handoffs._is_terminal`)
retains a Branch-B-terminal record for as long as ANY live child references
it, without distinguishing a succession child from an arbitrary one. This
drains what C3 (test_supersede_archives_atomically.py) stops accumulating —
a Branch-B `continued` record already stranded on disk whose only live child
is its own successor.

Fix under test: a NARROW, EXPLICIT exemption at the Check-3 call site in
`_is_terminal` — `edge_kinds={"forked_from"}` — applied ONLY when the record
qualifies as terminal via Branch B. A live SUCCESSION child (`predecessor` /
`additional_predecessors`) no longer retains such a record; a live
`forked_from` child still does (DR-224, AC4 — a spinoff founds its own line
and does not retire its origin).

Covers:
  - AC2-shape: a Branch-B `continued` record with only a live succession
    child archives — asserted by the FILE MOVING (absent from
    state/handoffs/, present under archive/handoffs/YYYY-MM/), never by a
    verdict flip alone.
  - AC4: the same record with a live `forked_from` child instead does NOT
    archive.
  - The case `_classify_heir_children`'s short-circuit would have got wrong:
    a record with BOTH a live succession child and a live `forked_from`
    child does NOT archive (the forked_from child must still retain even in
    the presence of a succession child).

Exercises the same DR-324-narrowed Check 3 (now `_scan_terminal`'s
`check3_edge_kinds` gate) through `archive_terminal_handoffs._handler`
(mode="already-terminal", dry_run:false) — the real production re-verify +
git-mv + commit path (`archive_handoffs.py` was killed and rebuilt from
scratch as `archive_terminal_handoffs.py`, 2026-08-23 PM ruling; this test
was repointed at the successor rather than retired because the narrow
succession-vs-forked_from exemption it pins is still live there) — so a
passing test proves the file actually moved, not merely that a helper
returned a different tuple.

Git-free is not reachable here: `archive_and_commit` performs a real
git-mv + commit, which needs a real object database to exhibit honestly.
Governed one-repo-per-test-function pattern (spawn-heavy-test-excision-
ledger ruling), mirroring test_archive_dest_conflict_heir_stamp_restore.py's
own harness.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    # popup-intentional-last-resort — test-only real-git spawn, mirrors the
    # governed real_git.py fixture's own unguarded pattern.
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _run(result):
    """`archive_terminal_handoffs._handler` is SYNC and returns a dict.

    This shim used to be `asyncio.run(coro)`, which had every test in this
    file erroring with "a coroutine was expected, got {...}" — independently
    of, and predating, the 2026-08-28 guard deletion. Kept as a shim rather
    than inlined so the call sites below stay readable.
    """
    if hasattr(result, "__await__"):
        return asyncio.run(result)
    return result


def _init_repo(worktree: Path) -> None:
    worktree.mkdir(parents=True)
    _git(["init", "-q", "-b", "main"], worktree)
    _git(["config", "user.email", "test@example.invalid"], worktree)
    _git(["config", "user.name", "test"], worktree)
    (worktree / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "-A"], worktree)
    _git(["commit", "-q", "-m", "seed"], worktree)


def _seed_continued_predecessor(worktree: Path, name: str) -> Path:
    """A Branch-B-terminal `continued` record — the exact stranded shape C3's
    fix now stops accumulating and C4 drains."""
    handoffs_dir = worktree / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    path = handoffs_dir / name
    path.write_text(
        "---\n"
        "status: claimed\n"
        "title: stranded predecessor\n"
        "deployment_state: continued\n"
        f"continued_into: successor-not-relevant.md\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], worktree)
    _git(["commit", "-q", "-m", f"add {name}"], worktree)
    return path


def _seed_succession_child(worktree: Path, name: str, predecessor: str) -> Path:
    handoffs_dir = worktree / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    path = handoffs_dir / name
    path.write_text(
        "---\n"
        "status: open\n"
        "deployment_state: in_flight\n"
        "title: succession child\n"
        f"predecessor: {predecessor}\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], worktree)
    _git(["commit", "-q", "-m", f"add {name}"], worktree)
    return path


def _seed_forked_from_child(worktree: Path, name: str, origin: str) -> Path:
    handoffs_dir = worktree / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    path = handoffs_dir / name
    path.write_text(
        "---\n"
        "status: open\n"
        "deployment_state: active\n"
        "title: fork child\n"
        'predecessor: "none"\n'
        f'forked_from: "{origin}"\n'
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], worktree)
    _git(["commit", "-q", "-m", f"add {name}"], worktree)
    return path


def _act(worktree: Path, cid: str) -> dict:
    from coordinator_core.ops.fleet import archive_terminal_handoffs

    common_dir = worktree / ".git"
    return _run(archive_terminal_handoffs._handler(
        {
            "mode": "already-terminal",
            "dry_run": False,
            "candidate_ids": [cid],
            "cap": 10,
        },
        repo_root=common_dir,
    ))


def test_continued_with_only_live_succession_child_archives(tmp_path: Path) -> None:
    """A Branch-B `continued` record whose only live child is a SUCCESSION
    child must drain — file moved out of state/handoffs/ into
    archive/handoffs/YYYY-MM/ (AC2-shape: never assert the verdict alone)."""
    worktree = tmp_path / "repo"
    _init_repo(worktree)

    predecessor_name = "2026-08-01-stranded-predecessor.md"
    predecessor_path = _seed_continued_predecessor(worktree, predecessor_name)
    successor_path = _seed_succession_child(
        worktree, "2026-08-02-successor.md", predecessor_name
    )

    cid = f"state/handoffs/{predecessor_name}"

    result = _act(worktree, cid)

    assert result["failed"] == [], result
    assert result["skipped"] == [], result
    assert result["acted"] == [{"id": cid, "archived": True}], result

    assert not predecessor_path.exists(), (
        "a live succession child must NOT retain a Branch-B continued record"
    )
    archived = list((worktree / "archive" / "handoffs").rglob(predecessor_name))
    assert len(archived) == 1, archived


def test_continued_with_only_live_forked_from_child_now_archives(tmp_path: Path) -> None:
    """INVERTED 2026-08-28. This pinned "a live `forked_from` child still
    retains", cited to DR-224 AC4. That citation does not resolve — DR-224
    contains no AC4, and its actual contract makes has-children mean
    SUPERSEDE. Check 3 was deleted on the PM ruling that having a child says
    nothing about whether a baton should be archived; the premise is pinned
    false in tests/test_coverage_dag_archived_repo_root.py."""
    worktree = tmp_path / "repo"
    _init_repo(worktree)

    predecessor_name = "2026-08-01-stranded-predecessor.md"
    predecessor_path = _seed_continued_predecessor(worktree, predecessor_name)
    fork_path = _seed_forked_from_child(
        worktree, "2026-08-02-spinoff.md", predecessor_name
    )

    cid = f"state/handoffs/{predecessor_name}"

    result = _act(worktree, cid)

    assert result["failed"] == [], result
    assert result["skipped"] == [], result
    assert result["acted"] == [{"id": cid, "archived": True}], result

    assert not predecessor_path.exists(), (
        "a live forked_from child must no longer retain the record"
    )
    archived = list((worktree / "archive" / "handoffs").rglob(predecessor_name))
    assert len(archived) == 1, archived


def test_continued_with_both_succession_and_fork_child_now_archives(tmp_path: Path) -> None:
    """INVERTED 2026-08-28, and kept rather than deleted because the SHAPE is
    still worth covering: a record carrying both child kinds at once. Under
    the old narrowing this was the case a naive `_classify_heir_children`
    reuse got wrong. With Check 3 gone neither kind retains, so the record
    archives — but the fixture still exercises the both-kinds path, which is
    where a future partial reintroduction of the guard would show up first."""
    worktree = tmp_path / "repo"
    _init_repo(worktree)

    predecessor_name = "2026-08-01-stranded-predecessor.md"
    predecessor_path = _seed_continued_predecessor(worktree, predecessor_name)
    successor_path = _seed_succession_child(
        worktree, "2026-08-02-successor.md", predecessor_name
    )
    fork_path = _seed_forked_from_child(
        worktree, "2026-08-03-spinoff.md", predecessor_name
    )

    cid = f"state/handoffs/{predecessor_name}"

    result = _act(worktree, cid)

    assert result["failed"] == [], result
    assert result["skipped"] == [], result
    assert result["acted"] == [{"id": cid, "archived": True}], result

    assert not predecessor_path.exists(), (
        "neither child kind retains any more; a record carrying both must "
        "still archive"
    )
    archived = list((worktree / "archive" / "handoffs").rglob(predecessor_name))
    assert len(archived) == 1, archived

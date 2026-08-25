"""
coordinator_core.ops.fleet.tests.test_archive_terminal_handoffs

Tier-T tests for the new `fleet.archive_terminal_handoffs` module (C1a of
docs/plans/2026-08-25-the-handoff-auto-archive-comes-back-capped.md), scoped
narrowly to that module's own file per C2's dispatch brief.

Imports the HANDLER FUNCTIONS directly from
coordinator_core.ops.fleet.archive_terminal_handoffs — NEVER resolved by op
key — because a peer chunk (C1b) is concurrently repointing the registration
from the TEMPORARY key "fleet.archive_terminal_handoffs" to
"fleet.archive_completed_handoffs"; resolving by key here would race that
rename.

One test per AC rail (plan's Acceptance Criteria table):
  - AC-2: a `continued` predecessor with no live child archives.
  - AC-3: each of the four refusal classes (live claim, unresolvable
    shipped_in, live forked_from child, dest-conflict) reports its own named
    `skipped` reason, never a silent drop.
  - AC-4: `cap` bounds moves actually applied and reports the remainder via
    the named "deferred-cap" reason, never silent truncation.
  - AC-5: git spawn count is independent of candidate count (5 vs 50),
    measured by literally counting subprocess invocations (a `wraps=`
    Mock's `.call_count`) — never by static inspection of the source. A
    companion test asserts the batched `git add` pathspec used by the shared
    `archive_and_commit` mover this op reuses unchanged stays scoped
    (never `-A`/`.`), and that the paired commit-plumbing spawns
    (`write-tree`/`commit-tree`/`update-ref`) carry no trailing pathspec of
    their own (staff-eng F3) — the repo-wide
    test_pathspec_less_commit_seams_are_guarded.py already pins the shared
    mover's empty-tree-refusal contract statically; this test pins the
    ACTUAL argv shape for THIS op's own act path, over real git.

Real git spawn is load-bearing throughout (shipped_in SHA reachability,
worktree-dirty exclusion, and the actual archive-and-commit mover all read
real git state) — one throwaway repo per test function, mirroring the
governed pattern in test_archive_shipped_handoffs_live_claim.py and
test_migrate_vocabulary_discharges_archival.py in this same directory.

Negative-spec:
  - Does NOT test the single-flight O_EXCL lock (staff-eng F5) — that is a
    concurrency property, not part of this chunk's AC-2/3/4/5 rail.
  - Does NOT test the `deferred` dry-run preview shape beyond what AC-4
    already exercises via the act path's "deferred-cap" reason.
  - Does NOT re-test AC-1/6/7/8/9/10 — those belong to other chunks of the
    same plan (module deletion, perf budget, entry-point wiring).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.ops.fleet.archive_terminal_handoffs import (
    _REASON_DEST_CONFLICT,
    _handle_act,
    _handler,
    _scan_terminal,
    handoff_archive_dest,
    handoff_claim_dir,
)
from coordinator_core.win_portability import no_console_creationflags

# Real-git spawn is load-bearing (see module docstring) — same convention as
# every other real-git-fixture module in this directory.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_CS_CLAIM_HOLDER_LIVE_PATCH = (
    "coordinator_core.ops.fleet.archive_terminal_handoffs.cs_claim_holder_live"
)

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    # popup-intentional-last-resort — test-only real-git spawn, mirrors this
    # directory's sibling real-git fixtures; no console window risk on the
    # CI/dev platforms this suite runs on.
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, env=_GIT_ENV, timeout=15,
        stdin=subprocess.DEVNULL, **no_console_creationflags(),
    )
    assert result.returncode == 0, (args, result.stdout, result.stderr)
    return result


def _common_dir(repo: Path) -> Path:
    result = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(result.stdout.strip()).resolve()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _seed(repo: Path, name: str, fm_extra: str) -> Path:
    """Write + commit one live handoff under state/handoffs/."""
    path = repo / "state" / "handoffs" / name
    _write(path, f'---\ntitle: "{name}"\ncreated: 2026-01-01\n{fm_extra}\n---\n\nBody.\n')
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return path


def _cid(name: str) -> str:
    return f"state/handoffs/{name}"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _init_repo(root)
    return root


# ---------------------------------------------------------------------------
# AC-2: a `continued` predecessor with no live child archives
# ---------------------------------------------------------------------------


def test_ac2_continued_predecessor_with_no_live_child_archives(repo: Path):
    name = "2026-01-01-continued-parent.md"
    parent = _seed(
        repo, name,
        'status: claimed\ndeployment_state: continued\ncontinued_into: hnd-child-1',
    )
    cid = _cid(name)
    common_dir = _common_dir(repo)

    preview = _run(_handler(
        {"mode": "already-terminal", "dry_run": True, "cap": 10},
        repo_root=common_dir,
    ))
    preview_ids = [c["id"] for c in preview.get("candidates", [])]
    assert cid in preview_ids, (
        f"a continued predecessor with no live child must surface as a "
        f"dry_run candidate; got {preview!r}"
    )

    act = _run(_handler(
        {"mode": "already-terminal", "dry_run": False, "cap": 10, "candidate_ids": [cid]},
        repo_root=common_dir,
    ))
    acted_ids = [item["id"] for item in act.get("acted", [])]
    assert cid in acted_ids, f"expected archival; got {act!r}"
    assert not parent.exists(), "archived handoff must be gone from state/handoffs/"
    archived = list((repo / "archive" / "handoffs").rglob(name))
    assert len(archived) == 1, archived


# ---------------------------------------------------------------------------
# AC-3: each of the four refusal classes reports its own named skipped reason
# ---------------------------------------------------------------------------


def test_ac3_live_claim_refusal_is_named_in_skipped(repo: Path):
    name = "2026-01-02-live-claim.md"
    handoff_path = _seed(repo, name, "status: claimed")
    cid = _cid(name)
    common_dir = _common_dir(repo)

    claim_dir = handoff_claim_dir(common_dir, handoff_path)
    claim_dir.mkdir(parents=True, exist_ok=True)

    with patch(_CS_CLAIM_HOLDER_LIVE_PATCH, return_value=True):
        act = _run(_handle_act("already-terminal", repo, common_dir, [cid], cap=10))

    acted_ids = [item["id"] for item in act.get("acted", [])]
    assert cid not in acted_ids, f"a live claim must retain, not archive; got {act!r}"
    skipped = [s for s in act.get("skipped", []) if s.get("id") == cid]
    assert skipped, f"live-claim refusal must appear in skipped[], not be silently dropped; got {act!r}"
    assert skipped[0].get("reason"), f"skipped entry must carry a named reason; got {skipped!r}"


def test_ac3_unresolvable_shipped_in_refusal_is_named_in_skipped(repo: Path):
    name = "2026-01-03-unresolvable-shipped-in.md"
    _seed(
        repo, name,
        'status: claimed\ndeployment_state: shipped\n'
        'shipped_in: "0000000000000000000000000000000000000000"',
    )
    cid = _cid(name)
    common_dir = _common_dir(repo)

    act = _run(_handle_act("already-terminal", repo, common_dir, [cid], cap=10))

    acted_ids = [item["id"] for item in act.get("acted", [])]
    assert cid not in acted_ids, (
        f"a shipped record whose shipped_in does not resolve must retain "
        f"(fail-closed); got {act!r}"
    )
    skipped = [s for s in act.get("skipped", []) if s.get("id") == cid]
    assert skipped, f"unresolvable-shipped_in refusal must appear in skipped[]; got {act!r}"
    assert skipped[0].get("reason"), f"skipped entry must carry a named reason; got {skipped!r}"


def test_ac3_live_forked_from_child_refusal_is_named_in_skipped(repo: Path):
    parent_name = "2026-01-04-forked-parent.md"
    _seed(repo, parent_name, "status: claimed\ndeployment_state: continued")
    child_name = "2026-01-05-forked-child.md"
    _seed(
        repo, child_name,
        f'status: open\npredecessor: "none"\nforked_from: "{parent_name}"\n'
        'deployment_state: in_flight',
    )
    cid = _cid(parent_name)
    common_dir = _common_dir(repo)

    act = _run(_handle_act("already-terminal", repo, common_dir, [cid], cap=10))

    acted_ids = [item["id"] for item in act.get("acted", [])]
    assert cid not in acted_ids, (
        f"a live forked_from child must retain its parent (DR-324 narrowing); got {act!r}"
    )
    skipped = [s for s in act.get("skipped", []) if s.get("id") == cid]
    assert skipped, f"live-forked_from-child refusal must appear in skipped[]; got {act!r}"
    assert skipped[0].get("reason"), f"skipped entry must carry a named reason; got {skipped!r}"


def test_ac3_dest_conflict_refusal_is_named_in_skipped(repo: Path):
    name = "2026-01-06-dest-conflict.md"
    handoff_path = _seed(repo, name, "status: claimed")
    cid = _cid(name)
    common_dir = _common_dir(repo)

    dst = handoff_archive_dest(repo, handoff_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("a DIFFERENT, pre-existing archive copy\n", encoding="utf-8")

    act = _run(_handle_act("already-terminal", repo, common_dir, [cid], cap=10))

    acted_ids = [item["id"] for item in act.get("acted", [])]
    assert cid not in acted_ids, f"a byte-differing dest conflict must never archive; got {act!r}"
    skipped = [s for s in act.get("skipped", []) if s.get("id") == cid]
    assert skipped, f"dest-conflict refusal must appear in skipped[]; got {act!r}"
    assert skipped[0].get("reason") == _REASON_DEST_CONFLICT, (
        f"dest-conflict must use the dedicated named reason, not a generic one; got {skipped!r}"
    )


# ---------------------------------------------------------------------------
# AC-4: cap bounds moves applied and reports the remainder
# ---------------------------------------------------------------------------


def test_ac4_cap_bounds_moves_and_reports_remainder(repo: Path):
    names = [f"2026-01-{10 + i:02d}-cap-candidate-{i}.md" for i in range(5)]
    for name in names:
        _seed(repo, name, "status: claimed")
    cids = [_cid(name) for name in names]
    common_dir = _common_dir(repo)

    act = _run(_handle_act("already-terminal", repo, common_dir, cids, cap=2))

    acted_ids = [item["id"] for item in act.get("acted", [])]
    assert len(acted_ids) == 2, f"cap=2 must bound moves applied to 2; got {act!r}"
    # Oldest-first: the two earliest-created candidates are the ones applied.
    assert acted_ids == cids[:2], f"cap must apply oldest-first; got {act!r}"

    deferred = [s for s in act.get("skipped", []) if s.get("id") in cids[2:]]
    assert len(deferred) == 3, f"the remaining 3 must be reported, never silently dropped; got {act!r}"
    for item in deferred:
        assert "deferred-cap" in item.get("reason", ""), (
            f"over-cap candidates must be named deferred-cap, not silently truncated; got {item!r}"
        )


# ---------------------------------------------------------------------------
# AC-5: git spawn count is independent of candidate count
# ---------------------------------------------------------------------------


def _seed_bulk_claimed(repo: Path, count: int, prefix: str) -> list:
    cids = []
    for i in range(count):
        name = f"2026-02-{(i % 28) + 1:02d}-{prefix}-{i}.md"
        path = repo / "state" / "handoffs" / name
        _write(path, f'---\ntitle: "{name}"\ncreated: 2026-01-01\nstatus: claimed\n---\n\nBody.\n')
        cids.append(_cid(name))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"seed {count} handoffs")
    return cids


def test_ac5_spawn_count_independent_of_candidate_count(tmp_path_factory):
    counts_seen = {}
    for n in (5, 50):
        root = tmp_path_factory.mktemp(f"ac5-repo-{n}")
        _init_repo(root)
        _seed_bulk_claimed(root, n, "candidate")
        common_dir = _common_dir(root)

        with patch("subprocess.run", wraps=subprocess.run) as spy:
            candidates = _run(_scan_terminal(root, common_dir))
        assert len(candidates) == n, (
            f"fixture sanity: expected {n} scanned candidates, got {len(candidates)}"
        )
        counts_seen[n] = spy.call_count

    assert counts_seen[5] > 0, (
        f"spy recorded zero subprocess.run invocations — patch target is wrong; got {counts_seen!r}"
    )
    assert counts_seen[5] == counts_seen[50], (
        f"git spawn count must be independent of candidate count (AC-5), "
        f"counted by real subprocess invocations, not by inspection; got {counts_seen!r}"
    )


# ---------------------------------------------------------------------------
# F3: the act-path mover's git add/commit-plumbing pathspec stays scoped
# ---------------------------------------------------------------------------


def test_f3_git_add_and_commit_plumbing_pathspec_scoping(repo: Path):
    name = "2026-01-20-pathspec-scoping.md"
    _seed(repo, name, "status: claimed")
    cid = _cid(name)
    common_dir = _common_dir(repo)

    captured_argv = []
    real_exec = asyncio.create_subprocess_exec

    async def _spy_exec(*args, **kwargs):
        captured_argv.append(list(args))
        return await real_exec(*args, **kwargs)

    with patch("asyncio.create_subprocess_exec", _spy_exec):
        act = _run(_handle_act("already-terminal", repo, common_dir, [cid], cap=10))

    acted_ids = [item["id"] for item in act.get("acted", [])]
    assert cid in acted_ids, f"setup sanity: candidate must archive cleanly; got {act!r}"

    add_calls = [argv for argv in captured_argv if len(argv) >= 2 and argv[1] == "add"]
    assert add_calls, f"expected at least one `git add` spawn; captured {captured_argv!r}"
    for argv in add_calls:
        assert "-A" not in argv, f"git add must never use -A; got {argv!r}"
        assert "." not in argv, f"git add must never use a bare '.'; got {argv!r}"
        assert "--" in argv, f"git add must scope via an explicit '--' pathspec; got {argv!r}"

    commit_plumbing_calls = [
        argv for argv in captured_argv
        if len(argv) >= 2 and argv[1] in ("write-tree", "commit-tree", "update-ref", "commit")
    ]
    assert commit_plumbing_calls, (
        f"expected at least one commit-plumbing spawn; captured {captured_argv!r}"
    )
    for argv in commit_plumbing_calls:
        assert "--" not in argv, (
            f"commit-plumbing spawn must carry no trailing pathspec — the private "
            f"index IS the scope (FORWARD-B); got {argv!r}"
        )
        assert "-A" not in argv and "." not in argv, f"got {argv!r}"

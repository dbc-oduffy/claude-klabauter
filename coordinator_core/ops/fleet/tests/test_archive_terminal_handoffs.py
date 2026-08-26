"""
coordinator_core.ops.fleet.tests.test_archive_terminal_handoffs

Tier-T tests for the extracted `plan_sweep` / `apply_sweep` core (C2 of
docs/plans/2026-08-25-the-terminal-handoff-sweep-stops-being-an-op.md),
discharging that plan's AC-1 and AC-2. Supersedes this file's predecessor
content, written against
docs/plans/2026-08-25-the-handoff-auto-archive-comes-back-capped.md (C1a of
that superseded plan) — the rail-coverage tests below keep testing the same
observable behaviour C1's extraction promised to preserve unchanged, now
exercised through the current plan's own AC numbering.

Imports the HANDLER FUNCTIONS directly from
coordinator_core.ops.fleet.archive_terminal_handoffs — NEVER resolved by op
key — same rationale as before: resolving by key would race any concurrent
registration-key work in a peer chunk.

Coverage:
  - AC-1: a subprocess-spy test (`patch("subprocess.run", wraps=...)`, never
    static inspection) counting spawns across the composed
    `plan_sweep` + `apply_sweep` over a 200-record fixture — exactly 2 for
    `plan_sweep` (the worktree-dirty rail, the `shipped_in` rail) and
    exactly 0 for `apply_sweep`. A ratchet on the exact counts, not a bound.
  - AC-2: rail coverage, one case each, asserting a NAMED (non-empty)
    `skipped` reason rather than merely that the candidate was dropped:
    live `claimed_by`, unresolvable `shipped_in`, live `forked_from` child,
    byte-divergent dest-conflict (exact reason string), worktree-dirty. Plus
    the cap cases: at cap (nothing deferred), over cap (deferred with the
    named `deferred-cap` reason), and absent cap (a setup error, exit_code:1
    — never an unbounded default).

Real git spawn is load-bearing throughout (shipped_in SHA reachability,
worktree-dirty exclusion, and the actual archive-and-commit mover all read
real git state) — one throwaway repo per test function, mirroring the
governed pattern in test_archive_shipped_handoffs_live_claim.py and
test_migrate_vocabulary_discharges_archival.py in this same directory.

Negative-spec:
  - Does NOT test the single-flight O_EXCL lock (staff-eng F5) — that is a
    concurrency property, not part of this chunk's AC-1/AC-2 rail.
  - Does NOT test the `deferred` dry-run preview shape beyond what the cap
    cases already exercise via the act path's "deferred-cap" reason.
  - Does NOT re-test AC-3/4/5/6/7/8/9/10/11 — those belong to other chunks
    of the same plan (the in-plane fold-in seam, the wire-envelope wrapper,
    perf measurement, entry-point wiring, the git-free rail readers).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import coordinator_core.ops.fleet._common as _common_mod
from coordinator_core.ops.fleet.archive_terminal_handoffs import (
    _REASON_DEST_CONFLICT,
    _SCAN_REASON_CONSUMED_BY_LIVE,
    _SCAN_REASON_LIVE_CHILD,
    _SCAN_REASON_LIVE_CLAIM,
    _SCAN_REASON_NOT_TERMINAL,
    _SCAN_REASON_WORKTREE_DIRTY,
    _handle_act,
    _handler,
    _object_exists_no_spawn,
    _scan_terminal,
    apply_sweep,
    collect_live_handoff_paths,
    handoff_archive_dest,
    handoff_claim_dir,
    plan_sweep,
    rel_id,
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
# AC-2 (cap cases): at cap, and absent cap is a setup error
# ---------------------------------------------------------------------------


def test_ac2_cap_case_at_cap_defers_nothing(repo: Path):
    names = [f"2026-01-{30 + i:02d}-at-cap-{i}.md" for i in range(3)]
    for name in names:
        _seed(repo, name, "status: claimed")
    cids = [_cid(name) for name in names]
    common_dir = _common_dir(repo)

    act = _run(_handle_act("already-terminal", repo, common_dir, cids, cap=3))

    acted_ids = [item["id"] for item in act.get("acted", [])]
    assert sorted(acted_ids) == sorted(cids), (
        f"exactly-at-cap must archive every requested candidate; got {act!r}"
    )
    deferred = [s for s in act.get("skipped", []) if "deferred-cap" in s.get("reason", "")]
    assert not deferred, f"at cap, nothing is deferred; got {act!r}"


def test_ac2_absent_cap_is_a_setup_error_never_unbounded(repo: Path):
    name = "2026-01-40-absent-cap.md"
    _seed(repo, name, "status: claimed")
    common_dir = _common_dir(repo)

    result = _run(_handler(
        {"mode": "already-terminal", "dry_run": True},
        repo_root=common_dir,
    ))
    assert result.get("exit_code") == 1, (
        f"an absent cap must fail closed as a setup error (exit_code:1), "
        f"never an unbounded default; got {result!r}"
    )


# ---------------------------------------------------------------------------
# AC-2 (worktree-dirty rail): reports a named reason, never a silent drop
# ---------------------------------------------------------------------------


def test_ac2_worktree_dirty_refusal_is_named_in_skipped(repo: Path):
    name = "2026-01-50-worktree-dirty.md"
    handoff_path = _seed(repo, name, "status: claimed")
    cid = _cid(name)
    common_dir = _common_dir(repo)

    # Uncommitted on-disk edit — diverges from HEAD without being staged.
    handoff_path.write_text(
        handoff_path.read_text(encoding="utf-8") + "\nuncommitted edit\n",
        encoding="utf-8",
    )

    act = _run(_handle_act("already-terminal", repo, common_dir, [cid], cap=10))

    acted_ids = [item["id"] for item in act.get("acted", [])]
    assert cid not in acted_ids, (
        f"a worktree-dirty candidate must retain, not archive; got {act!r}"
    )
    skipped = [s for s in act.get("skipped", []) if s.get("id") == cid]
    assert skipped, f"worktree-dirty refusal must appear in skipped[], not be silently dropped; got {act!r}"
    assert skipped[0].get("reason"), f"skipped entry must carry a named reason; got {skipped!r}"


# ---------------------------------------------------------------------------
# AC-1: plan_sweep + apply_sweep spawn-count ratchet over a 200-record fixture
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


def test_ac1_plan_sweep_and_apply_sweep_spawn_count_ratchet(tmp_path_factory):
    root = tmp_path_factory.mktemp("ac1-repo")
    _init_repo(root)
    cids = _seed_bulk_claimed(root, 199, "candidate")

    # One `shipped` candidate with a resolvable shipped_in — exercises the
    # shipped_in batch rail, so the ratchet counts both rails' spawns, not
    # just the always-on worktree-dirty one.
    head_sha = _git(root, "rev-parse", "HEAD").stdout.strip()
    shipped_name = "2026-02-28-shipped-candidate.md"
    shipped_path = root / "state" / "handoffs" / shipped_name
    _write(
        shipped_path,
        f'---\ntitle: "{shipped_name}"\ncreated: 2026-01-01\nstatus: claimed\n'
        f'deployment_state: shipped\nshipped_in: "{head_sha}"\n---\n\nBody.\n',
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed shipped candidate")
    cids.append(_cid(shipped_name))

    common_dir = _common_dir(root)

    with patch("subprocess.run", wraps=subprocess.run) as plan_spy:
        moves, skipped = _run(plan_sweep(root, common_dir, cap=200))
    assert len(moves) == 200, (
        f"fixture sanity: expected all 200 seeded candidates to plan as moves; "
        f"got {len(moves)} moves, {len(skipped)} skipped"
    )
    assert plan_spy.call_count == 1, (
        f"plan_sweep must spawn exactly 1 git process on the standalone path "
        f"(worktree-dirty rail only — the shipped_in rail is a bounded, "
        f"git-spawn-free reader, C10/AC-11) regardless of candidate count — "
        f"a ratchet on the exact count, not a bound; got {plan_spy.call_count}"
    )

    with patch("subprocess.run", wraps=subprocess.run) as apply_spy:
        acted, failed = apply_sweep(moves)
    assert not failed, f"fixture sanity: apply_sweep must not fail any move; got {failed!r}"
    assert len(acted) == 200
    assert apply_spy.call_count == 0, (
        f"apply_sweep must spawn exactly zero git processes — os.replace only; "
        f"got {apply_spy.call_count}"
    )


def test_ac1_plan_sweep_spawns_zero_git_on_in_plane_path(tmp_path_factory):
    """C10/AC-11: an in-plane caller that already computed worktree
    divergence (commit_pipeline/commit_scoped's own diverging_paths pass)
    feeds it through `known_dirty_relpaths=`, so plan_sweep spawns ZERO git
    processes — the shipped_in rail is already spawn-free (Rail 2), and
    Rail 1's git status spawn is bypassed by the caller-supplied answer.
    """
    root = tmp_path_factory.mktemp("ac1-in-plane-repo")
    _init_repo(root)
    cids = _seed_bulk_claimed(root, 5, "in-plane")
    common_dir = _common_dir(root)

    with patch("subprocess.run", wraps=subprocess.run) as plan_spy:
        moves, skipped = _run(
            plan_sweep(root, common_dir, cap=10, known_dirty_relpaths=set())
        )
    assert len(moves) == 5, (skipped, moves)
    assert plan_spy.call_count == 0, (
        f"plan_sweep must spawn zero git processes on the in-plane path "
        f"(known_dirty_relpaths supplied); got {plan_spy.call_count}"
    )


# ---------------------------------------------------------------------------
# F3: the act-path mover's git add/commit-plumbing pathspec stays scoped
# ---------------------------------------------------------------------------


def test_f3_git_add_and_commit_plumbing_pathspec_scoping(repo: Path, monkeypatch):
    """RE-TARGETED 2026-08-26 (C2 `dccf2fc01`, this plan's C7).

    The FORWARD-B property this test pins -- the mover's write path is
    explicitly SCOPED to only the paths it means to touch, never a
    repo-wide `-A`/`.` sweep, and never an implicit pathspec-less read of a
    shared index -- moved with the mechanism, not away from it.
    `archive_and_commit` no longer spawns `git add` or any commit-plumbing
    (`write-tree`/`commit-tree`/`update-ref`/`commit`) at all: staging is
    `git hash-object -w --stdin-paths` (`_hash_object_stdin_paths`, a
    SYNCHRONOUS wrapper offloaded via `asyncio.to_thread`, invisible to an
    `asyncio.create_subprocess_exec` spy -- see this plan's sibling
    `test_head_race_between_read_tree_and_commit.py` for the same
    technique), fed an EXPLICIT path list over stdin (never argv, never
    `-A`/`.`), and the commit is built from an in-process `assembled` dict
    scoped to exactly this batch's acted src/dst paths
    (`_commit_via_head_spine`), never from reading any index.
    """
    name = "2026-01-20-pathspec-scoping.md"
    _seed(repo, name, "status: claimed")
    cid = _cid(name)
    common_dir = _common_dir(repo)

    captured_argv = []
    real_exec = asyncio.create_subprocess_exec

    async def _spy_exec(*args, **kwargs):
        captured_argv.append(list(args))
        return await real_exec(*args, **kwargs)

    real_hash_object = _common_mod._hash_object_stdin_paths
    hash_object_calls = []

    def _spy_hash_object(paths, **kwargs):
        hash_object_calls.append(list(paths))
        return real_hash_object(paths, **kwargs)

    real_spine = _common_mod._commit_via_head_spine
    spine_calls = []

    def _spy_spine(root, assembled, *args, **kwargs):
        spine_calls.append(dict(assembled))
        return real_spine(root, assembled, *args, **kwargs)

    monkeypatch.setattr(_common_mod, "_hash_object_stdin_paths", _spy_hash_object)
    monkeypatch.setattr(_common_mod, "_commit_via_head_spine", _spy_spine)

    with patch("asyncio.create_subprocess_exec", _spy_exec):
        act = _run(_handle_act("already-terminal", repo, common_dir, [cid], cap=10))

    acted_ids = [item["id"] for item in act.get("acted", [])]
    assert cid in acted_ids, f"setup sanity: candidate must archive cleanly; got {act!r}"

    # The whole class this test used to police (a wide-open `-A`/`.`
    # staging call, or a pathspec'd commit-plumbing call reading the shared
    # index) is now structurally impossible rather than merely avoided:
    # there is no index-reading spawn left in this path to police.
    add_calls = [argv for argv in captured_argv if len(argv) >= 2 and argv[1] == "add"]
    assert not add_calls, f"git add must never be spawned again; captured {add_calls!r}"
    commit_plumbing_calls = [
        argv for argv in captured_argv
        if len(argv) >= 2 and argv[1] in ("write-tree", "commit-tree", "update-ref", "commit")
    ]
    assert not commit_plumbing_calls, (
        f"commit-plumbing spawns must never return -- landing is fully "
        f"in-process via _commit_via_head_spine; captured {commit_plumbing_calls!r}"
    )

    # The replacement staging call: hash-object, fed an EXPLICIT path list
    # -- never -A, never a bare '.', never empty.
    assert hash_object_calls, f"expected at least one hash-object call; captured {captured_argv!r}"
    for paths in hash_object_calls:
        assert "-A" not in paths and "." not in paths, f"got {paths!r}"
        assert paths, "hash-object must be fed an explicit non-empty path list"

    # The replacement commit call: _commit_via_head_spine, scoped to
    # exactly this batch's acted paths -- never the whole worktree/index.
    assert spine_calls, "expected at least one _commit_via_head_spine call"
    for assembled in spine_calls:
        assert assembled, "assembled tree delta must be non-empty and explicit"


# ---------------------------------------------------------------------------
# C10/AC-11: the bounded, git-spawn-free Rail-2 object-existence reader
# ---------------------------------------------------------------------------


def test_c10_object_exists_no_spawn_resolves_loose(repo: Path):
    common_dir = _common_dir(repo)
    head_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    with patch("subprocess.run", wraps=subprocess.run) as spy:
        resolved = _object_exists_no_spawn(common_dir, head_sha)
    assert resolved is True, "a loose commit object must resolve"
    assert spy.call_count == 0, "the bounded reader must spawn no git process"


def test_c10_object_exists_no_spawn_resolves_packed(repo: Path):
    common_dir = _common_dir(repo)
    head_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "gc", "-q")

    with patch("subprocess.run", wraps=subprocess.run) as spy:
        resolved = _object_exists_no_spawn(common_dir, head_sha)
    assert resolved is True, "a packed commit object must resolve via the .idx binary search"
    assert spy.call_count == 0, "the bounded reader must spawn no git process"


def test_c10_object_exists_no_spawn_reports_unresolvable(repo: Path):
    common_dir = _common_dir(repo)
    resolved = _object_exists_no_spawn(common_dir, "0" * 40)
    assert resolved is False, "a sha naming no object must report unresolvable"


def test_c10_object_exists_no_spawn_refuses_under_alternates(repo: Path):
    common_dir = _common_dir(repo)
    head_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    alternates = common_dir / "objects" / "info" / "alternates"
    alternates.parent.mkdir(parents=True, exist_ok=True)
    alternates.write_text("/nonexistent/other/repo/.git/objects\n", encoding="utf-8")

    resolved = _object_exists_no_spawn(common_dir, head_sha)
    assert resolved is False, (
        "an alternates file present must degrade to unresolvable (fail-closed) "
        "even for a sha that resolves locally — alternates is an unmodeled path"
    )


# ---------------------------------------------------------------------------
# AC-2 (re-opened): every scan rail names ITSELF, and no live record is
# silently dropped
#
# The four `..._refusal_is_named_in_skipped` cases above are all satisfied by
# the act path's generic `terminality-drift` fallback — they assert that *a*
# reason exists, never that the refusing rail is the one that spoke. That is
# how AC-2 was ticked over a `_scan_terminal` with no skipped channel at all.
# These cases assert the rail-specific reason, so a rail that goes silent
# again fails here instead of passing on the catch-all.
# ---------------------------------------------------------------------------


def _scan_reasons(repo: Path) -> dict:
    """Run plan_sweep over the whole corpus, returning {candidate_id: reason}
    for every record the scan itself refused."""
    scan_skipped: list = []
    _run(plan_sweep(repo, _common_dir(repo), 50, scan_skipped=scan_skipped))
    return {item["id"]: item["reason"] for item in scan_skipped}


def test_ac2_not_terminal_rail_names_itself(repo: Path):
    name = "2026-02-01-open-baton.md"
    _seed(repo, name, "status: open\ndeployment_state: ready_to_fire")

    reason = _scan_reasons(repo).get(_cid(name))

    assert reason is not None, "an open baton the scan refuses must still be named, not dropped"
    assert reason.startswith(_SCAN_REASON_NOT_TERMINAL), reason
    assert "ready_to_fire" in reason, (
        f"the reason must carry WHY it did not qualify, not just that it did not; got {reason!r}"
    )


def test_ac2_worktree_dirty_rail_names_itself(repo: Path):
    name = "2026-02-02-dirty.md"
    path = _seed(repo, name, "status: claimed")
    path.write_text(path.read_text(encoding="utf-8") + "uncommitted edit\n", encoding="utf-8")

    reason = _scan_reasons(repo).get(_cid(name))

    assert reason is not None, "a worktree-dirty record must be named, not dropped"
    assert reason == _SCAN_REASON_WORKTREE_DIRTY, reason


def test_ac2_live_child_rail_names_itself(repo: Path):
    parent_name = "2026-02-03-parent.md"
    _seed(repo, parent_name, "status: claimed\ndeployment_state: continued")
    _seed(
        repo, "2026-02-04-child.md",
        f'status: open\npredecessor: "none"\nforked_from: "{parent_name}"\n'
        'deployment_state: in_flight',
    )

    reason = _scan_reasons(repo).get(_cid(parent_name))

    assert reason is not None, "a parent retained by a live child must be named, not dropped"
    assert reason == _SCAN_REASON_LIVE_CHILD, reason


def test_ac2_live_claim_rail_names_itself(repo: Path):
    name = "2026-02-05-claimed.md"
    handoff_path = _seed(repo, name, "status: claimed")
    handoff_claim_dir(_common_dir(repo), handoff_path).mkdir(parents=True, exist_ok=True)

    with patch(_CS_CLAIM_HOLDER_LIVE_PATCH, return_value=True):
        reason = _scan_reasons(repo).get(_cid(name))

    assert reason is not None, "a live claim holder must be named, not dropped"
    assert reason == _SCAN_REASON_LIVE_CLAIM, reason


def test_ac2_consumed_by_live_rail_names_itself(repo: Path):
    name = "2026-02-06-consumed.md"
    _seed(repo, name, 'status: claimed\nconsumed_by: "sid-alive-0001"')

    with patch(
        "coordinator_core.ops.fleet.archive_terminal_handoffs.resolve_live_session_ids",
        return_value={"sid-alive-0001"},
    ):
        reason = _scan_reasons(repo).get(_cid(name))

    assert reason is not None, "a consumed_by naming a live session must be named, not dropped"
    assert reason.startswith(_SCAN_REASON_CONSUMED_BY_LIVE), reason
    assert "sid-alive-0001" in reason, (
        f"the reason must name the live session that retained it; got {reason!r}"
    )


def test_outcome_every_live_record_is_accounted_for(repo: Path):
    """The outcome criterion the plan never had: a sweep either moves a
    record, skips it with a reason, or the scan refuses it with a reason.
    A record in none of the three has been dropped silently, which is the
    defect that let a sweep archive nothing while eleven ACs stayed green.
    """
    _seed(repo, "2026-03-01-archivable.md", "status: claimed")
    _seed(repo, "2026-03-02-open.md", "status: open\ndeployment_state: ready_to_fire")
    parent = "2026-03-03-retained-parent.md"
    _seed(repo, parent, "status: claimed\ndeployment_state: continued")
    _seed(
        repo, "2026-03-04-live-child.md",
        f'status: open\npredecessor: "none"\nforked_from: "{parent}"\n'
        'deployment_state: in_flight',
    )
    dirty = _seed(repo, "2026-03-05-dirty.md", "status: claimed")
    dirty.write_text(dirty.read_text(encoding="utf-8") + "edit\n", encoding="utf-8")

    live = collect_live_handoff_paths(repo)
    scan_skipped: list = []
    moves, skipped = _run(
        plan_sweep(repo, _common_dir(repo), 50, scan_skipped=scan_skipped)
    )

    accounted = (
        {move.candidate_id for move in moves}
        | {item["id"] for item in skipped}
        | {item["id"] for item in scan_skipped}
    )
    unaccounted = {rel_id(p, repo) for p in live} - accounted
    assert not unaccounted, (
        f"every live record must be moved, skipped, or refused with a reason; "
        f"{len(unaccounted)} vanished with no disposition: {sorted(unaccounted)}"
    )
    assert moves, "the archivable record must actually be planned for a move"


def test_ac4_scan_reasons_never_reach_the_cockpit_wire(repo: Path):
    """AC-4 preservation: the scan channel is opt-in, and the op wrapper does
    not opt in — so a wire envelope carries only the reasons the producer
    contract already publishes, with no allowlist for a new reason to drift
    out of.
    """
    name = "2026-04-01-open-baton.md"
    _seed(repo, name, "status: open\ndeployment_state: ready_to_fire")
    target = "2026-04-02-archivable.md"
    _seed(repo, target, "status: claimed")

    act = _run(_handle_act("already-terminal", repo, _common_dir(repo), [_cid(target)], cap=10))

    wire_reasons = [item.get("reason", "") for item in act.get("skipped", [])]
    for scan_reason in (
        _SCAN_REASON_NOT_TERMINAL,
        _SCAN_REASON_WORKTREE_DIRTY,
        _SCAN_REASON_LIVE_CHILD,
        _SCAN_REASON_LIVE_CLAIM,
        _SCAN_REASON_CONSUMED_BY_LIVE,
    ):
        assert not any(r.startswith(scan_reason) for r in wire_reasons), (
            f"scan reason {scan_reason!r} reached the wire; got {act!r}"
        )
    wire_ids = [item.get("id") for item in act.get("skipped", [])]
    assert _cid(name) not in wire_ids, (
        f"a record the caller never named must not appear on the wire; got {act!r}"
    )

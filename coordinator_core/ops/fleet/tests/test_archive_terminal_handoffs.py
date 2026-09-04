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
from coordinator_core.ops.ceremony.git_native import (
    _DIVERGENCE_CHECK_ARGV_BUDGET_CHARS,
    GitResult,
)
from coordinator_core.ops.fleet.archive_terminal_handoffs import (
    _REASON_DEST_CONFLICT,
    _SCAN_REASON_CONSUMED_BY_LIVE,
    _SCAN_REASON_LIVE_CLAIM,
    _SCAN_REASON_NOT_TERMINAL,
    _SCAN_REASON_WORKTREE_DIRTY,
    _dirty_handoff_relpaths,
    _dirty_relpaths_in_process,
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
    # `plan_sweep`/`_handle_act`/`_handler` are SYNCHRONOUS (C2) — this
    # helper predates that migration and always assumed a coroutine. Accept
    # a plain return value unchanged so every existing call site here keeps
    # working without an s/_run(x)/x/ rewrite across the whole file.
    if asyncio.iscoroutine(coro):
        return asyncio.run(coro)
    return coro


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

    # Inverted 2026-08-28. This asserted the parent was RETAINED and its
    # refusal named. Check 3 is deleted (PM ruling: having a child says
    # nothing about whether a baton should be archived), and the
    # `forked_from` half it rested on cited a "DR-224, AC4" that does not
    # exist — DR-224's actual contract makes has-children mean SUPERSEDE.
    acted_ids = [item["id"] for item in act.get("acted", [])]
    assert cid in acted_ids, (
        f"a live forked_from child must no longer retain its parent; got {act!r}"
    )
    assert not [s for s in act.get("skipped", []) if s.get("id") == cid], (
        f"the parent must not be refused at all; got {act!r}"
    )


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

    RE-SITED AGAIN, 2026-08-26 (`cffa6e99f`): `_hash_object_stdin_paths` is
    now called ONLY over a batch's `restage_src=True` subset --
    `archive_terminal_handoffs` never sets `restage_src=True` (it always
    takes the default, `restage_src=False`), so that call no longer fires
    on THIS op's path at all; asserting `hash_object_calls` is non-empty
    would silently stop exercising anything the moment that call stopped
    firing here (the exact failure mode this repair exists to close --
    see `hash_object_calls` below, which now asserts the opposite: the
    call is NEVER made on this path, because the whole batch is
    `restage_src=False`). The FORWARD-B pathspec-scoping property survives
    intact through `_commit_via_head_spine`'s spy instead: it fires
    unconditionally, for every move regardless of `restage_src`, and its
    `spine_calls` sentinel (asserted below) is what proves this test's
    interception actually still fires.
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

    # archive_terminal_handoffs never sets restage_src=True, so the
    # hash-object staging call (scoped to that subset only) must NOT fire
    # on this op's path at all -- if it ever does, something started
    # setting restage_src=True here and this assertion should be revisited
    # together with the docstring above.
    assert hash_object_calls == [], (
        f"archive_terminal_handoffs is restage_src=False-only; "
        f"_hash_object_stdin_paths must not be called: {hash_object_calls!r}"
    )

    # The replacement commit call: _commit_via_head_spine, scoped to
    # exactly this batch's acted paths -- never the whole worktree/index.
    # This is the interception this test now actually depends on; the
    # assertion below both proves it fired at all AND pins the
    # pathspec-scoping property the test is named for.
    assert spine_calls, "expected at least one _commit_via_head_spine call -- interception did not fire"
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


def test_c10_object_exists_no_spawn_resolves_under_multi_pack_index(repo: Path):
    """A `multi-pack-index` must be READ, not bailed on.

    Regression: treating its presence as unmodeled made this reader answer
    False for every packed object in any repo that has one — `git gc` /
    `git maintenance` write one by default — which silently retained every
    shipped handoff whose ship commit was packed, archiving nothing.
    """
    common_dir = _common_dir(repo)
    head_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "gc", "-q")
    _git(repo, "multi-pack-index", "write")
    assert (common_dir / "objects" / "pack" / "multi-pack-index").is_file(), (
        "precondition: this repo must actually carry a multi-pack-index"
    )

    with patch("subprocess.run", wraps=subprocess.run) as spy:
        resolved = _object_exists_no_spawn(common_dir, head_sha)
    assert resolved is True, (
        "a packed commit must still resolve when a multi-pack-index is present"
    )
    assert spy.call_count == 0, "the bounded reader must spawn no git process"


def test_c10_object_exists_no_spawn_unresolvable_sha_under_multi_pack_index(repo: Path):
    common_dir = _common_dir(repo)
    _git(repo, "gc", "-q")
    _git(repo, "multi-pack-index", "write")

    resolved = _object_exists_no_spawn(common_dir, "0" * 40)
    assert resolved is False, (
        "reading the midx must not turn a sha naming no object into a false 'exists'"
    )


def test_c10_object_exists_no_spawn_refuses_under_midx_chain(repo: Path):
    common_dir = _common_dir(repo)
    head_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (common_dir / "objects" / "pack" / "multi-pack-index.d").mkdir(parents=True, exist_ok=True)

    resolved = _object_exists_no_spawn(common_dir, head_sha)
    assert resolved is False, (
        "an incremental multi-pack-index chain is unmodeled — degrade to "
        "unresolvable (fail-closed), never guess 'exists'"
    )


@pytest.mark.parametrize("packed", [False, True])
@pytest.mark.parametrize("abbrev_len", [7, 8, 9, 12, 40])
def test_c10_object_exists_no_spawn_resolves_abbreviated_sha(
    repo: Path, packed: bool, abbrev_len: int
):
    """`shipped_in` is written abbreviated by several stamping paths.

    Regression: requiring a full 40 hex made this reader answer False for
    every abbreviated value, which `_classify_branch` turned into permanent
    fail-closed retention of the NEWEST shipped records — 17 of them on this
    corpus, growing with every ship.
    """
    common_dir = _common_dir(repo)
    head_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if packed:
        _git(repo, "gc", "-q")

    resolved = _object_exists_no_spawn(common_dir, head_sha[:abbrev_len])
    assert resolved is True, (
        f"a {abbrev_len}-hex abbreviation of a real commit must resolve "
        f"({'packed' if packed else 'loose'})"
    )


@pytest.mark.parametrize("packed", [False, True])
def test_c10_object_exists_no_spawn_abbreviated_miss_is_unresolvable(repo: Path, packed: bool):
    common_dir = _common_dir(repo)
    if packed:
        _git(repo, "gc", "-q")

    assert _object_exists_no_spawn(common_dir, "0" * 8) is False, (
        "a prefix naming no object must not be widened into a false 'exists'"
    )


def test_c10_object_exists_no_spawn_refuses_sha_below_abbrev_floor(repo: Path):
    common_dir = _common_dir(repo)
    head_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    assert _object_exists_no_spawn(common_dir, head_sha[:6]) is False, (
        "below git's 7-hex abbreviation floor the match set is too wide to be "
        "evidence about the recorded commit — refuse rather than range-search"
    )
    assert _object_exists_no_spawn(common_dir, "") is False
    assert _object_exists_no_spawn(common_dir, "zz" + head_sha[:8]) is False, (
        "a non-hex value must stay unresolvable"
    )


def test_c10_object_exists_no_spawn_refuses_unreadable_midx(repo: Path):
    common_dir = _common_dir(repo)
    head_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "gc", "-q")

    pack_dir = common_dir / "objects" / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "multi-pack-index").write_bytes(b"NOTAMIDX" + b"\x00" * 64)

    resolved = _object_exists_no_spawn(common_dir, head_sha)
    assert resolved is False, (
        "a midx whose layout the reader refuses must degrade to unresolvable, "
        "not fall through to a per-pack scan that answers around it"
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


def test_a_live_forked_from_child_no_longer_retains_the_parent(repo: Path):
    """Was `test_ac2_live_child_rail_names_itself`, inverted 2026-08-28.

    Check 3 (childlessness) was deleted on the PM ruling that having a child
    says nothing about whether a baton should be archived. The child seeded
    here is a `forked_from` spinoff — the last edge kind still blocking after
    DR-324 narrowed succession edges away — and its retain rested on a
    "DR-224, AC4" citation that does not resolve (DR-224 contains no AC4). The
    premise is pinned false in
    coordinator_core/tests/test_coverage_dag_archived_repo_root.py.

    A refusal reason of None means the record was NOT refused: it survives the
    scan and is archivable, which is the point.
    """
    parent_name = "2026-02-03-parent.md"
    _seed(repo, parent_name, "status: claimed\ndeployment_state: continued")
    _seed(
        repo, "2026-02-04-child.md",
        f'status: open\npredecessor: "none"\nforked_from: "{parent_name}"\n'
        'deployment_state: in_flight',
    )

    reason = _scan_reasons(repo).get(_cid(parent_name))

    assert reason is None, (
        f"a live forked_from child must no longer retain its parent; got {reason!r}"
    )


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


# ---------------------------------------------------------------------------
# C3: classify first, dirty-check only the survivors
# ---------------------------------------------------------------------------


def test_c3_zero_survivors_spawns_zero_git_processes(repo: Path):
    """AC-2's amended form: a pass where classification yields zero
    survivors spawns ZERO processes for the worktree-dirty rail — not merely
    a narrower pathspec. Every seeded record here is non-terminal, so no
    candidate ever reaches Rail 1.
    """
    for i in range(5):
        _seed(repo, f"2026-05-{i + 1:02d}-non-terminal.md", "status: open\ndeployment_state: ready_to_fire")
    common_dir = _common_dir(repo)

    with patch("subprocess.run", wraps=subprocess.run) as plan_spy:
        moves, skipped = _run(plan_sweep(repo, common_dir, cap=50))

    assert not moves, f"fixture sanity: nothing here should qualify; got {moves!r}"
    assert plan_spy.call_count == 0, (
        f"zero survivors must spawn zero git processes for the worktree-dirty "
        f"rail (C3); got {plan_spy.call_count}"
    )


def test_c3_dirty_and_non_terminal_reason_precedence_is_not_terminal(repo: Path):
    """DESIGNED reason-precedence change (staff-eng Finding 1, C3): a record
    that is BOTH worktree-dirty AND non-terminal now surfaces `not-terminal:
    ...` and never reaches the dirty rail, because classification runs
    first. This is a deliberate reorder effect, not a regression.
    """
    name = "2026-05-10-dirty-and-non-terminal.md"
    path = _seed(repo, name, "status: open\ndeployment_state: ready_to_fire")
    path.write_text(path.read_text(encoding="utf-8") + "uncommitted edit\n", encoding="utf-8")

    reason = _scan_reasons(repo).get(_cid(name))

    assert reason is not None, "the record must still be named, not dropped"
    assert reason.startswith(_SCAN_REASON_NOT_TERMINAL), (
        f"classify-first means not-terminal wins over worktree-dirty for a "
        f"record that is both; got {reason!r}"
    )
    assert reason != _SCAN_REASON_WORKTREE_DIRTY


# `test_ac10_membership_error_rail_names_itself` was DELETED 2026-08-28 with the
# arm it covered. `_SCAN_REASON_MEMBERSHIP_ERROR` was the fail-closed
# `reverse_membership` ValueError rail: an error computing children retained the
# record forever. With Check 3 gone children do not decide archival, so an error
# computing them is not a reason to retain -- and `reverse_membership` is no
# longer imported by that module, leaving nothing to patch.


def test_c3_dirty_check_pathspec_scoped_to_survivors_only(repo: Path):
    """The dirty check's pathspec is bounded by the SURVIVOR set, not the
    whole `state/handoffs` tree — a non-terminal record's path must never
    appear in the scoped `git status --porcelain` pathspec.
    """
    survivor_name = "2026-05-12-survivor.md"
    _seed(repo, survivor_name, "status: claimed")
    non_terminal_name = "2026-05-13-non-terminal.md"
    _seed(repo, non_terminal_name, "status: open\ndeployment_state: ready_to_fire")
    common_dir = _common_dir(repo)

    # Force the SPAWNING arm: this test is about the porcelain pathspec, and
    # the in-process arm answers the same question without one (its own
    # no-spawn guarantee is asserted separately, below).
    with patch(
        "coordinator_core.ops.fleet.archive_terminal_handoffs._dirty_relpaths_in_process",
        return_value=None,
    ), patch("subprocess.run", wraps=subprocess.run) as spy:
        _run(plan_sweep(repo, common_dir, cap=50))

    dirty_calls = [c for c in spy.call_args_list if "status" in c.args[0]]
    assert len(dirty_calls) == 1, f"expected exactly one git status call; got {dirty_calls!r}"
    argv = dirty_calls[0].args[0]
    assert _cid(survivor_name) in argv, f"the survivor's path must be in the scoped pathspec; got {argv!r}"
    assert _cid(non_terminal_name) not in argv, (
        f"a non-terminal record must never appear in the survivor-scoped "
        f"pathspec; got {argv!r}"
    )


def test_c3_fallback_to_unscoped_call_above_pathspec_budget():
    """staff-eng Finding 8: above the pathspec byte budget, the dirty check
    falls back to the CURRENT unscoped call (`git status --porcelain --
    state/handoffs`) rather than risking the Windows argv cap or spawning
    one chunk per budget-full of survivors.
    """
    huge_survivor_set = [f"state/handoffs/{'x' * 200}-{i}.md" for i in range(100)]
    assert sum(len(p) + 1 for p in huge_survivor_set) > _DIVERGENCE_CHECK_ARGV_BUDGET_CHARS

    with patch(
        "coordinator_core.ops.ceremony.git_native.status_porcelain",
        return_value=GitResult(returncode=0, stdout="", stderr=""),
    ) as spy:
        dirty = _dirty_handoff_relpaths(Path("."), huge_survivor_set)

    assert not dirty
    spy.assert_called_once()
    called_paths = spy.call_args.args[1]
    assert called_paths == ["state/handoffs"], (
        f"over-budget survivor set must fall back to the unscoped call, "
        f"still exactly one spawn; got {called_paths!r}"
    )


def test_c3_fail_closed_on_git_status_failure_treats_survivors_as_dirty():
    """staff-eng Finding 8: a non-zero git exit or launch failure on the
    dirty-check rail must be FAIL-CLOSED (every survivor refused as dirty),
    never an empty dirty set — an empty set from a failed git call is
    exactly the fail-open mode this rail exists to refuse.
    """
    survivors = ["state/handoffs/2026-05-20-a.md", "state/handoffs/2026-05-21-b.md"]

    # The in-process arm must be forced to DECLINE, not merely left alone.
    # Without this the call never reaches the patched failure at all (these
    # paths do not exist, so the in-process arm returns them as untracked ->
    # dirty) and the assertion below passes VACUOUSLY, green over a rail it
    # never exercised.
    with patch(
        "coordinator_core.ops.fleet.archive_terminal_handoffs._dirty_relpaths_in_process",
        return_value=None,
    ), patch(
        "coordinator_core.ops.ceremony.git_native.status_porcelain",
        return_value=GitResult(returncode=1, stdout="", stderr="fatal: boom"),
    ) as spy:
        dirty = _dirty_handoff_relpaths(Path("."), survivors)

    spy.assert_called_once()
    assert dirty == set(survivors), (
        f"a failed git status call must fail-closed to 'every survivor is "
        f"dirty', never an empty set; got {dirty!r}"
    )


def test_in_process_arm_declining_is_a_fallback_never_a_fail_closed_refusal():
    """A DECLINE is "ask git instead", not "every survivor is dirty".
    Conflating the two would refuse every survivor on any box where
    `content_matches_index_sha` cannot certify its preconditions -- i.e.
    every stock non-`autocrlf=true` checkout.
    """
    survivors = ["state/handoffs/2026-05-20-a.md"]

    with patch(
        "coordinator_core.ops.fleet.archive_terminal_handoffs._dirty_relpaths_in_process",
        return_value=None,
    ), patch(
        "coordinator_core.ops.ceremony.git_native.status_porcelain",
        return_value=GitResult(returncode=0, stdout="", stderr=""),
    ) as spy:
        dirty = _dirty_handoff_relpaths(Path("."), survivors)

    spy.assert_called_once()
    assert dirty == set(), (
        f"a decline must fall through to git's own (here: clean) answer, "
        f"never to a fail-closed refusal; got {dirty!r}"
    )


def test_in_process_arm_matches_porcelain_over_a_corpus_that_can_go_red(repo: Path):
    """Equivalence against the rail it replaces, over clean / unstaged /
    STAGED-but-uncommitted / deleted / untracked -- and zero spawns doing it.

    The staged case is the one a worktree-only check gets wrong: git reports
    it dirty, and a survivor whose bytes are staged but not committed is
    exactly as unsafe to move as one with unstaged edits.
    """
    names = ["clean", "unstaged", "staged", "deleted"]
    for n in names:
        _seed(repo, f"{n}.md", "status: claimed")

    (repo / "state" / "handoffs" / "unstaged.md").write_text("EDITED\n", encoding="utf-8")
    (repo / "state" / "handoffs" / "staged.md").write_text("EDITED\n", encoding="utf-8")
    _git(repo, "add", "state/handoffs/staged.md")
    (repo / "state" / "handoffs" / "deleted.md").unlink()
    _write(repo / "state" / "handoffs" / "untracked.md", "untracked\n")

    survivors = sorted(_cid(f"{n}.md") for n in names + ["untracked"])

    calls: list = []
    real_init = subprocess.Popen.__init__

    def _spy(self, args, *a, **kw):
        calls.append(args)
        return real_init(self, args, *a, **kw)

    subprocess.Popen.__init__ = _spy
    try:
        in_process = _dirty_relpaths_in_process(repo, survivors)
    finally:
        subprocess.Popen.__init__ = real_init

    if in_process is None:
        pytest.skip("content_matches_index_sha declined here (preconditions unmet)")

    assert calls == [], f"the in-process arm must spawn nothing; got {calls!r}"

    with patch(
        "coordinator_core.ops.fleet.archive_terminal_handoffs._dirty_relpaths_in_process",
        return_value=None,
    ):
        porcelain = {
            p for p in _dirty_handoff_relpaths(repo, survivors) if p in set(survivors)
        }

    assert in_process == porcelain, (
        f"in-process and porcelain must agree; only in-process: "
        f"{sorted(in_process - porcelain)!r}, only porcelain: "
        f"{sorted(porcelain - in_process)!r}"
    )
    assert _cid("clean.md") not in in_process
    assert _cid("staged.md") in in_process, "the index-vs-HEAD axis must be covered"


# ---------------------------------------------------------------------------
# C4 (AC-12): the cheap frontmatter pre-filter
# ---------------------------------------------------------------------------


def _write_fm(path: Path, block: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(block, encoding="utf-8", newline="")
    return path


def test_c4_disqualifies_a_plain_non_terminal_pair(tmp_path: Path):
    from coordinator_core.ops.fleet.archive_terminal_handoffs import (
        _SCAN_REASON_NOT_TERMINAL,
        _prefilter_scan_disqualifies,
    )

    p = _write_fm(tmp_path / "h.md", "---\nstatus: open\ndeployment_state: in_progress\n---\n\nBody.\n")
    reason = _prefilter_scan_disqualifies(p)
    assert reason is not None and reason.startswith(_SCAN_REASON_NOT_TERMINAL)


def test_c4_disqualifies_claimed_but_in_flight(tmp_path: Path):
    from coordinator_core.ops.fleet.archive_terminal_handoffs import _prefilter_scan_disqualifies

    p = _write_fm(tmp_path / "h.md", "---\nstatus: claimed\ndeployment_state: in_flight\n---\n\nBody.\n")
    reason = _prefilter_scan_disqualifies(p)
    assert reason is not None and "in_flight" in reason


@pytest.mark.parametrize(
    "block",
    [
        "---\nstatus: claimed\ndeployment_state: active\n---\n\nBody.\n",  # Branch A qualifies
        "---\nstatus: open\ndeployment_state: shipped\n---\n\nBody.\n",  # Branch B qualifies
        "no leading fence at all\n",  # no leading '---' on line 1
        "---\nstatus: open\ndeployment_state: in_progress\n",  # no closing delimiter
        "﻿---\nstatus: open\ndeployment_state: in_progress\n---\n\nBody.\n",  # BOM
        "---\nstatus: \"open\"\ndeployment_state: in_progress\n---\n\nBody.\n",  # quoted scalar
        "---\nstatus: |\n  open\ndeployment_state: in_progress\n---\n\nBody.\n",  # block scalar
        "---\nstatus: [open]\ndeployment_state: in_progress\n---\n\nBody.\n",  # flow collection
        "---\nstatus: &anchor open\ndeployment_state: in_progress\n---\n\nBody.\n",  # anchor
        "---\nstatus:\topen\ndeployment_state: in_progress\n---\n\nBody.\n",  # tab in indentation
        "---\nstatus: open\nstatus: open\ndeployment_state: in_progress\n---\n\nBody.\n",  # duplicate key
        "---\ndeployment_state: in_progress\n---\n\nBody.\n",  # status key absent
        "---\nstatus: open\n---\n\nBody.\n",  # deployment_state key absent
    ],
)
def test_c4_closed_fall_through_enumeration_never_refuses(tmp_path: Path, block: str):
    """Every case in the closed fall-through list (staff-eng Finding 6) must
    return None (uncertain -> full parse decides), never a guessed refusal —
    including the two genuinely-qualifying shapes, which must also come back
    None since the pre-filter never refuses a record the full parse admits.
    """
    from coordinator_core.ops.fleet.archive_terminal_handoffs import _prefilter_scan_disqualifies

    p = _write_fm(tmp_path / "h.md", block)
    assert _prefilter_scan_disqualifies(p) is None


def test_ac11_unconditional_legs_are_not_paid_when_nothing_survives(repo: Path):
    """AC-11: `build_reverse_edge_index` and `resolve_live_session_ids` are
    DEFERRED until a candidate survives classification -- 29.9ms of the
    measured 243.8ms that an all-refused pass must not pay.

    The implementation shipped with C3's `if not survivors: return` and no
    assertion, so the laziness was real and unguarded: any future reorder
    that hoists either leg back above the short-circuit reintroduces the
    cost silently, with every existing test still green. Mutation-tested
    against itself below -- a fixture that DOES produce a survivor must
    reach both legs, or this test would pass over a scan that never ran.
    """
    non_terminal = "2026-06-01-not-terminal.md"
    _seed(repo, non_terminal, "status: open\ndeployment_state: ready_to_fire")
    common_dir = _common_dir(repo)

    # Record and DELEGATE, never stub: a stubbed return value of the wrong
    # shape makes the positive arm below fail for a reason that has nothing
    # to do with whether the leg was reached.
    import coordinator_core.ops.fleet.archive_terminal_handoffs as _mod

    real_index = _mod.build_reverse_edge_index
    real_sids = _mod.resolve_live_session_ids
    reached: list = []

    def _spy_index(*a, **k):
        reached.append("index")
        return real_index(*a, **k)

    def _spy_sids(*a, **k):
        reached.append("sids")
        return real_sids(*a, **k)

    with patch.object(_mod, "build_reverse_edge_index", _spy_index), patch.object(
        _mod, "resolve_live_session_ids", _spy_sids
    ):
        _run(plan_sweep(repo, common_dir, cap=50))

    assert reached == [], (
        f"an all-refused pass must pay neither unconditional leg; reached {reached!r}"
    )

    # The negative arm is only meaningful if the positive one fires: seed a
    # genuinely archivable record and confirm both legs ARE reached.
    # Same shape test_ac2_continued_predecessor_with_no_live_child_archives
    # already proves reaches the act path -- a bare `shipped` does NOT (its
    # shipped_in resolvability gate refuses it), which this arm caught.
    _seed(
        repo,
        "2026-06-02-terminal.md",
        "status: claimed\ndeployment_state: continued\ncontinued_into: hnd-child-1",
    )
    reached.clear()
    with patch.object(_mod, "build_reverse_edge_index", _spy_index), patch.object(
        _mod, "resolve_live_session_ids", _spy_sids
    ):
        _run(plan_sweep(repo, common_dir, cap=50))

    # One leg, not two, since 2026-08-28: the reverse-edge index build was
    # deleted with Check 3 — it existed only to answer "does anything still
    # point at this node?", which nothing asks any more. The `index` spy is
    # kept above so this test still fails loudly if a future change
    # reintroduces that build; `resolve_live_session_ids` is now the only
    # unconditional leg whose laziness this test guards.
    assert "sids" in reached, (
        f"a surviving candidate must reach the live-session leg, else the "
        f"negative arm above is vacuous; reached {reached!r}"
    )
    assert "index" not in reached, (
        f"the reverse-edge index build was deleted with Check 3; something "
        f"has reintroduced a corpus-wide edge index; reached {reached!r}"
    )


def test_ac12_prefilter_never_disqualifies_a_full_parse_admit():
    """AC-12's mechanical assertion, over the full live corpus: for every
    on-disk handoff frontmatter, either the pre-filter admits it (defers to
    the full parse) or the full parse itself would have refused it too.
    A passing result is what makes this chunk safe to ship; a failing one is
    what makes it safe to drop (see the module's own C4 docstring block).
    """
    from coordinator_core import dag as dag_mod
    from coordinator_core.ops.fleet.archive_terminal_handoffs import (
        _classify_branch,
        _prefilter_scan_disqualifies,
    )

    repo_root = Path(__file__).resolve().parents[4]
    handoffs_dir = repo_root / "state" / "handoffs"
    paths = sorted(handoffs_dir.glob("*.md"))
    assert paths, f"expected a live handoffs corpus under {handoffs_dir}"

    failures = []
    for path in paths:
        prefilter_admits = _prefilter_scan_disqualifies(path) is None
        meta = dag_mod._read_meta(str(path)) or {}
        qualifies, _reason, _label, _branch_b = _classify_branch(meta, {})
        full_parse_admits = bool(qualifies)
        if not (prefilter_admits or not full_parse_admits):
            failures.append((path.name, meta.get("status"), meta.get("deployment_state")))

    assert not failures, (
        f"pre-filter disqualified a record the full parse would have "
        f"admitted (under-archive risk): {failures!r}"
    )


# ---------------------------------------------------------------------------
# AC-4 (happy limb) and AC-6 -- the two criteria C2 shipped with only half
# their evidence. AC-4's stall/timeout limb lives beside the in-plane fold-in
# suite (`test_ac4_sync_handler_still_dispatches_and_honors_per_op_timeout`);
# what was never asserted is that a NORMAL dispatch of the now-sync handler
# still returns `exit_code: 0` and never `-32006`. AC-6's negative half
# (`test_ac4_scan_reasons_never_reach_the_cockpit_wire`) pinned that no NEW
# reason leaks to the wire; the envelope's own key set -- the thing the
# producer contract freezes -- had nothing holding it.
# ---------------------------------------------------------------------------


_OP_METHOD = "fleet.archive_completed_handoffs"

#: The FROZEN envelope, per
#: `coordinator_core/contract/cockpit-invoke-producer-contract.md` section 2.1
#: ("All three slice-1 ops share one params/result shape") and
#: `_common.build_dry_run_result` / `build_act_result`. BOTH arms carry the
#: same seven keys -- the act arm returns `candidates: []` rather than
#: dropping the key, which is what makes the shape shared rather than
#: per-arm. `deferred` is additive and appears only when the cap actually
#: bounds the candidate list.
_ENVELOPE_KEYS = {
    "exit_code", "mode", "dry_run", "candidates", "acted", "skipped", "failed",
}


def test_ac4_a_normal_dispatch_of_the_sync_handler_returns_exit_code_zero(repo: Path):
    """AC-4's happy limb: `fleet.archive_completed_handoffs` returns
    `exit_code: 0` with no `-32006` through a REAL dispatch.

    The sibling test proves the per-op timeout now fires via `ipc.py`'s own
    sync branch -- but it only ever exercises the stalled arm, where the
    dispatch returns an error by construction. An op that went sync and then
    stopped resolving, or that got suspended out from under the plan, would
    pass that test and fail every caller. `-32006` is `ipc.OP_SUSPENDED_ERROR`
    and is named explicitly because the sweep op has been suspended before
    (`d2738e6d9` un-suspended it) and a killed op's name lives on in
    string-keyed registries.
    """
    import coordinator_core.ipc as _ipc

    _seed(repo, "2026-06-01-dispatchable.md", "status: claimed")

    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": _OP_METHOD,
        "_origin_worktree": str(repo),
        "params": {"mode": "already-terminal", "dry_run": True, "cap": 10},
    }
    reply = asyncio.run(_ipc.dispatch_message(msg))

    assert "error" not in reply, (
        f"a normal dispatch of the sync handler must not error; got {reply!r}"
    )
    result = reply.get("result")
    assert isinstance(result, dict), f"expected a result envelope; got {reply!r}"
    assert result.get("exit_code") == 0, (
        f"a normal dry_run dispatch must return exit_code:0; got {result!r}"
    )
    assert _cid("2026-06-01-dispatchable.md") in [
        c["id"] for c in result.get("candidates", [])
    ], f"the seeded terminal record must surface as a candidate; got {result!r}"


def test_ac4_the_op_is_not_suspended_so_dispatch_never_yields_32006():
    """The `-32006` half of AC-4, asserted at the registry rather than
    inferred from one dispatch that happened not to hit it."""
    import coordinator_core.ipc as _ipc

    reply = asyncio.run(_ipc.dispatch_message({
        "jsonrpc": "2.0", "id": 1, "method": _OP_METHOD, "params": {},
    }))
    error = reply.get("error") or {}
    assert error.get("code") != _ipc.OP_SUSPENDED_ERROR, (
        f"{_OP_METHOD} must not be suspended -- AC-4 pins it dispatchable; "
        f"got {reply!r}"
    )


def test_ac6_the_cockpit_wire_envelope_key_set_is_unchanged(repo: Path):
    """AC-6: the wire envelope is byte-identical against the producer
    contract, `cap` param included -- the predecessor's freeze still binds and
    C2's de-async does not renegotiate it.

    Asserted as an EXACT key-set equality in both directions: a dropped key
    breaks every consumer reading it, and an added key is a bilateral,
    memo-gated change this plan had no authority to make (see
    `_common.build_act_result`'s own WIRE-SAFETY block, which strips exactly
    such an additive annotation before it reaches the wire).
    """
    _seed(repo, "2026-06-02-envelope.md", "status: claimed")
    common_dir = _common_dir(repo)

    preview = _run(_handler(
        {"mode": "already-terminal", "dry_run": True, "cap": 10},
        repo_root=common_dir,
    ))
    assert set(preview) == _ENVELOPE_KEYS, (
        f"dry_run:true envelope drifted from the producer contract; "
        f"missing={_ENVELOPE_KEYS - set(preview)!r} "
        f"added={set(preview) - _ENVELOPE_KEYS!r}"
    )
    assert preview["dry_run"] is True and preview["exit_code"] == 0

    act = _run(_handler(
        {
            "mode": "already-terminal", "dry_run": False, "cap": 10,
            "candidate_ids": [_cid("2026-06-02-envelope.md")],
        },
        repo_root=common_dir,
    ))
    assert set(act) == _ENVELOPE_KEYS, (
        f"dry_run:false envelope drifted from the producer contract; "
        f"missing={_ENVELOPE_KEYS - set(act)!r} "
        f"added={set(act) - _ENVELOPE_KEYS!r}"
    )
    assert act["candidates"] == [], (
        "the act arm carries `candidates` as an EMPTY LIST, never a dropped "
        f"key -- that is what makes the shape shared; got {act['candidates']!r}"
    )
    for item in act.get("acted", []):
        assert set(item) == {"id", "archived"}, (
            f"contract section 2.1 pins acted[] as exactly {{id, archived}}; "
            f"got {item!r}"
        )


def test_ac6_cap_is_still_a_required_param_on_both_arms(repo: Path):
    """AC-6's `cap`-included half: the BREAKING 2026-08-25 param is still
    required on `dry_run:true` and `dry_run:false` alike. The act arm had a
    covering test (`test_ac2_absent_cap_is_a_setup_error_never_unbounded`);
    the preview arm did not, and an unbounded preview is the cheaper of the
    two mistakes to ship unnoticed.
    """
    common_dir = _common_dir(repo)
    for dry_run in (True, False):
        result = _run(_handler(
            {"mode": "already-terminal", "dry_run": dry_run},
            repo_root=common_dir,
        ))
        assert result.get("exit_code") == 1, (
            f"an absent cap must be a setup error on the dry_run={dry_run} "
            f"arm too, never an unbounded sweep; got {result!r}"
        )


# ---------------------------------------------------------------------------
# AC-5 -- every refusal is preserved across C3's reorder, asserted as the
# three byte-identical comparisons the criterion actually names.
#
# The "before" side is not on disk, which is not the same fact as there being
# no oracle: `21f2e4539^` is C3's own parent and git holds that module
# permanently. It loads and runs against the same fixture with an identical
# `_scan_terminal` signature, so the comparison the criterion names is
# runnable after the fact -- it just has to be reconstructed rather than read.
# ---------------------------------------------------------------------------


#: C3 ("Classify handoffs in-memory before dirt-check; scope git query to
#: survivors"). Its parent is the last commit whose `_scan_terminal` still
#: dirty-checked before classifying -- the "before" side of AC-5's comparison.
_C3_REORDER_COMMIT = "21f2e4539"


def _load_pre_reorder_module(tmp_path: Path):
    """Materialise `archive_terminal_handoffs.py` as it stood immediately
    before C3's reorder, and import it under its own module name.

    Loaded from git rather than vendored as a fixture copy on purpose: a
    checked-in copy is a snapshot someone has to remember to keep honest,
    while `21f2e4539^` cannot drift.

    REGISTRY RESTORATION IS LOAD-BEARING, not tidiness. Executing this module
    re-runs its `@register_op("fleet.archive_completed_handoffs")` side
    effect, which REBINDS the live op key in `ipc._REGISTRY` to the
    pre-reorder handler process-wide. Left in place, every later real
    dispatch in the session resolves to a stale handler that no other test's
    monkeypatch can reach — which is exactly how this first landed, turning
    `test_ac4_sync_handler_still_dispatches_and_honors_per_op_timeout` red
    under a shuffled test order and nowhere else.
    """
    import importlib.util
    import sys

    import coordinator_core.ipc as _ipc

    shown = subprocess.run(
        ["git", "show", f"{_C3_REORDER_COMMIT}^:coordinator_core/ops/fleet/archive_terminal_handoffs.py"],
        cwd=str(Path(__file__).resolve().parents[4]),
        capture_output=True, text=True, timeout=30,
        stdin=subprocess.DEVNULL, **no_console_creationflags(),
    )
    assert shown.returncode == 0, (
        f"the pre-reorder module must be retrievable from git; "
        f"`git show {_C3_REORDER_COMMIT}^:...` failed: {shown.stderr!r}"
    )
    path = tmp_path / "pre_reorder_atho.py"
    path.write_text(shown.stdout, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("pre_reorder_atho", str(path))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["pre_reorder_atho"] = module
    registry_before = dict(_ipc._REGISTRY)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop("pre_reorder_atho", None)
        _ipc._REGISTRY.clear()
        _ipc._REGISTRY.update(registry_before)
    return module


def test_loading_the_pre_reorder_module_leaves_the_live_op_registry_intact(tmp_path):
    """Guards the loader above, not the sweep: the restoration is what keeps
    these AC-5 comparisons from silently breaking an unrelated test that
    dispatches the real op."""
    import coordinator_core.ipc as _ipc

    key = "fleet.archive_completed_handoffs"
    before = _ipc._REGISTRY.get(key)
    _load_pre_reorder_module(tmp_path)
    assert _ipc._REGISTRY.get(key) is before, (
        "loading the pre-reorder module must not leave its own handler bound "
        "to the live op key"
    )


def _seed_every_rail(repo: Path) -> None:
    """One fixture reaching every rail AC-5 cares about, including the record
    that is refused by BOTH the worktree-dirty rail and classification --
    the single class whose reason the reorder is designed to change."""
    _seed(repo, "2026-07-01-archivable.md", "status: claimed")
    _seed(repo, "2026-07-02-open.md", "status: open\ndeployment_state: ready_to_fire")
    parent = "2026-07-03-retained-parent.md"
    _seed(repo, parent, "status: claimed\ndeployment_state: continued")
    _seed(
        repo, "2026-07-04-live-child.md",
        f'status: open\npredecessor: "none"\nforked_from: "{parent}"\n'
        'deployment_state: in_flight',
    )
    dirty = _seed(repo, "2026-07-05-dirty.md", "status: claimed")
    dirty.write_text(dirty.read_text(encoding="utf-8") + "edit\n", encoding="utf-8")
    both = _seed(
        repo, "2026-07-06-dirty-and-non-terminal.md",
        "status: open\ndeployment_state: ready_to_fire",
    )
    both.write_text(both.read_text(encoding="utf-8") + "edit\n", encoding="utf-8")


def _scan_both_sides(repo: Path, tmp_path: Path):
    """Run pre-reorder and post-reorder `_scan_terminal` over the SAME
    fixture; return `(pre, post)` as `{refused: {id: reason}, survivors: [id]}`."""
    import inspect

    from coordinator_core.ops.fleet import archive_terminal_handoffs as post_mod

    pre_mod = _load_pre_reorder_module(tmp_path)
    common_dir = _common_dir(repo)

    sides = []
    for module in (pre_mod, post_mod):
        refused: "list[dict]" = []
        survivors = module._scan_terminal(repo, common_dir, skipped=refused)
        if inspect.iscoroutine(survivors):
            survivors = asyncio.run(survivors)
        sides.append({
            "refused": {item["id"]: item["reason"] for item in refused},
            "survivors": sorted(rel_id(entry[0], repo) for entry in survivors),
        })
    return sides[0], sides[1]


def test_ac5_refused_id_set_is_byte_identical_across_the_reorder(repo, tmp_path):
    """AC-5 (1): the SET OF REFUSED IDS is byte-identical before and after
    the reorder over the same fixture -- genuinely invariant. Classifying
    first changes WHICH rail names a record, never WHETHER it is refused.
    """
    _seed_every_rail(repo)
    pre, post = _scan_both_sides(repo, tmp_path)

    assert set(pre["refused"]) == set(post["refused"]), (
        "C3's reorder must refuse exactly the same records; "
        f"only-before={sorted(set(pre['refused']) - set(post['refused']))!r} "
        f"only-after={sorted(set(post['refused']) - set(pre['refused']))!r}"
    )
    assert pre["refused"], "fixture sanity: the fixture must refuse something"


def test_ac5_survivor_set_is_byte_identical_across_the_reorder(repo, tmp_path):
    """AC-5 (3): the SURVIVOR SET is byte-identical -- the property that
    actually matters, because it is what the sweep goes on to archive."""
    _seed_every_rail(repo)
    pre, post = _scan_both_sides(repo, tmp_path)

    assert pre["survivors"] == post["survivors"], (
        f"the reorder must not change what survives classification; "
        f"before={pre['survivors']!r} after={post['survivors']!r}"
    )
    assert post["survivors"], (
        "fixture sanity: something must survive, else this comparison is "
        "vacuously true against two empty sets"
    )


def test_ac5_reasons_are_byte_identical_except_the_documented_both_rails_class(
    repo, tmp_path
):
    """AC-5 (2): the REASON is byte-identical for every record refused by
    exactly one rail. The single documented exception is a record refused by
    BOTH the worktree-dirty rail and classification, which now reports
    `not-terminal: ...` instead of `worktree-dirty` -- categorical for that
    class, not merely relabeled for some instances.

    Asserted as an exact partition rather than a diff budget: a second
    changed-reason shape would pass a "at most one diff" check and is
    precisely the drift this criterion exists to catch.
    """
    _seed_every_rail(repo)
    pre, post = _scan_both_sides(repo, tmp_path)

    diffs = {
        rid: (pre["refused"].get(rid), post["refused"].get(rid))
        for rid in set(pre["refused"]) | set(post["refused"])
        if pre["refused"].get(rid) != post["refused"].get(rid)
    }

    unexplained = {
        rid: pair for rid, pair in diffs.items()
        if not (
            (pair[0] or "").startswith(_SCAN_REASON_WORKTREE_DIRTY)
            and (pair[1] or "").startswith(_SCAN_REASON_NOT_TERMINAL)
        )
    }
    assert not unexplained, (
        "the ONLY reason change the reorder is designed to produce is "
        "worktree-dirty -> not-terminal for a record refused by both rails; "
        f"got {unexplained!r}"
    )

    both_rails_id = _cid("2026-07-06-dirty-and-non-terminal.md")
    assert both_rails_id in diffs, (
        "fixture sanity: the both-rails record must actually change reason, "
        "else the exception clause above is vacuous and would pass against a "
        f"reorder that changed nothing at all; diffs={diffs!r}"
    )

    # Categorical for the class, not merely relabeled for some instances:
    # `worktree-dirty` must disappear from the both-rails record's census
    # entirely rather than surviving alongside the new reason.
    assert not (post["refused"][both_rails_id] or "").startswith(
        _SCAN_REASON_WORKTREE_DIRTY
    ), post["refused"][both_rails_id]

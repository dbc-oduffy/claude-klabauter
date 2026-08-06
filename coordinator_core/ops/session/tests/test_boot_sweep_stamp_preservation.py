"""
coordinator_core.ops.session.tests.test_boot_sweep_stamp_preservation

SPIKE (2026-08-05): executes, rather than reads, the claimed dependency
between session.boot_sweep._commit_consumed_metadata and
archive_and_commit's main-index resync (coordinator_core/ops/fleet/_common.py).

Background: archive_and_commit commits a git-mv rename from a private
HEAD-seeded index, then resyncs the SHARED main index with
`git update-index --add -- <dst>`, which reads dst's WORKTREE blob. Because
`git mv` re-keys the private index's HEAD-seeded entry for src rather than
rehashing src's current on-disk content, the archival commit itself can carry
HEAD's OLD content at dst while the resync stages dst's CURRENT DISK content
into the main index — a divergence between "what the archival commit
recorded" and "what the main index now holds" (see
test_index_residue_reproduction.py Arm B for the standalone repro of this
divergence).

The claim under test: _commit_consumed_metadata's pre-move
shipped_in/deployment_state stamps (written to the handoff's SOURCE path
BEFORE archive_and_commit's git-mv runs) allegedly reach _commit_consumed_
metadata's own follow-up commit ONLY BECAUSE the resync staged dst's dirty
worktree content into the main index — i.e. that a HEAD-blob resync (the
pending fix under discussion) would silently drop these stamps.

FINDING (see test_mechanism_resync_not_load_bearing_for_stamps below): this
claim does NOT hold. _commit_consumed_metadata commits via
`git commit -m <subject> -- <dst_paths>` — a PATHSPEC'd commit. A pathspec'd
`git commit` unconditionally re-reads the WORKTREE for the named paths before
committing (the same worktree-absorption mechanism archive_and_commit's own
FORWARD-B hazard note names), independent of whatever the main index already
holds staged for that path. Deliberately unstaging dst from the main index
between archive_and_commit's return and _commit_consumed_metadata's commit
(test_mechanism_resync_not_load_bearing_for_stamps) does not change the
outcome: the stamps still land, because the pathspec is the carrier, not the
resync.

Spec backlinks:
  - Blocker claim under test: coordinator_core/ops/session/boot_sweep.py
    _commit_consumed_metadata (see its own docstring's "After
    _handle_act_handoffs commits the git-mv..." paragraph).
  - Mechanism this file discriminates against: coordinator_core/ops/fleet/
    _common.py archive_and_commit's main-index resync (_update_index_with_retry
    / "Resync the MAIN index" block).
  - Sibling repro reused for scratch-git-repo harness style:
    coordinator_core/ops/fleet/tests/test_index_residue_reproduction.py
    (_init_scratch_repo, _git, _run).
  - Fixture idioms reused: this package's own test_boot_sweep.py (BootRepo /
    boot_repo fixture, seed_handoff, _default_caller_session_id).

Negative-spec:
  - Does NOT edit coordinator_core/ops/fleet/_common.py or
    coordinator_core/ops/session/boot_sweep.py — production code is
    untouched; this file only observes current HEAD behavior.
  - Does NOT re-test archive_and_commit's already-fixed retry-exhaustion
    residue mechanism (test_archive_and_commit_index_resync_residue.py) —
    this file's Move objects always leave restage_src at its documented
    default (False), matching the real non-heir call site in
    boot_sweep._sweep_consumed_handoffs.

Coverage-gap closure (2026-08-05, C5b): the `boot_repo` fixture below is now
parametrized over BOTH the unified-worktree layout (state_worktree ==
git_root_worktree) AND the two-repo layout (state_worktree != git_root_worktree,
mirroring test_boot_sweep.py's `_build_test_repo`/`_STATE_DIRS`/`_GIT_ROOT_DIRS`
two-repo tests). Both tests below now run against both layouts, so
_commit_consumed_metadata's two-repo Commit-B branch (committing from
state_worktree over dst_paths — see that function's own docstring "Two-worktree
edition" paragraph) is actually EXECUTED here, not merely code-inspected.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest

import coordinator_core.ops  # noqa: F401 — fires every op registration
import coordinator_core.ops.session.boot_sweep  # noqa: F401 — registers session.boot_sweep

from coordinator_core.ops.session.boot_sweep import _handler, _handle_act_handoffs

# Reuse the sibling reproduction's scratch-git-repo primitives — same shape,
# same reasoning for why they exist (see that module's own docstring).
from coordinator_core.ops.fleet.tests.test_index_residue_reproduction import (
    _git,
    _init_scratch_repo,
)

# Reuse this package's own BootRepo fixture helper — the real end-to-end
# consumed-handoff sweep path (seed_handoff, _default_caller_session_id) is
# exercised via boot_repo/BootRepo, not a hand-rolled scratch repo, for the
# AC regression test: it must drive the REAL sweep, not a synthetic stand-in.
#
# The `boot_repo` fixture itself is NOT imported — this module defines its
# OWN `boot_repo` fixture below, parametrized over the unified AND two-repo
# layouts (see module docstring "Coverage-gap closure"), reusing
# `_build_test_repo`/`_STATE_DIRS`/`_GIT_ROOT_DIRS`, the same two-repo
# construction primitives test_boot_sweep.py's own two-repo tests use.
from coordinator_core.ops.session.tests.test_boot_sweep import (
    BootRepo,
    _DEFAULT_TEST_SESSION_ID,
    _build_test_repo,
    _GIT_ROOT_DIRS,
    _STATE_DIRS,
)


@pytest.fixture(autouse=True)
def _default_caller_session_id(monkeypatch):
    """Autouse fixtures do NOT cross module boundaries — test_boot_sweep.py's
    identically-named fixture only applies to tests IN that file. This file
    needs the same CLAUDE_SESSION_ID = _DEFAULT_TEST_SESSION_ID setup so
    stamp_shipped_in's ownership guard (archive_stamp.py) recognizes this
    process as the author of BootRepo._git's own commits — see that
    fixture's docstring in test_boot_sweep.py for the full rationale."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", _DEFAULT_TEST_SESSION_ID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed.

    Mirrors test_index_residue_reproduction.py's / test_boot_sweep.py's
    identical helper.
    """
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# boot_repo — parametrized over the unified and two-repo layouts.
# ---------------------------------------------------------------------------

# Same directory skeleton as test_boot_sweep.py's own `boot_repo` fixture
# (docs/plans, state/handoffs, archive/handoffs, cross-repo/*, etc.) — this
# module builds it directly via `_build_test_repo` rather than invoking that
# already-decorated fixture function, since this module supplies its OWN
# `boot_repo` fixture (parametrized) under the same name.
_UNIFIED_DIRS = [
    "docs/plans",
    "state/handoffs",
    "state/bug-backlog",
    "archive/specs",
    "archive/handoffs",
    "archive/bug-backlog",
    "cross-repo/inbox",
    "cross-repo/archive",
]


class _LayoutBootRepo:
    """Adapts a single BootRepo (unified layout) or a STATE+GIT_ROOT BootRepo
    pair (two-repo layout) behind the same interface the two tests in this
    file already use (`.root`, `.common_dir`, `._git`, `.seed_handoff`,
    `.path_exists`, `.git_status_clean`), plus two additions the two-repo
    layout needs:

    - `.extra_params` — the `_handler` params dict (`{}` unified,
      `{"state_common_dir": ...}` two-repo) — the same shape
      test_boot_sweep.py's own two-repo tests pass.
    - `.dest_root` — the repo root that owns archive destinations
      (state/handoffs, archive/handoffs) — == `.root` when unified, but the
      SEPARATE state repo when two-repo (scope-target commits and
      `.root`/`._git` stay pinned to GIT_ROOT per docs/plans being a
      GIT_ROOT-relative code path, matching test_boot_sweep.py's own
      "scope_paths are GIT_ROOT-relative" two-repo convention).

    Spec backlink: coordinator_core/ops/session/boot_sweep.py
    _commit_consumed_metadata's own "Two-worktree edition" docstring
    paragraph — dst_paths physically reside in STATE repo after git-mv.
    """

    def __init__(self, state_repo: BootRepo, git_root_repo: BootRepo, unified: bool) -> None:
        self.state_repo = state_repo
        self.git_root_repo = git_root_repo
        self.unified = unified

    @property
    def root(self) -> Path:
        return self.git_root_repo.root

    @property
    def dest_root(self) -> Path:
        return self.state_repo.root

    @property
    def common_dir(self) -> Path:
        return self.git_root_repo.common_dir

    @property
    def extra_params(self) -> dict:
        if self.unified:
            return {}
        return {"state_common_dir": str(self.state_repo.common_dir)}

    def _git(self, *args: str, **kwargs) -> "subprocess.CompletedProcess":
        return self.git_root_repo._git(*args, **kwargs)

    def seed_handoff(self, *args, **kwargs) -> Path:
        return self.state_repo.seed_handoff(*args, **kwargs)

    def path_exists(self, repo_rel: str) -> bool:
        return self.state_repo.path_exists(repo_rel)

    def git_status_clean(self) -> bool:
        if self.unified:
            return self.state_repo.git_status_clean()
        return self.state_repo.git_status_clean() and self.git_root_repo.git_status_clean()


@pytest.fixture(params=["unified", "two-repo"])
def boot_repo(tmp_path, request) -> _LayoutBootRepo:
    """Parametrized over both layouts so both tests below EXECUTE
    _commit_consumed_metadata's two-repo Commit-B branch, not merely inspect
    it (see module docstring "Coverage-gap closure").

    unified:  a single BootRepo — state_worktree.resolve() == git_root_worktree.resolve().
    two-repo: a STATE BootRepo (owns state/handoffs, archive/handoffs) + a
              separate GIT_ROOT BootRepo (owns docs/plans, tasks/) — built via
              the same `_build_test_repo`/`_STATE_DIRS`/`_GIT_ROOT_DIRS`
              primitives test_boot_sweep.py's own two-repo tests use.
    """
    if request.param == "unified":
        repo = _build_test_repo(tmp_path / "repo", _UNIFIED_DIRS)
        return _LayoutBootRepo(state_repo=repo, git_root_repo=repo, unified=True)

    state_repo = _build_test_repo(tmp_path / "state", _STATE_DIRS)
    git_root_repo = _build_test_repo(tmp_path / "gitroot", _GIT_ROOT_DIRS)
    return _LayoutBootRepo(state_repo=state_repo, git_root_repo=git_root_repo, unified=False)


# ---------------------------------------------------------------------------
# Test 1 — AC regression: stamps must survive into commit history at HEAD.
# ---------------------------------------------------------------------------


def test_stamps_present_in_commit_history_at_archive_destination(boot_repo):
    """Drives the REAL consumed-handoff sweep end-to-end and asserts the
    pre-move shipped_in stamp is present in the COMMIT HISTORY at the
    archive destination (not merely on disk) — `git show HEAD:<dst>` must
    contain the stamped frontmatter, and the working tree must be clean
    afterwards.

    This is the deliverable AC test: it must FAIL if a future change (e.g.
    the pending HEAD-blob resync fix) silently drops the stamp from the
    metadata commit. It passes against current HEAD.
    """
    scope_path = "docs/plans/2026-07-01-scope-target.md"
    scope_file = boot_repo.root / scope_path
    scope_file.parent.mkdir(parents=True, exist_ok=True)
    scope_file.write_text("---\ntitle: scope target\n---\n", encoding="utf-8")
    boot_repo._git("add", str(scope_file))
    boot_repo._git("commit", "-m", "add scope target")

    boot_repo.seed_handoff(
        "2026-07-01-scoped.md", "consumed",
        extra_frontmatter=f"scope: {scope_path}",
    )
    cid = "state/handoffs/2026-07-01-scoped.md"

    result = _run(_handler(boot_repo.extra_params, repo_root=boot_repo.common_dir))

    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"
    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert cid in archived_ids, (
        f"consumed handoff with scope must be archived; got archived_ids={archived_ids!r}"
    )

    dest_rel = "archive/handoffs/2026-07/2026-07-01-scoped.md"
    assert boot_repo.path_exists(dest_rel), f"archived file must exist at {dest_rel!r}"

    # The AC assertion: the stamp must be in COMMIT HISTORY at HEAD, not just
    # on disk — `git show HEAD:<dst>` reads the committed blob directly.
    # dest_rel physically resides in the STATE repo after git-mv (== boot_repo.root
    # unified; the separate state repo two-repo) — use `dest_root`, not `root`.
    head_dst_content = _git(boot_repo.dest_root, "show", f"HEAD:{dest_rel}").stdout
    assert "shipped_in:" in head_dst_content, (
        f"shipped_in stamp must be present in the COMMIT HISTORY at "
        f"HEAD:{dest_rel} (not merely on disk); got content head="
        f"{head_dst_content[:400]!r}"
    )

    assert boot_repo.git_status_clean(), (
        "working tree must be clean after the sweep's commits"
    )


# ---------------------------------------------------------------------------
# Test 2 — mechanism discriminator: does the stamp depend on the resync's
# main-index staging, or does the pathspec'd commit carry it independently?
# ---------------------------------------------------------------------------


def test_mechanism_resync_not_load_bearing_for_stamps(boot_repo):
    """Isolates whether _commit_consumed_metadata's stamp survives
    INDEPENDENTLY of archive_and_commit's main-index resync.

    Wraps _handle_act_handoffs (the call site that invokes archive_and_commit
    inside _sweep_consumed_handoffs) so that, immediately AFTER
    archive_and_commit returns (resync already ran and staged dst) but
    BEFORE _commit_consumed_metadata's own commit runs, this test
    deliberately UNSTAGES the destination path from the main index
    (`git reset -- <dst>`) — undoing exactly what the resync staged.

    FINDING: the stamp still lands in _commit_consumed_metadata's commit.
    `git commit -m <subject> -- <dst_paths>` re-reads dst's WORKTREE content
    directly (a pathspec'd commit stages-then-commits the named paths from
    disk, bypassing whatever the main index already held for them) — the
    pathspec is the carrier of the stamped content, NOT the resync's
    `git update-index --add -- dst`. This directly REFUTES the claim that a
    HEAD-blob resync (the pending fix) would silently drop these stamps: the
    resync's staging is demonstrably not what carries them into this commit.
    """
    scope_path = "docs/plans/2026-07-01-scope-target.md"
    scope_file = boot_repo.root / scope_path
    scope_file.parent.mkdir(parents=True, exist_ok=True)
    scope_file.write_text("---\ntitle: scope target\n---\n", encoding="utf-8")
    boot_repo._git("add", str(scope_file))
    boot_repo._git("commit", "-m", "add scope target")

    boot_repo.seed_handoff(
        "2026-07-01-unstaged.md", "consumed",
        extra_frontmatter=f"scope: {scope_path}",
    )
    cid = "state/handoffs/2026-07-01-unstaged.md"
    dest_rel = "archive/handoffs/2026-07/2026-07-01-unstaged.md"

    real_handle_act_handoffs = _handle_act_handoffs

    async def _act_then_unstage_dst(*args, **kwargs):
        """Runs the real archival call, then unstages the resync's own
        staged dst entry from the main index BEFORE returning control to
        _sweep_consumed_handoffs — so _commit_consumed_metadata's later
        commit runs against an UNSTAGED dst, isolating the pathspec's own
        worktree-read behavior from the resync's staging."""
        act_result = await real_handle_act_handoffs(*args, **kwargs)
        acted = act_result.get("acted", [])
        for item in acted:
            if item.get("id") != cid:
                continue
            # dest_rel physically resides in the STATE repo after git-mv —
            # use `dest_root`, not `root` (which is GIT_ROOT in the two-repo
            # layout and would never find this path).
            dst_path = boot_repo.dest_root / dest_rel
            if dst_path.exists():
                _git(boot_repo.dest_root, "reset", "--", str(dst_path))
        return act_result

    with patch(
        "coordinator_core.ops.session.boot_sweep._handle_act_handoffs",
        side_effect=_act_then_unstage_dst,
    ):
        result = _run(_handler(boot_repo.extra_params, repo_root=boot_repo.common_dir))

    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"
    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert cid in archived_ids, (
        f"consumed handoff with scope must be archived; got archived_ids={archived_ids!r}"
    )
    assert boot_repo.path_exists(dest_rel), f"archived file must exist at {dest_rel!r}"

    # Discriminating assertion: the stamp lands in commit history at HEAD
    # even though the resync's own staged entry for dst was deliberately
    # unstaged before _commit_consumed_metadata's commit ran.
    head_dst_content = _git(boot_repo.dest_root, "show", f"HEAD:{dest_rel}").stdout
    assert "shipped_in:" in head_dst_content, (
        f"MECHANISM FINDING: if this fails, the resync's main-index staging "
        f"IS load-bearing for stamp preservation (contradicts this test's "
        f"docstring finding). Got HEAD:{dest_rel} content head="
        f"{head_dst_content[:400]!r}"
    )

    assert boot_repo.git_status_clean(), (
        "working tree must be clean after the sweep's commits, even though "
        "dst was deliberately unstaged mid-sweep — the pathspec'd commit "
        "must have re-staged and committed it"
    )

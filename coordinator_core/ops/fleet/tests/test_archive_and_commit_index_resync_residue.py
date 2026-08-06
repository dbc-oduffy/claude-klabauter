"""
coordinator_core.ops.fleet.tests.test_archive_and_commit_index_resync_residue

Regression net for the 2026-08-02 main-index-resync-residue fix: a contended
`.git/index.lock` exhausting the resync retry budget in archive_and_commit /
rm_and_commit must be REPORTED to the caller (via an additive
`index_resync_failed` key on the acted[]/reaped[] item), not merely logged
and silently swallowed.

Incident this closes: the ORIGINAL fixed 3-attempt/0.05s-sleep retry budget
(150ms of sleeping) was exhausted by ordinary `.git/index.lock` contention
from a genuinely concurrent `git commit` on this shared tree. The archival
commit landed correctly (HEAD advanced, content moved), but the main
`.git/index` was left holding a stale rename (`RD <dst> -> <src>`) — three
already-archived memos resurrected into a re-deliverable staged state across
2026-08-01/02, discovered only by manual `git status` inspection two days
later because the failure's only signal was a daemon-side `_LOG.error` line.

Coverage:
  - archive_and_commit: a persistently-failing `git update-index` (mocked,
    every attempt) still lands the commit (acted non-empty, HEAD advanced),
    but the acted[] item is annotated `index_resync_failed` with a non-empty
    reason string — the residue is no longer silent.
  - archive_and_commit: the ORDINARY success path (no contention) does NOT
    carry the `index_resync_failed` key at all — additive-only, does not
    perturb the frozen `{id, archived: true}` shape a v1 consumer expects.
  - rm_and_commit: same contended-resync-is-reported shape for its
    remove-only resync leg.
  - Retry hardening is exercised for real (not entirely mocked-away): a
    transient failure that clears within the retry budget still succeeds
    with no `index_resync_failed` annotation.

Style: mocks asyncio.create_subprocess_exec selectively (mirrors
test_archive_and_commit_forward_b_hazard.py's `_FakeCompletedProc` /
side-effect-wrapping-the-real-create pattern) rather than holding a real OS
file lock across the test, and monkeypatches the module's retry constants
down to keep the exhaustion case fast — this file's own root-cause repro
(scratch-repo + a real `.git/index.lock` held on a background thread) is
retained separately as a dispatch scratchpad artifact, not shipped as a
slow test here.

Spec backlinks:
  - Fixed sites: coordinator_core/ops/fleet/_common.py archive_and_commit,
    rm_and_commit, _update_index_with_retry
  - Pattern mirrors: test_archive_and_commit_forward_b_hazard.py (selective
    subprocess mocking), test_common_rm_and_commit.py (rm_and_commit house
    style), test_fleet_common.py (fleet_repo fixture usage)
"""

from __future__ import annotations

import asyncio

import pytest

from coordinator_core.ops.fleet import _common
from coordinator_core.ops.fleet._common import Move, archive_and_commit, rm_and_commit


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


class _FakeCompletedProc:
    """Stand-in for asyncio.subprocess.Process exposing only what the resync
    loop reads off a completed process: .returncode and an awaitable
    .communicate(). Mirrors test_archive_and_commit_forward_b_hazard.py's
    identical helper."""

    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


def _fail_all_update_index(monkeypatch):
    """Patch asyncio.create_subprocess_exec so every `git update-index` call
    fails with a synthetic "would be locked" stderr, while every OTHER git
    subcommand (read-tree, mv, rm, commit) passes through to the real
    implementation unmodified. Also shrinks the retry budget so the
    exhaustion case runs fast in CI.
    """
    real_create = asyncio.create_subprocess_exec

    async def _side_effect(*args, **kwargs):
        if len(args) >= 2 and args[0] == "git" and args[1] == "update-index":
            return _FakeCompletedProc(
                128, b"", b"fatal: Unable to create '.git/index.lock': File exists."
            )
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _side_effect)
    monkeypatch.setattr(_common, "_INDEX_RETRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(_common, "_INDEX_RETRY_INITIAL_SLEEP_S", 0.001)
    monkeypatch.setattr(_common, "_INDEX_RETRY_BACKOFF_CAP_S", 0.001)


def _fail_then_succeed_update_index(monkeypatch, fail_times: int = 1):
    """Patch so the first `fail_times` `git update-index` invocations fail and
    every subsequent one (and every other git subcommand) succeeds for real —
    proves the retry hardening actually rides out a transient failure rather
    than merely reporting one.
    """
    real_create = asyncio.create_subprocess_exec
    calls = {"update_index": 0}

    async def _side_effect(*args, **kwargs):
        if len(args) >= 2 and args[0] == "git" and args[1] == "update-index":
            calls["update_index"] += 1
            if calls["update_index"] <= fail_times:
                return _FakeCompletedProc(
                    128, b"", b"fatal: Unable to create '.git/index.lock': File exists."
                )
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _side_effect)
    monkeypatch.setattr(_common, "_INDEX_RETRY_MAX_ATTEMPTS", 5)
    monkeypatch.setattr(_common, "_INDEX_RETRY_INITIAL_SLEEP_S", 0.001)
    monkeypatch.setattr(_common, "_INDEX_RETRY_BACKOFF_CAP_S", 0.001)


# ---------------------------------------------------------------------------
# archive_and_commit
# ---------------------------------------------------------------------------


def test_archive_and_commit_resync_exhaustion_is_reported(fleet_repo, monkeypatch):
    """A persistently-failing main-index resync still lands the commit, but
    the acted[] item is annotated `index_resync_failed` — not silently
    swallowed (the exact defect this fix closes)."""
    plan_path = fleet_repo.seed_plan("2026-01-01-my-plan.md", "implemented")
    dst = fleet_repo.root / "archive" / "specs" / "2026-01-01-my-plan.md"
    move = Move(src=plan_path, dst=dst, candidate_id="docs/plans/2026-01-01-my-plan.md")

    _fail_all_update_index(monkeypatch)

    acted, failed = _run(archive_and_commit(fleet_repo.root, [move], "test archive"))

    assert failed == []
    assert len(acted) == 1
    assert acted[0]["id"] == "docs/plans/2026-01-01-my-plan.md"
    assert acted[0]["archived"] is True
    assert "index_resync_failed" in acted[0]
    assert acted[0]["index_resync_failed"]  # non-empty reason string
    # The commit itself is authoritative and unaffected by resync exhaustion.
    assert not plan_path.exists()
    assert dst.exists()
    assert "test archive" in fleet_repo.git_log_subject()


def test_archive_and_commit_resync_success_has_no_residue_key_given_precommitted_src(fleet_repo):
    """Ordinary success path: `index_resync_failed` is absent entirely — the
    additive key must not perturb the frozen {id, archived: true} shape.

    Precondition made explicit: `fleet_repo.seed_plan` stages AND commits
    `src` before this test ever calls `archive_and_commit`, so the trailing
    `git_status_clean()` assertion is only ever exercising the clean-src
    resync path. It says nothing about residue on a dirty/uncommitted src —
    that is not this test's claim."""
    plan_path = fleet_repo.seed_plan("2026-01-02-my-plan.md", "implemented")
    dst = fleet_repo.root / "archive" / "specs" / "2026-01-02-my-plan.md"
    move = Move(src=plan_path, dst=dst, candidate_id="docs/plans/2026-01-02-my-plan.md")

    acted, failed = _run(archive_and_commit(fleet_repo.root, [move], "test archive"))

    assert failed == []
    assert len(acted) == 1
    assert acted[0] == {
        "id": "docs/plans/2026-01-02-my-plan.md",
        "archived": True,
    }
    assert "index_resync_failed" not in acted[0]
    assert fleet_repo.git_status_clean()


def test_archive_and_commit_resync_rides_out_transient_failure(fleet_repo, monkeypatch):
    """A resync failure that clears within the retry budget succeeds with no
    residue annotation — the retry hardening actually rides out contention,
    it does not merely report exhaustion."""
    plan_path = fleet_repo.seed_plan("2026-01-03-my-plan.md", "implemented")
    dst = fleet_repo.root / "archive" / "specs" / "2026-01-03-my-plan.md"
    move = Move(src=plan_path, dst=dst, candidate_id="docs/plans/2026-01-03-my-plan.md")

    _fail_then_succeed_update_index(monkeypatch, fail_times=1)

    acted, failed = _run(archive_and_commit(fleet_repo.root, [move], "test archive"))

    assert failed == []
    assert len(acted) == 1
    assert "index_resync_failed" not in acted[0]
    assert fleet_repo.git_status_clean()


def test_archive_and_commit_ls_tree_lookup_failure_still_removes_stale_src(fleet_repo, monkeypatch):
    """Review: code-reviewer F1 — an `_ls_tree_head_cacheinfo` failure (nonzero
    exit, empty stdout, unparseable row) must not skip the `--remove -- src`
    attempt entirely. The pre-cacheinfo shape ran `--remove` and `--add`
    independently; a regression made `--add`'s gating short-circuit `--remove`
    too, stranding the stale src entry in the main index on top of the
    already-annotated missed add — exactly the residue-silence shape this
    file's incident history exists to close.

    Verified inert on other paths: the ordinary success path is unaffected
    (`--remove` still runs before `--add`, same as before this fix — see
    `test_archive_and_commit_resync_success_has_no_residue_key_given_precommitted_src`
    above, unchanged and still green), and the partial-success
    (`--remove` succeeds, `--add` fails) path is unaffected (`--add`'s own
    failure handling is untouched; only the lookup-failure branch changed).
    """
    plan_path = fleet_repo.seed_plan("2026-01-04-my-plan.md", "implemented")
    dst = fleet_repo.root / "archive" / "specs" / "2026-01-04-my-plan.md"
    move = Move(src=plan_path, dst=dst, candidate_id="docs/plans/2026-01-04-my-plan.md")

    async def _fake_ls_tree_head_cacheinfo(*args, **kwargs):
        return None

    monkeypatch.setattr(_common, "_ls_tree_head_cacheinfo", _fake_ls_tree_head_cacheinfo)

    acted, failed = _run(archive_and_commit(fleet_repo.root, [move], "test archive"))

    assert failed == []
    assert len(acted) == 1
    assert acted[0]["archived"] is True
    assert "ls-tree-lookup-failed" in acted[0]["index_resync_failed"]

    # The stale src entry must be gone from the main index — the exact
    # postcondition this fix restores on the ls-tree-lookup-failure branch.
    ls_files = fleet_repo._git_unchecked(
        "ls-files", "--", str(plan_path.relative_to(fleet_repo.root)),
    ).stdout.decode().strip()
    assert ls_files == "", "stale src entry must be removed from the main index even when ls-tree lookup fails"


# ---------------------------------------------------------------------------
# rm_and_commit
# ---------------------------------------------------------------------------


def test_rm_and_commit_resync_exhaustion_is_reported(fleet_repo, monkeypatch):
    """rm_and_commit's remove-only resync leg carries the same defect and fix
    — exhaustion is annotated on the reaped[] item, not silently dropped."""
    bug_path = fleet_repo.seed_bug("2026-01-01-my-bug.yaml", "closed")

    _fail_all_update_index(monkeypatch)

    reaped, failed = _run(rm_and_commit(fleet_repo.root, [bug_path], "test reap"))

    assert failed == []
    assert len(reaped) == 1
    assert reaped[0]["reaped"] is True
    assert "index_resync_failed" in reaped[0]
    assert reaped[0]["index_resync_failed"]
    assert not bug_path.exists()
    assert "test reap" in fleet_repo.git_log_subject()

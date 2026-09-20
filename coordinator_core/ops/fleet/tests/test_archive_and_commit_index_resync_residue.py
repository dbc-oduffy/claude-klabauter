"""
coordinator_core.ops.fleet.tests.test_archive_and_commit_index_resync_residue

C4 of docs/plans/2026-08-13-archive-family-coverage-restoration.md — REWRITE,
real-git, governed pattern (own scoped module, one throwaway repo per test,
spawn visible in the diff).

Regression net for the main-index-resync-residue class: `archive_and_commit`
lands its batch commit in-process (`_commit_via_head_spine`, no `git commit`
subprocess — see test_archive_and_commit_forward_b_hazard.py's structural
guard), but the shared, real `.git/index` does not move with that commit on
its own. A separate resync step (`_resync_main_index_for_moves`, `_common.py`)
runs ONE real `git restore --staged -- <src> <dst>` per batch afterward to
bring the main index back in line with the new HEAD. Contention on
`.git/index.lock` — from a genuinely concurrent peer commit on this shared
tree — can exhaust that resync's retry budget; when it does, the archival
commit is unaffected (already landed, authoritative) but the residue MUST be
surfaced to the caller as an additive `index_resync_failed` key on the
acted[] item, never silently swallowed. That silent-swallow shape is the
2026-08-01/02 incident this file traces to.

**Mechanism drift from the culled version, re-derived against HEAD
2026-09-19 (AC7b):** the culled file mocked `git update-index` calls and
patched `_common._INDEX_RETRY_MAX_ATTEMPTS`/`_INDEX_RETRY_INITIAL_SLEEP_S`/
`_INDEX_RETRY_BACKOFF_CAP_S` directly. The retry constants are unchanged in
name and are still read as plain module globals inside `_update_index_with_
retry` (not captured as a bound default at another function's def time), so
patching them still works. What moved: the actual git subcommand issued by
the resync is now `git restore --staged -- <paths...>` (batched over both
src and dst per move, one argv per bounded chunk), not `git update-index`
(the pre-2026-08-19 `--cacheinfo`/`--remove` approximation this file's own
module docstring already called out as retired). This module intercepts
`git restore` accordingly.

**Two of the culled file's five `def test_` functions are ported here**
(`resync_exhaustion_is_reported`, `resync_success_has_no_residue_key`) — the
two that pin the defect/fix shape itself. The remaining three
(`resync_rides_out_transient_failure`, `ls_tree_lookup_failure_still_
removes_stale_src`, and the `rm_and_commit` sibling `resync_exhaustion_is_
reported`) are DROPPED-AS-OBSOLETE from this module, not silently absorbed:
the transient-retry case is the same code path as exhaustion with a lower
iteration count and does not exercise a distinct branch; the `_ls_tree_
head_cacheinfo` case names a helper removed from `_common` (`2c0e8d696`,
per this plan's own § Problem "Post-cull behaviour commits" note) and has no
current call site to monkeypatch; the `rm_and_commit` sibling belongs with
`test_common_rm_and_commit.py` (C3's surface, not this row's `writes:`) and
is named here only so C5(a)'s roster does not double-count it as this
module's gap.

Spec backlinks:
  - Fixed sites: coordinator_core/ops/fleet/_common.py archive_and_commit,
    _resync_main_index_for_moves, _update_index_with_retry
  - Pattern mirrors: coordinator_core/ops/fleet/tests/
    test_archive_and_commit_disk_head_drift.py (governed real-git module
    shape: own `_git` helper, `no_console_creationflags()`, one throwaway
    repo per test via `tmp_path`)

AC5b — per-test justification register
---------------------------------------
Both tests below assert on the real `.git/index.lock`-contention retry
mechanism's OWN reporting behaviour (whether `index_resync_failed` is
annotated), which is decided by a real git subprocess's exit code inside
`_update_index_with_retry` — no filesystem-only mock can exhibit "a real
`git restore --staged` invocation failed/succeeded N times", so both stay
real-git rather than moving to the git-free seam.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import _common
from coordinator_core.ops.fleet._common import Move, archive_and_commit
from coordinator_core.win_portability import no_console_creationflags

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
        check=True, **no_console_creationflags(),
    )


def _run(coro):
    return asyncio.run(coro)


def _seed_repo_with_plan(tmp_path: Path, name: str) -> tuple[Path, Path, Path]:
    """Init a throwaway repo with a committed plan file. Returns
    (root, src, dst) — dst is the archival destination this test's single
    Move targets, not yet created."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "test@example.invalid"], root)
    _git(["config", "user.name", "test"], root)

    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    src = plans / name
    src.write_text("---\nstatus: implemented\n---\n\nplan body\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed: plan"], root)

    dst = root / "archive" / "specs" / name
    return root, src, dst


class _FakeCompletedProc:
    """Stand-in for asyncio.subprocess.Process exposing only what the resync
    loop reads off a completed process: .returncode and an awaitable
    .communicate()."""

    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


def _fail_all_git_restore(monkeypatch):
    """Every `git restore` invocation fails with a synthetic index.lock
    contention error; every other git subcommand passes through to the real
    asyncio.create_subprocess_exec unmodified. Retry budget shrunk so the
    exhaustion case runs fast."""
    real_create = asyncio.create_subprocess_exec

    async def _side_effect(*args, **kwargs):
        if len(args) >= 2 and args[0] == "git" and args[1] == "restore":
            return _FakeCompletedProc(
                128, b"", b"fatal: Unable to create '.git/index.lock': File exists."
            )
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _side_effect)
    monkeypatch.setattr(_common, "_INDEX_RETRY_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(_common, "_INDEX_RETRY_INITIAL_SLEEP_S", 0.001)
    monkeypatch.setattr(_common, "_INDEX_RETRY_BACKOFF_CAP_S", 0.001)


def test_archive_and_commit_resync_exhaustion_is_reported(tmp_path, monkeypatch):
    """A persistently-failing main-index resync still lands the commit, but
    the acted[] item is annotated `index_resync_failed` — not silently
    swallowed (the exact defect this fix closes)."""
    root, src, dst = _seed_repo_with_plan(tmp_path, "2026-01-01-my-plan.md")
    move = Move(src=src, dst=dst, candidate_id="docs/plans/2026-01-01-my-plan.md")

    _fail_all_git_restore(monkeypatch)

    acted, failed = _run(archive_and_commit(root, [move], "test archive"))

    assert failed == []
    assert len(acted) == 1
    assert acted[0]["id"] == "docs/plans/2026-01-01-my-plan.md"
    assert acted[0]["archived"] is True
    assert "index_resync_failed" in acted[0]
    assert acted[0]["index_resync_failed"]

    # The commit itself is authoritative and unaffected by resync exhaustion.
    assert not src.exists()
    assert dst.exists()
    head_subject = _git(["log", "-1", "--format=%s"], root).stdout.strip()
    assert head_subject == "test archive"


def test_archive_and_commit_resync_success_has_no_residue_key(tmp_path):
    """Ordinary success path: `index_resync_failed` is absent entirely — the
    additive key must not perturb the frozen {id, archived: true} shape."""
    root, src, dst = _seed_repo_with_plan(tmp_path, "2026-01-02-my-plan.md")
    move = Move(src=src, dst=dst, candidate_id="docs/plans/2026-01-02-my-plan.md")

    acted, failed = _run(archive_and_commit(root, [move], "test archive"))

    assert failed == []
    assert len(acted) == 1
    assert acted[0] == {
        "id": "docs/plans/2026-01-02-my-plan.md",
        "archived": True,
    }
    assert "index_resync_failed" not in acted[0]

    status = _git(["status", "--porcelain"], root).stdout
    assert status.strip() == "", f"expected a clean tree/index after a successful resync: {status!r}"

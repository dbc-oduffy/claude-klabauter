"""
coordinator_core.ops.fleet.tests.test_archive_and_commit_reversal_else_src_reappeared

AC4 of docs/plans/2026-08-13-fleet-archive-dest-collision-vs-idempotent-
replay.md: archive_and_commit's post-move reversal guard carries an
`elif move.dst.exists():` alongside its original `if move.dst.exists() and
not move.src.exists():` — deliberately an `elif` narrowing, not a bare
`else`. When the destination has been clobbered back to a "src also still
present" shape at reversal time, the reversal cannot run without clobbering
dst, so it must WARN and land the orphaned dst in `failed[]` rather than
silently no-op.

F-5 swap (2026-08-21, docs/plans/2026-08-20-the-close-ceremony-stops-paying-
for-the-join.md C5): `git mv` is no longer the mover -- `os.replace` is, and
it has no split-failure mode of its own (see _common.archive_and_commit's
docstring). The reversal guard this module pinned then sat at the batched
`git add -- src dst` staging call that ran after every move's `os.replace`
had already landed.

RE-TARGETED, 2026-08-26 (C2 `dccf2fc01`, this plan's C7): `git add --`
itself is gone too, AND (a second, later removal on the SAME day —
`cffa6e99f`, this repair) the batched `git hash-object -w --stdin-paths`
call (`_hash_object_stdin_paths`) is no longer called at all on a
`restage_src=False` move (the default, and what this test's fixture uses)
-- it now fires ONLY over the `restage_src=True` subset. This test's
original interception point (`_hash_object_stdin_paths`) therefore never
runs on this fixture's path any more, and the test would go green having
verified nothing.

RE-SITED (this repair): the interception now targets
`_commit_via_head_spine` -- the HEAD-race CAS landing call
(`coordinator_core.ops.ceremony.git_native._commit_via_head_spine`,
imported into `_common`) that fires unconditionally on the commit-landing
step, for every move regardless of `restage_src`. It sits strictly between
os.replace (already landed) and the commit actually being recorded, which
is exactly where this test needs to interpose: a failure there drives the
SAME `commit_rc != 0` reversal branch the old hash-object failure used to
drive (both failure shapes feed the identical post-commit reversal loop
below `if commit_rc != 0:`). A call-counter sentinel on the mock asserts
the interception genuinely fired, so a future re-site of the call site
fails this test loudly instead of going quietly green.

1. Quiet path (no reversal attempted at all): dst pre-exists and force is
   False -- archive_and_commit refuses the move BEFORE calling os.replace
   (see the `dst-exists` guard), so nothing moved and there is nothing to
   reverse or WARN about.
2. Elif-fires path: os.replace succeeds, then the commit-landing step
   (`_commit_via_head_spine`, mocked -- a generic failure shape, not an
   index/worktree divergence, so no real-git requirement here) fails with a
   concurrent write recreating src's original path before the reversal
   attempt runs. The reversal cannot rename dst back over a reappeared src,
   so it WARNs and leaves dst orphaned, landing the item in failed[]
   annotated "reversal-skipped-src-reappeared".

Real git is load-bearing for the repo scaffolding (a working tree +
private-index staging) but not for either scenario's divergence -- both are
now generic control-flow, so the stage-failure call is mocked directly
rather than engineered through real index state. Deliberately its OWN
small, scoped module (not an import of
coordinator_core/ops/ceremony/tests/fixtures/real_git.py, which is reserved
for the commit-mechanism selector's own tests), per the standing test-cull
ruling (state/audits/2026-08-07-spawn-heavy-test-excision-ledger.md) that
real-git fixtures must not go ambient again.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import coordinator_core.ops.fleet._common as _common_mod
from coordinator_core.ops.fleet._common import Move, archive_and_commit
from coordinator_core.ops.ceremony.git_native import GitResult
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
        check=True,
    **no_console_creationflags())


def _run(coro):
    return asyncio.run(coro)


def _init_repo(root: Path) -> None:
    root.mkdir()
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "test@example.invalid"], root)
    _git(["config", "user.name", "test"], root)


def test_quiet_when_dst_exists_refusal_never_touches_disk(tmp_path: Path, caplog) -> None:
    """force=False + pre-existing dst refuses BEFORE os.replace runs -- no
    move happened, so there is nothing to reverse and no WARNING to log."""
    root = tmp_path / "repo"
    _init_repo(root)

    src_dir = root / "a"
    src_dir.mkdir()
    src = src_dir / "src.txt"
    src.write_text("src content\n", encoding="utf-8")

    dst_dir = root / "b"
    dst_dir.mkdir()
    dst = dst_dir / "dst.txt"
    dst.write_text("real archived content\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed src+dst"], root)

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.fleet._common"):
        acted, failed = _run(archive_and_commit(
            worktree_root=root,
            moves=[Move(src=src, dst=dst, candidate_id="a/src.txt")],
            subject="test: dst-exists refusal is quiet",
        ))

    assert acted == []
    assert len(failed) == 1
    assert failed[0]["id"] == "a/src.txt"
    assert "dst-exists" in failed[0]["reason"]
    assert "reversal-skipped-src-reappeared" not in failed[0]["reason"]
    assert not any(
        "skipping reversal" in rec.message for rec in caplog.records
    ), "a refusal that never called os.replace must not log a reversal WARNING"

    # Both copies survive untouched -- no clobber, no phantom move.
    assert src.exists()
    assert src.read_text(encoding="utf-8") == "src content\n"
    assert dst.exists()
    assert dst.read_text(encoding="utf-8") == "real archived content\n"


def test_reversal_elif_fires_when_src_reappears_before_stage_reversal(
    tmp_path: Path, caplog, monkeypatch,
) -> None:
    """os.replace succeeds, the commit-landing step then fails, and a
    concurrent writer recreates src's original path before the reversal
    runs -- the elif must fire: WARN, leave dst orphaned, annotate failed[]
    with "reversal-skipped-src-reappeared" rather than clobber the
    reappeared src.

    Re-sited (2026-08-26) onto `_commit_via_head_spine` -- see module
    docstring for why `_hash_object_stdin_paths` no longer fires on this
    (restage_src=False, default) path. `call_count` proves the mock
    actually fired, so a future re-site of the commit-landing call site
    fails this test loudly instead of going quietly green."""
    root = tmp_path / "repo"
    _init_repo(root)

    src_dir = root / "a"
    src_dir.mkdir()
    src = src_dir / "src.txt"
    src.write_text("src content\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed src"], root)

    dst = root / "b" / "dst.txt"

    call_count = 0

    def _commit_via_head_spine_and_reappear_src(*args, **kwargs):
        # The real HEAD-spine commit landing is left to fail synthetically
        # rather than engineered through real git state: the mock recreates
        # src (simulating a concurrent writer landing on the vacated path)
        # and returns a synthetic failed GitResult directly.
        nonlocal call_count
        call_count += 1
        src.write_text("concurrent writer content\n", encoding="utf-8")
        return GitResult(returncode=1, stdout="", stderr="synthetic commit-landing failure")

    monkeypatch.setattr(
        _common_mod, "_commit_via_head_spine", _commit_via_head_spine_and_reappear_src
    )

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.fleet._common"):
        acted, failed = _run(archive_and_commit(
            worktree_root=root,
            moves=[Move(src=src, dst=dst, candidate_id="a/src.txt")],
            subject="test: reappeared-src reversal at commit-landing failure",
        ))

    assert call_count >= 1, "the mocked _commit_via_head_spine was never invoked -- interception did not fire"
    assert acted == []
    assert len(failed) == 1
    assert failed[0]["id"] == "a/src.txt"
    assert "reversal-skipped-src-reappeared" in failed[0]["reason"]
    assert any(
        "skipping reversal" in rec.message and "reappeared" in rec.message
        for rec in caplog.records
    ), "the elif branch must WARN naming the reappeared-source skip"

    # dst is left orphaned (os.replace's move already landed there) and the
    # reappeared src is left untouched -- no clobber, no phantom reversal.
    assert dst.exists()
    assert dst.read_text(encoding="utf-8") == "src content\n"
    assert src.exists()
    assert src.read_text(encoding="utf-8") == "concurrent writer content\n"

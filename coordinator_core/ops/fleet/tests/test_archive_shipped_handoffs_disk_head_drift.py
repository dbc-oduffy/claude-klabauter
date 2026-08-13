"""
coordinator_core.ops.fleet.tests.test_archive_shipped_handoffs_disk_head_drift

Pins the disk/HEAD drift guard (archive_and_commit, coordinator_core/ops/fleet/
_common.py, landed 4541069c3) across the composite path it actually protects:
fleet.archive_shipped_handoffs._scan_shipped/_is_shipped_terminal (the
terminality predicate, which reads current ON-DISK frontmatter) feeding
_handle_act (which calls archive_and_commit with restage_src=False — the
shared default both the standalone fleet.archive_shipped_handoffs op and
session.boot_sweep's batch sweep use).

Genuine failure shape under test: verify-passes-then-move-refuses. A handoff
whose on-disk frontmatter carries deployment_state:shipped and a
git-reachable shipped_in SHA, but where that stamp was never committed (HEAD
still holds the pre-stamp content), is exactly the shape
handoff.ship_and_archive's "graceful partial outcome" can leave behind (see
coordinator_core/ops/handoff_ship_archive.py's module docstring) if a second,
separately-stamped shipped_in write is also left uncommitted before a batch
sweep runs. _is_shipped_terminal genuinely passes (it reads disk); the guard
inside archive_and_commit must then refuse the move rather than let git mv
silently commit HEAD's stale (pre-stamp) blob.

This module spawns real git (mirrors test_archive_and_commit_disk_head_drift.py
in this same directory — index/worktree divergence cannot be exhibited by a
mocked git) but stays its own small, scoped module per the standing test-cull
ruling (state/audits/2026-08-07-spawn-heavy-test-excision-ledger.md) that
real-git fixtures must not go ambient again. Exactly ONE throwaway repo, ONE
test.

Spec backlink: commit 4541069c3 (disk/HEAD drift guard); this repo's own
test_archive_and_commit_disk_head_drift.py (guard's own unit-level pin, at
the archive_and_commit layer directly rather than through the composite).

Negative-spec:
  - Does NOT re-test archive_and_commit directly — that is
    test_archive_and_commit_disk_head_drift.py's job. This file's job is the
    composite: _is_shipped_terminal must genuinely pass on disk content
    before _handle_act's own archive_and_commit call refuses the move.
  - Does NOT exercise restage_src=True (handoff_ship_archive.py's own opt-in
    call site) — that path intentionally restages src moments before the
    move and is out of scope for this drift-guard regression.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet.archive_shipped_handoffs import _handle_act, _is_shipped_terminal

# Real-git spawn is load-bearing: same argument as
# test_archive_and_commit_disk_head_drift.py's own docstring -- the disk/HEAD
# drift the composite path must refuse is an index/worktree divergence no
# mocked git has an index to exhibit. Single test, single repo -- no
# module-scope hoist needed. The spawn ratchet's `_BASELINE` is shrink-only
# pre-existing residue and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    # popup-intentional-last-resort — test-only real-git spawn, mirrors this
    # directory's own test_archive_and_commit_disk_head_drift.py; no console
    # window risk on the CI/dev platforms this suite runs on.
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _run(coro):
    return asyncio.run(coro)


def test_verify_passes_then_move_refuses_across_scan_and_act(tmp_path: Path):
    """A handoff whose on-disk frontmatter satisfies _is_shipped_terminal but
    whose committed HEAD blob still holds the pre-stamp content must NOT be
    archived by _handle_act — the disk/HEAD drift guard inside its
    archive_and_commit call must refuse the move, surfacing the refusal in
    the act envelope with a reason naming the drift."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "test@example.invalid"], root)
    _git(["config", "user.name", "test"], root)

    handoffs = root / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    src = handoffs / "2026-08-06-roadmap-sedge-03.md"
    cid = "state/handoffs/2026-08-06-roadmap-sedge-03.md"

    # Committed (HEAD) content: in_flight, no shipped_in.
    head_content = (
        "---\n"
        "status: claimed\n"
        "deployment_state: in_flight\n"
        "---\n\nBody.\n"
    )
    src.write_text(head_content, encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed: in_flight handoff"], root)
    head_sha = _git(["rev-parse", "HEAD"], root).stdout.strip()

    # Uncommitted disk-only stamp: deployment_state:shipped + a resolvable
    # shipped_in (HEAD's own SHA — git-reachable, so the SHA-gate passes) —
    # never staged, never committed.
    drifted_content = (
        "---\n"
        "status: claimed\n"
        f"deployment_state: shipped\nshipped_in: {head_sha}\n"
        "---\n\nBody.\n"
    )
    src.write_text(drifted_content, encoding="utf-8")

    assert src.read_text(encoding="utf-8") != head_content, "fixture sanity: disk must differ from HEAD"

    # Step 1: the terminality predicate genuinely passes — it reads disk.
    is_terminal, note = _run(_is_shipped_terminal(src, root))
    assert is_terminal is True, f"predicate must pass on disk content; got note={note!r}"

    # Step 2: _handle_act (restage_src default False, the shared batch path
    # both fleet.archive_shipped_handoffs and session.boot_sweep use) must
    # NOT move it — the archive_and_commit drift guard must refuse.
    act_result = _run(_handle_act("already-terminal", root, [cid]))

    acted_ids = [item.get("id") for item in act_result.get("acted", [])]
    assert cid not in acted_ids, f"drifted candidate must not be acted on: {act_result!r}"

    dst = root / "archive" / "handoffs" / "2026-08" / src.name
    assert not dst.exists(), "nothing must be archived with stale HEAD content"
    assert src.exists()
    assert src.read_text(encoding="utf-8") == drifted_content

    # Review: reviewer flagged that matching bare "drift" over the union of
    # skipped[]/failed[] doesn't discriminate this guard from _handle_act's
    # own D2(iv) terminality-drift re-verify (which lands in skipped[] with
    # a distinct "terminality-drift:" reason). Assert failed[] specifically,
    # with the archive_and_commit guard's own "disk/HEAD drift" reason text.
    failed_reasons = [
        item.get("reason", "")
        for item in act_result.get("failed", [])
        if item.get("id") == cid
    ]
    assert failed_reasons, f"candidate must appear in failed[] with a reason; got {act_result!r}"
    assert any("disk/HEAD drift" in r for r in failed_reasons), (
        f"reason must name the disk/HEAD drift; got failed_reasons={failed_reasons!r}"
    )

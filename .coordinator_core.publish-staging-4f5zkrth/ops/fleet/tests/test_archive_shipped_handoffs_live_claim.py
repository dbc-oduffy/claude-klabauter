"""
coordinator_core.ops.fleet.tests.test_archive_shipped_handoffs_live_claim

Pins the 2026-08-13 live-claim-dir gate added to
fleet.archive_shipped_handoffs._is_shipped_terminal (Check 3), which aligns
this op with fleet.archive_completed_handoffs's own Check 4 primary key. Before
this gate, a shipped handoff whose claim dir was still live-held was RETAINED
by fleet.archive_completed_handoffs's Branch B but archived anyway by this op
on the SAME boot sweep (session.boot_sweep runs both sweeps in one process),
making the retention inert. See archive_shipped_handoffs.py's module docstring
negative-spec for the full rationale.

Also pins the same-day holder_initiated opt-out fix: Check 3 landing bare
regressed handoff.ship_and_archive (a HOLDER-initiated archive of a handoff
the calling session itself holds — cs_claim_holder_live cannot tell a
self-claim from a peer's, so it read the self-claim as live and skipped
archival on the ordinary ship path). Cases (e)/(f) below pin the fix and its
guard-rail: the opt-out archives despite a live claim, and stays off by
default so the background/boot-sweep shape is unaffected.

This module spawns real git (index/worktree divergence and SHA-reachability
cannot be exhibited by a mocked git) but stays its own small, scoped module
per the standing test-cull ruling (state/audits/2026-08-07-spawn-heavy-
test-excision-ledger.md) that real-git fixtures must not go ambient again —
ONE throwaway repo (module-scoped git init, function-scoped handoff seeding),
covering exactly the four cases this gate's dispatch brief named plus the
follow-up holder_initiated pair.

Negative-spec:
  - Does NOT re-test the SHA-gate or the deployment_state check — those are
    pinned elsewhere (test_archive_shipped_handoffs_disk_head_drift.py and the
    now-culled test_archive_shipped_handoffs.py's historical coverage, folded
    into boot_sweep's own two-repo tests). This file's job is Check 3 only.
  - Does NOT exercise the consumed_by/resolve_live_session_ids fallback —
    _is_shipped_terminal carries no such fallback by design (module docstring
    negative-spec): the claim-dir key is its sole liveness signal.
  - Does NOT exercise handoff_ship_archive.py's own handler end-to-end (the
    composite: handoff.stamp + handoff.transition + this op's act path) —
    that would need a claim-dir fixture wired through session.claims'
    claim-taking machinery, not just a bare mkdir'd claim dir, to be a
    faithful "this session holds it" shape. Cases (e)/(f) exercise the SAME
    _handle_act seam that composite calls, with holder_initiated threaded
    exactly as handoff_ship_archive.py threads it — see that module's Step 3
    comment. A true composite-level regression test is left as a follow-up if
    a lighter-weight claim fixture becomes available; reported, not built here.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.ops.fleet._common import handoff_claim_dir
from coordinator_core.ops.fleet.archive_shipped_handoffs import (
    _handle_act,
    _is_shipped_terminal,
    _scan_shipped,
)

# Real-git spawn is load-bearing: Check 3's live-claim gate is verified
# against SHA-reachability of a genuinely committed `shipped_in` and a real
# `state/handoffs/` commit history — a mocked git has no object database to
# resolve reachability against. The spawn ratchet's `_BASELINE` is
# shrink-only pre-existing residue and is explicitly not the route for
# this file -- coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_CS_CLAIM_HOLDER_LIVE_PATCH = (
    "coordinator_core.ops.fleet.archive_shipped_handoffs.cs_claim_holder_live"
)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    # popup-intentional-last-resort — test-only real-git spawn, mirrors this
    # directory's sibling disk_head_drift module; no console window risk on
    # the CI/dev platforms this suite runs on.
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "test@example.invalid"], root)
    _git(["config", "user.name", "test"], root)
    (root / "state" / "handoffs").mkdir(parents=True)
    seed = root / "seed.txt"
    seed.write_text("seed\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _seed_shipped(root: Path, name: str, sha: str) -> Path:
    """Write + commit a handoff with deployment_state:shipped and a reachable
    shipped_in — both checks 1/2 pass, isolating whatever Check 3 does."""
    path = root / "state" / "handoffs" / name
    path.write_text(
        "---\n"
        "status: claimed\n"
        "deployment_state: shipped\n"
        f"shipped_in: {sha}\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", f"add {name}"], root)
    return path


def _head_sha(root: Path) -> str:
    return _git(["rev-parse", "HEAD"], root).stdout.strip()


# ---------------------------------------------------------------------------
# (a) shipped + reachable shipped_in + LIVE claim dir -> retained
# ---------------------------------------------------------------------------


def test_live_claim_dir_retains_shipped_candidate(repo: Path):
    sha = _head_sha(repo)
    name = "2026-08-13-live-claim.md"
    handoff_path = _seed_shipped(repo, name, sha)

    claim_dir = handoff_claim_dir(repo, handoff_path)
    claim_dir.mkdir(parents=True, exist_ok=True)

    with patch(_CS_CLAIM_HOLDER_LIVE_PATCH, return_value=True):
        is_terminal, reason = _run(_is_shipped_terminal(handoff_path, repo, repo))
        candidates = _run(_scan_shipped(repo, common_dir=repo))

    assert is_terminal is False
    assert reason == "live claim (claim-dir holder live)"
    assert candidates == [], f"live-claim-held candidate must not be scanned; got {candidates!r}"


# ---------------------------------------------------------------------------
# (b) shipped + reachable shipped_in + dead/absent claim dir -> archived as before
# ---------------------------------------------------------------------------


def test_dead_claim_dir_still_archives(repo: Path):
    sha = _head_sha(repo)
    name = "2026-08-13-dead-claim.md"
    handoff_path = _seed_shipped(repo, name, sha)
    cid = f"state/handoffs/{name}"

    claim_dir = handoff_claim_dir(repo, handoff_path)
    claim_dir.mkdir(parents=True, exist_ok=True)

    with patch(_CS_CLAIM_HOLDER_LIVE_PATCH, return_value=False):
        is_terminal, reason = _run(_is_shipped_terminal(handoff_path, repo, repo))
        act = _run(_handle_act("already-terminal", repo, [cid], common_dir=repo))

    assert is_terminal is True, f"dead claim-dir holder must not block terminality; got {reason!r}"
    acted_ids = [item["id"] for item in act.get("acted", [])]
    assert cid in acted_ids, f"dead-holder claim-dir lock must not strand the handoff; got {act!r}"


def test_absent_claim_dir_still_archives(repo: Path):
    sha = _head_sha(repo)
    name = "2026-08-13-no-claim.md"
    handoff_path = _seed_shipped(repo, name, sha)
    cid = f"state/handoffs/{name}"

    # No claim dir created at all — the pre-2026-08-13 baseline shape.
    act = _run(_handle_act("already-terminal", repo, [cid], common_dir=repo))
    acted_ids = [item["id"] for item in act.get("acted", [])]
    assert cid in acted_ids, f"absent claim dir must not block archival; got {act!r}"


# ---------------------------------------------------------------------------
# (c) cs_claim_holder_live raising -> retained (fail-closed-to-keep)
# ---------------------------------------------------------------------------


def test_claim_liveness_probe_exception_retains_candidate(repo: Path):
    sha = _head_sha(repo)
    name = "2026-08-13-probe-error.md"
    handoff_path = _seed_shipped(repo, name, sha)

    claim_dir = handoff_claim_dir(repo, handoff_path)
    claim_dir.mkdir(parents=True, exist_ok=True)

    with patch(_CS_CLAIM_HOLDER_LIVE_PATCH, side_effect=OSError("boom")):
        is_terminal, reason = _run(_is_shipped_terminal(handoff_path, repo, repo))

    assert is_terminal is False
    assert reason == "live claim (claim-dir holder live)", (
        f"an unreadable/erroring claim-dir probe must degrade to RETAIN, "
        f"never assume-terminal; got {reason!r}"
    )


# ---------------------------------------------------------------------------
# (d) act-path re-verify (D2(iv)) catches a claim that went live between
#     scan and act
# ---------------------------------------------------------------------------


def test_act_time_reverify_catches_claim_gone_live_after_scan(repo: Path):
    sha = _head_sha(repo)
    name = "2026-08-13-drift-to-live.md"
    handoff_path = _seed_shipped(repo, name, sha)
    cid = f"state/handoffs/{name}"

    # Scan-time: no claim dir yet — candidate surfaces normally.
    with patch(_CS_CLAIM_HOLDER_LIVE_PATCH, return_value=False):
        candidates = _run(_scan_shipped(repo, common_dir=repo))
    assert any(p == handoff_path for p, _ in candidates), (
        f"candidate must surface at scan time with no claim dir; got {candidates!r}"
    )

    # Between scan and act, a session claims the handoff (claim dir appears, live).
    claim_dir = handoff_claim_dir(repo, handoff_path)
    claim_dir.mkdir(parents=True, exist_ok=True)

    with patch(_CS_CLAIM_HOLDER_LIVE_PATCH, return_value=True):
        act = _run(_handle_act("already-terminal", repo, [cid], common_dir=repo))

    acted_ids = [item["id"] for item in act.get("acted", [])]
    assert cid not in acted_ids, (
        f"D2(iv) act-time re-verify must catch the newly-live claim; got {act!r}"
    )
    skipped_reasons = [
        item.get("reason", "") for item in act.get("skipped", []) if item.get("id") == cid
    ]
    assert skipped_reasons, f"candidate must appear in skipped[]; got {act!r}"
    assert any("terminality-drift" in r and "live claim" in r for r in skipped_reasons), (
        f"skip reason must name the terminality drift and the live claim; got {skipped_reasons!r}"
    )


# ---------------------------------------------------------------------------
# (e) holder_initiated opt-out (2026-08-13 regression fix): a self-held live
#     claim must NOT block handoff.ship_and_archive's own archive call, and
#     the opt-out must never become an accidental default for the background
#     (boot-sweep-shaped) path.
# ---------------------------------------------------------------------------


def test_holder_initiated_archives_despite_live_claim(repo: Path):
    """The ship_and_archive shape: holder_initiated=True must archive a
    candidate even though its OWN claim dir reads live — cs_claim_holder_live
    cannot tell a self-claim from a peer's, so this op's opt-out must bypass
    Check 3 entirely rather than rely on that distinction."""
    sha = _head_sha(repo)
    name = "2026-08-13-holder-initiated.md"
    handoff_path = _seed_shipped(repo, name, sha)
    cid = f"state/handoffs/{name}"

    claim_dir = handoff_claim_dir(repo, handoff_path)
    claim_dir.mkdir(parents=True, exist_ok=True)

    with patch(_CS_CLAIM_HOLDER_LIVE_PATCH, return_value=True):
        act = _run(
            _handle_act(
                "already-terminal", repo, [cid], common_dir=repo, holder_initiated=True
            )
        )

    acted_ids = [item["id"] for item in act.get("acted", [])]
    assert cid in acted_ids, (
        f"holder_initiated=True must archive despite a live (self-)claim; got {act!r}"
    )


def test_holder_initiated_default_false_still_retains_on_live_claim(repo: Path):
    """Guard against the opt-out ever becoming a silent default: the SAME
    fixture as (a), through _handle_act rather than _is_shipped_terminal
    directly, with holder_initiated left at its default (False) — the
    background/boot-sweep shape must still retain."""
    sha = _head_sha(repo)
    name = "2026-08-13-background-live-claim.md"
    handoff_path = _seed_shipped(repo, name, sha)
    cid = f"state/handoffs/{name}"

    claim_dir = handoff_claim_dir(repo, handoff_path)
    claim_dir.mkdir(parents=True, exist_ok=True)

    with patch(_CS_CLAIM_HOLDER_LIVE_PATCH, return_value=True):
        act = _run(_handle_act("already-terminal", repo, [cid], common_dir=repo))

    acted_ids = [item["id"] for item in act.get("acted", [])]
    assert cid not in acted_ids, (
        f"default holder_initiated=False must still retain on a live claim; got {act!r}"
    )

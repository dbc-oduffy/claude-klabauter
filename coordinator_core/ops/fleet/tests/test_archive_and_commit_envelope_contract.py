
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet._common import Move, archive_and_commit
from coordinator_core.ops.fleet.tests.archive_git_free_seam import make_recording_mover
from coordinator_core.win_portability import no_console_creationflags

# spawn ratchet's `_BASELINE` is shrink-only pre-existing residue and is
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True, **no_console_creationflags(),
    )


def _run(coro):
    return asyncio.run(coro)


def test_real_archive_and_commit_envelope_matches_stub_default_shape(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q", "-b", "main"], root)
    _git(["config", "user.email", "test@example.invalid"], root)
    _git(["config", "user.name", "test"], root)

    handoffs = root / "state" / "handoffs"
    handoffs.mkdir(parents=True)

    clean_src = handoffs / "2026-08-01-clean.md"
    clean_content = "---\nstatus: claimed\ndeployment_state: shipped\n---\n\nBody.\n"
    clean_src.write_text(clean_content, encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed: clean handoff"], root)

    clean_dst = root / "archive" / "handoffs" / "2026-08" / clean_src.name
    clean_move = Move(
        src=clean_src, dst=clean_dst,
        candidate_id="state/handoffs/2026-08-01-clean.md",
    )

    real_acted, real_failed = _run(
        archive_and_commit(worktree_root=root, moves=[clean_move], subject="fleet: archive 1 shipped handoff(s)")
    )

    stub_mover = make_recording_mover()
    stub_acted, stub_failed = _run(stub_mover(root, [clean_move], "fleet: archive 1 shipped handoff(s)"))

    assert real_acted == stub_acted == [{"id": clean_move.candidate_id, "archived": True}]
    assert real_failed == stub_failed == []

    drift_src = handoffs / "2026-08-02-missing.md"

    drift_dst = root / "archive" / "handoffs" / "2026-08" / drift_src.name
    drift_move = Move(
        src=drift_src, dst=drift_dst,
        candidate_id="state/handoffs/2026-08-02-missing.md",
    )

    real_drift_acted, real_drift_failed = _run(
        archive_and_commit(worktree_root=root, moves=[drift_move], subject="fleet: archive 1 shipped handoff(s)")
    )

    assert real_drift_acted == []
    assert len(real_drift_failed) == 1
    real_item = real_drift_failed[0]
    assert set(real_item.keys()) == {"id", "reason"}
    assert real_item["id"] == drift_move.candidate_id
    assert isinstance(real_item["reason"], str) and real_item["reason"]

    stub_failure_mover = make_recording_mover(
        response=([], [{"id": drift_move.candidate_id, "reason": real_item["reason"]}])
    )
    stub_drift_acted, stub_drift_failed = _run(
        stub_failure_mover(root, [drift_move], "fleet: archive 1 shipped handoff(s)")
    )
    assert stub_drift_acted == real_drift_acted == []
    assert set(stub_drift_failed[0].keys()) == set(real_item.keys())
    assert stub_drift_failed[0]["id"] == real_item["id"]

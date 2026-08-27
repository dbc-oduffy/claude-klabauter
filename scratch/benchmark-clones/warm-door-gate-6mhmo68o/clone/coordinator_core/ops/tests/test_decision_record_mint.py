"""
coordinator_core.ops.tests.test_decision_record_mint — coverage for
"decision_record.mint_id" / "decision_record.release_id".

Coverage:
  (a) registration guard — both ops fire @register_op.
  (b) empty worktree -> first mint returns DR-1, reservation file created.
  (c) existing docs/decisions/DR-<N>-*.md files -> mint returns max+1,
      ignoring any bare (non-DR-prefixed) .md siblings.
  (d) an outstanding (unexpired) reservation raises the floor above the
      existing .md max, even with no matching .md file yet.
  (e) an EXPIRED reservation is swept (deleted) and does NOT raise the floor
      — the number becomes available again.
  (f) concurrent mints (real OS processes, not threads) against the same
      worktree never return the same number — the property the whole
      mechanism exists for.
  (g) release_id deletes an outstanding reservation and reports
      released=True; a second release of the same number reports False.
  (h) repo_root=None -> setup error from both handlers, no write attempted.
  (i) exhausting _MAX_MINT_ATTEMPTS raises RuntimeError from the library call.

Concurrency test rationale (f): a thread-based test would not exercise the
real race, because CPython's GIL already serializes each thread's call into
`os.open` one at a time from Python's perspective for anything shy of an
actual OS-level context switch mid-syscall — real OS processes are the only
way to force two callers to actually contend for the same
`O_CREAT|O_EXCL` create at the filesystem level, mirroring how
`coordinator_core/tests/test_locked_write_held_lock.py` proves its own
crash-safety property with `subprocess.Popen` rather than threads.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.ipc import get_op_handler
from coordinator_core.ops.decision_record_mint import (
    _MAX_MINT_ATTEMPTS,
    _reservation_path,
    _reservations_dir,
    mint_next_dr_id,
    release_dr_id,
)


def test_ops_are_registered() -> None:
    assert get_op_handler("decision_record.mint_id") is not None
    assert get_op_handler("decision_record.release_id") is not None


def test_mint_first_number_in_empty_worktree(tmp_path: Path) -> None:
    number = mint_next_dr_id(tmp_path, holder="test", title="first")
    assert number == 1
    reservation = _reservation_path(_reservations_dir(tmp_path), 1)
    assert reservation.is_file()


def test_mint_floors_above_existing_dr_files(tmp_path: Path) -> None:
    decisions = tmp_path / "docs" / "decisions"
    decisions.mkdir(parents=True)
    (decisions / "DR-5-some-title.md").write_bytes(b"---\nid: DR-5\n---\n")
    (decisions / "DR-12-other-title.md").write_bytes(b"---\nid: DR-12\n---\n")
    (decisions / "not-a-dr-file.md").write_bytes(b"hello\n")

    number = mint_next_dr_id(tmp_path)
    assert number == 13


def test_outstanding_reservation_raises_floor_above_md_max(tmp_path: Path) -> None:
    decisions = tmp_path / "docs" / "decisions"
    decisions.mkdir(parents=True)
    (decisions / "DR-3-x.md").write_bytes(b"---\nid: DR-3\n---\n")

    first = mint_next_dr_id(tmp_path)
    assert first == 4

    second = mint_next_dr_id(tmp_path)
    assert second == 5
    assert second != first


def test_expired_reservation_is_swept_and_reclaimed(tmp_path: Path, monkeypatch) -> None:
    reservations_dir = _reservations_dir(tmp_path)
    reservations_dir.mkdir(parents=True)
    stale = _reservation_path(reservations_dir, 7)
    stale.write_bytes(b'{"reserved_at": "old"}\n')

    old_time = time.time() - (20 * 86400)
    os.utime(stale, (old_time, old_time))

    number = mint_next_dr_id(tmp_path)
    assert number == 1
    assert not stale.exists()


def test_release_id_deletes_reservation_once(tmp_path: Path) -> None:
    number = mint_next_dr_id(tmp_path)
    assert release_dr_id(tmp_path, number) is True
    assert release_dr_id(tmp_path, number) is False


def test_handler_missing_repo_root_errors(tmp_path: Path) -> None:
    mint_handler = get_op_handler("decision_record.mint_id")
    result = mint_handler({}, repo_root=None)
    assert result["exit_code"] == 1
    assert result["id"] is None

    release_handler = get_op_handler("decision_record.release_id")
    result2 = release_handler({"number": 1}, repo_root=None)
    assert result2["exit_code"] == 1
    assert result2["released"] is False


def test_exhausted_attempts_raises(tmp_path: Path, monkeypatch) -> None:
    # Force every candidate in [1, 3] to already be taken while making the
    # floor-selection scan itself report 0 (as if a genuinely concurrent
    # scan-vs-create race kept losing) -- the shape `_MAX_MINT_ATTEMPTS`
    # exists to bound, not a state the ordinary floor-then-create path can
    # reach on its own (see module docstring: the scan only picks a cheap
    # STARTING point, so a stale scan result is exactly the case this
    # function must not spin forever on).
    monkeypatch.setattr(
        "coordinator_core.ops.decision_record_mint._MAX_MINT_ATTEMPTS", 3
    )
    monkeypatch.setattr(
        "coordinator_core.ops.decision_record_mint._reserved_max", lambda _dir: 0
    )
    reservations_dir = _reservations_dir(tmp_path)
    reservations_dir.mkdir(parents=True)
    for n in range(1, 4):
        _reservation_path(reservations_dir, n).write_bytes(b"{}\n")

    with pytest.raises(RuntimeError):
        mint_next_dr_id(tmp_path)


# ---------------------------------------------------------------------------
# Concurrency proof — real OS processes racing the same worktree.
# ---------------------------------------------------------------------------

_WORKER_SNIPPET = """
import sys
sys.path.insert(0, {sys_path!r})
from coordinator_core.ops.decision_record_mint import mint_next_dr_id
from pathlib import Path
number = mint_next_dr_id(Path({worktree!r}), holder="worker")
sys.stdout.write(str(number))
"""


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_concurrent_mints_never_collide(tmp_path: Path) -> None:
    repo_root_for_import = str(Path(__file__).resolve().parents[3])
    n_workers = 8
    procs = []
    for _ in range(n_workers):
        code = _WORKER_SNIPPET.format(sys_path=repo_root_for_import, worktree=str(tmp_path))
        procs.append(
            subprocess.Popen(
                [sys.executable, "-c", code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        )

    numbers = []
    for proc in procs:
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, f"worker failed: {err}"
        numbers.append(int(out.strip()))

    assert len(numbers) == len(set(numbers)), f"collision(s) in {numbers}"
    assert sorted(numbers) == list(range(1, n_workers + 1))

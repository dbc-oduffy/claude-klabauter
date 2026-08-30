"""
coordinator_core.ops.fleet.tests.test_archive_and_commit_envelope_contract

AC12 of docs/plans/2026-08-13-archive-family-coverage-restoration.md: the
~380 archive-family tests ported onto archive_git_free_seam.py's
`make_recording_mover` stub are only honest against production if the
stub's return envelope is proven equal, in shape, to the real
`archive_and_commit`'s. Nothing enforced that before this module.

Two cases, ONE throwaway repo, ONE test function (governed real-git
pattern; `popup-intentional-last-resort` marker below; this module is
deliberately its own scoped module per the standing test-cull ruling in
state/audits/2026-08-07-spawn-heavy-test-excision-ledger.md, mirroring
test_archive_and_commit_disk_head_drift.py rather than importing it):

1. Success: one move that lands cleanly. Real `archive_and_commit`
   returns `(acted, failed)` with `acted == [{"id": ..., "archived":
   True}]`, `failed == []` — asserted to equal exactly what
   `archive_git_free_seam.make_recording_mover()`'s default response
   builds for the same `moves` list.
2. Failure: a move whose src does not exist on disk at all, so the
   in-process `os.replace` (F-5 swap, 2026-08-21 — see _common.py's
   archive_and_commit docstring) raises and the item lands in `failed[]`
   with a `replace-failed` reason. Real `archive_and_commit` returns
   `acted == []`, `failed == [{"id": ..., "reason": <replace-failed
   reason>}]` — the KEYS present on the `failed[]` item (`id`, `reason`)
   are asserted to match what a caller-side `failed[]` entry the stub can
   be told to return (via `make_recording_mover(response=...)`) is shaped
   like. This is a key-shape/type assertion, not a literal-string pin —
   the reason TEXT is free to keep evolving; the envelope SHAPE a consumer
   parses (which keys exist, on which item) is the contract this test
   protects.

If a future change makes the real envelope's shape diverge from what the
stub can express (e.g. a new required key on every `acted[]`/`failed[]`
item), THIS test is where that shows up first — before it silently
invalidates the ~380 stub-driven tests.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.fleet._common import Move, archive_and_commit
from coordinator_core.ops.fleet.tests.archive_git_free_seam import make_recording_mover
from coordinator_core.win_portability import no_console_creationflags

# Real-git spawn is load-bearing: this file proves the real archive_and_commit
# envelope shape against a real repo (clean move + disk/HEAD drift refusal),
# not against a mock -- no monkeypatch stands in for the production entry
# point ~380 stub-driven tests are built on. Single test, single repo; no
# module-scope hoist needed since both cases share the one function. The
# spawn ratchet's `_BASELINE` is shrink-only pre-existing residue and is
# explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
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

    # --- Case 1: clean move, lands successfully. ---
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

    # --- Case 2: src does not exist on disk -- os.replace raises. ---
    drift_src = handoffs / "2026-08-02-missing.md"  # never created

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

    # The stub can be told to produce the SAME shape a caller must handle,
    # via make_recording_mover(response=...) -- proving a test built on
    # that call shape is asserting against a real-envelope-compatible
    # failed[] item, not an invented one.
    stub_failure_mover = make_recording_mover(
        response=([], [{"id": drift_move.candidate_id, "reason": real_item["reason"]}])
    )
    stub_drift_acted, stub_drift_failed = _run(
        stub_failure_mover(root, [drift_move], "fleet: archive 1 shipped handoff(s)")
    )
    assert stub_drift_acted == real_drift_acted == []
    assert set(stub_drift_failed[0].keys()) == set(real_item.keys())
    assert stub_drift_failed[0]["id"] == real_item["id"]

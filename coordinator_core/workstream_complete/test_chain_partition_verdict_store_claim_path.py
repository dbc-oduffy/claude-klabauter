"""
coordinator_core.workstream_complete.test_chain_partition_verdict_store_claim_path

Evidence test for chunk C3 (docs/plans/2026-08-05-in-process-writers-
declare-their-writes.md): `write_verdict_record` performs its atomic
`os.fdopen` write under `state/ceremony/wsc-chain-partition-verdict/` and is
called in-process (its only non-test caller is `coordinator/bin/wsc-
coverage-gate-runner.py`, which confirms this never transits
`ipc.dispatch_message`). Before this chunk, that write recorded no
`session.scope.touch()` claim, so the record could never enter a wrap
ceremony's `safe_paths` and no closing session could ever commit it.

Fixture shape mirrors `coordinator_core/ops/session/tests/
test_in_process_writer_claim_path.py` (the sibling oracle for chunk C1) —
same `_make_repo` git-init dance, same `core.init` + `started_at` stub for
the "already-spawned session" case, same phantom-session-dir assertion
shape for the C6-guard case (here: AC6's phantom-live-peer guard, ported
per § Key mechanism facts).

Spec backlink: docs/plans/2026-08-05-in-process-writers-declare-their-writes.md § C3
Module under test: coordinator_core/workstream_complete/chain_partition_verdict_store.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.session import safe_commit_offer
from coordinator_core.session import core
from coordinator_core.workstream_complete import chain_partition_verdict_store as store


def _make_repo(tmp_path: Path) -> Path:
    """Mirrors test_in_process_writer_claim_path._make_repo — check=True on
    every fixture-setup git call so a silent setup failure cannot masquerade
    as a passing test."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True
    )
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_written_verdict_record_is_committable_by_its_own_session(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    repo = _make_repo(repo_root)
    session_id = "sess-verdict-writer"
    core.init(session_id, cwd=str(repo))
    sdir = Path(core.session_dir(session_id, cwd=str(repo)))
    (sdir / "started_at").write_text("2000-01-01T00:00:00Z")

    # The real producer call path -- no test double, no monkeypatched writer.
    path = store.write_verdict_record(
        repo,
        session_id=session_id,
        verdict="PARTITION-MANDATORY",
        from_handoff="state/handoffs/x.md",
        git_range=None,
        basis="plan_oracle=4(...) tier=B",
        tier="B",
    )
    assert path.is_file(), "fixture failure: verdict record not on disk"
    rel_path = path.relative_to(repo).as_posix()

    offer = safe_commit_offer.compute_offer(session_id, cwd=str(repo))

    assert rel_path in offer["safe_paths"], (
        "the session whose write_verdict_record call wrote this record "
        "cannot commit it: "
        f"safe_paths={offer['safe_paths']!r} orphans={offer['orphans']!r} "
        f"excluded={offer['excluded']!r}"
    )


def test_write_verdict_record_does_not_materialize_a_phantom_session_dir(
    tmp_path: Path,
) -> None:
    """AC6: no claim call may materialize a session dir that did not
    already exist. ``never-spawned-session`` is never ``core.init()``'d
    before the write call, so ``write_verdict_record`` must skip the claim
    rather than let ``scope.touch``'s lazy ``core.init()`` create one -- the
    phantom-live-peer hazard ``coordinator_core/ipc.py``'s F1 comment
    documents."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    repo = _make_repo(repo_root)
    session_id = "never-spawned-session"

    assert core.session_dir(session_id, cwd=str(repo)) == "" or not Path(
        core.session_dir(session_id, cwd=str(repo))
    ).is_dir(), "fixture failure: session dir already exists before writing"

    path = store.write_verdict_record(
        repo,
        session_id=session_id,
        verdict="single-reviewer-ok",
        from_handoff="state/handoffs/y.md",
        git_range=None,
        basis="",
        tier="none",
    )
    assert path.is_file(), "fixture failure: verdict record not on disk"

    assert not Path(core.session_dir(session_id, cwd=str(repo))).is_dir(), (
        "writing a verdict record for a never-initialized session must not "
        "materialize a phantom session dir via scope.touch's lazy core.init()"
    )

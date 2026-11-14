"""A land_run whose deadline fires once the commit began answers from git, not from the halt text.

Driven through the default `dispatch.land_run` product entry under a keyed dispatch. The deadline is
forced at a `land_intent.mark` call: at COMMITTED (after the commit landed), at COMMITTING (before it).
"""

from __future__ import annotations

from contextlib import ExitStack

import pytest

from coordinator_core.benchmarks import land_run_slow_disk_profile as prof
from coordinator_core.ops.dispatch_emit import land_deadline, land_intent, land_run, run_token
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.warm import dispatch_ack, dispatch_fence

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

KEY = "321-654"


@pytest.fixture
def landing(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", prof.SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("COORDINATOR_INVOCATION_ORIGIN", "benchmark")
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    claim_dir = tmp_path / "svc" / "dispatch-claims"
    claim_dir.mkdir(parents=True)
    dispatch_fence.hold(str(claim_dir), KEY)
    assert dispatch_ack.journal_dir_beside(claim_dir)
    with ExitStack() as stack:
        root, base = prof.build_land_fixture(tmp_path / "fx", mode="clean", tracked=5, chain=2, stack=stack)
        try:
            yield root, base
        finally:
            dispatch_fence.release()
            land_intent._held = None


def _deadline_at(monkeypatch, phase: str, *, extra_expected: dict | None = None) -> None:
    """Run the real `mark`; for `phase` raise the deadline right after it. `extra_expected` adds a
    path the commit will never carry to the committing record."""
    real = land_intent.mark

    def mark(p, sha=None, **kw):
        if p == land_intent.PHASE_COMMITTING and extra_expected:
            kw["expected_blobs"] = {**(kw.get("expected_blobs") or {}), **extra_expected}
        real(p, sha, **kw)
        if p == phase:
            raise land_deadline.LandDeadlineExceeded("land_run.test_leg", [{"leg": "land_run.test_leg", "elapsed_secs": 1.0}])

    monkeypatch.setattr(land_intent, "mark", mark)


def _fire(root, base) -> dict:
    token = run_token.mint(root, prof.RUN_ID, prof.SCRIPT, session_id=prof.SELF)
    review = {**reviewed(), "prep": {"run_base_sha": base}}
    return land_run._handler(
        {"script_path": prof.SCRIPT, "incomplete_chunks": [], "inline_review": review,
         "session_id": prof.SELF, "run_id": prof.RUN_ID, "run_token": token},
        repo_root=root / ".git",
    )


def _head(root) -> str:
    return prof._git(["rev-parse", "HEAD"], root).strip()


def test_deadline_after_commit_reports_committed_with_sha(landing, monkeypatch):
    root, base = landing
    before = _head(root)
    _deadline_at(monkeypatch, land_intent.PHASE_COMMITTED)
    reply = _fire(root, base)
    assert _head(root) != before
    assert reply["committed"] is True, reply
    assert reply["sha"] == _head(root)
    assert reply["reconciled_from"] == "git"
    assert "refused" not in reply and "queued_for_resume" not in reply


def test_committing_record_carries_base_and_the_expected_map(landing, monkeypatch):
    root, base = landing
    _deadline_at(monkeypatch, land_intent.PHASE_COMMITTED)
    _fire(root, base)
    body = land_intent._held[2]
    assert body["base"] == base
    assert set(body["expected_blobs"]) == {prof.PATH_P}
    assert set(body["expected_blobs"][prof.PATH_P]) == {"sha256"}


def test_deadline_before_commit_reports_not_committed(landing, monkeypatch):
    root, base = landing
    before = _head(root)
    _deadline_at(monkeypatch, land_intent.PHASE_COMMITTING)
    reply = _fire(root, base)
    assert _head(root) == before
    assert reply["committed"] is False
    assert reply["sha"] is None
    assert reply["refused"] == land_deadline.REFUSED
    assert "queued_for_resume" not in reply


def test_partial_commit_named_loudly(landing, monkeypatch):
    root, base = landing
    _deadline_at(
        monkeypatch, land_intent.PHASE_COMMITTED, extra_expected={"src/never.py": {"blob": "1" * 40}},
    )
    reply = _fire(root, base)
    assert reply["committed"] is False
    assert reply["refused"] == land_run.PARTIAL_COMMIT_REFUSED
    assert _head(root) in reply["error"]
    assert "src/never.py" in reply["error"]
    assert prof.PATH_P not in reply["error"].split("missing:", 1)[1]

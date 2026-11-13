"""A product landing whose deadline fires at or before the commit leg with nothing committed queues a
land-resume entry; the same params then land exactly once. After the commit phase began, or past the
entry's bounds, the halt is unchanged.

Drives the real `land_run._handler` over a git repo; only `land_deadline.check` is made to raise.
"""

from __future__ import annotations

import time

import pytest

from coordinator_core.ops.dispatch_emit import land_deadline, land_intent, land_leg_record, land_resume, land_run
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import (  # noqa: F401
    RUN,
    SELF,
    _author,
    _declare,
    _dirty,
    _git,
    _land,
    _record_dir,
    _tree,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

#: script_read is retried once in the same call, so its stall must fire on both attempts.
_FIRES = {"land_run.script_read": 2, "land_run.integrity": 1, "land_run.commit": 1}


@pytest.fixture
def queue(tmp_path, monkeypatch):
    qdir = tmp_path / "land-resume-queue"
    monkeypatch.setattr(land_resume, "queue_dir", lambda root=None: qdir)
    return qdir


def _stall_at(monkeypatch, leg: str, times: int) -> None:
    real, left = land_deadline.check, [times]

    def check(name):
        if name == leg and left[0] > 0:
            left[0] -= 1
            raise land_deadline.LandDeadlineExceeded(leg, [], land_deadline.BOUND_BUDGET)
        real(name)

    monkeypatch.setattr(land_deadline, "check", check)


def _prepare(repo):
    _author(repo, "src/a.py", "a = 1\n")
    _declare(repo, "src/a.py")
    record = _record_dir(repo)
    return record, _git(["rev-parse", "HEAD"], repo).strip()


@pytest.mark.parametrize("leg", sorted(_FIRES))
def test_a_deadline_before_the_commit_queues_and_the_resume_lands_exactly_once(repo, queue, monkeypatch, leg):
    record, head = _prepare(repo)
    _stall_at(monkeypatch, leg, _FIRES[leg])

    halted = _land(repo)

    assert halted["refused"] == land_deadline.REFUSED and halted["leg"] == leg
    assert halted["committed"] is False
    assert halted["queued_for_resume"] is True
    entry = land_resume.load(None, RUN)
    assert entry is not None and entry["last_leg"] == leg and entry["attempts"] == 1
    assert _git(["rev-parse", "HEAD"], repo).strip() == head
    legs_file = record_dir(repo, RUN) / land_leg_record.FILE_NAME
    assert '"outcome": "land-deadline"' in legs_file.read_text(encoding="utf-8")

    resumed = _land(repo)

    assert resumed["committed"] is True, resumed
    assert _git(["rev-parse", "HEAD~1"], repo).strip() == head
    assert _tree(repo) == sorted(["src/a.py", *record])
    assert _git(["rev-list", "--count", f"{head}..HEAD"], repo).strip() == "1"
    assert '"outcome": "landed"' in legs_file.read_text(encoding="utf-8")
    assert not [p for p in _dirty(repo) if "receipt" in p]


def test_a_deadline_after_the_commit_phase_began_enqueues_nothing(repo, queue, monkeypatch):
    def committing(mode, root, repo_root, params):
        land_intent._held = (None, "k", {"phase": land_intent.PHASE_COMMITTING})
        raise land_deadline.LandDeadlineExceeded("land_run.commit", [], land_deadline.BOUND_BUDGET)

    monkeypatch.setattr(land_run, "_dispatch_mode", committing)
    monkeypatch.setattr(land_intent, "_held", None)

    out = _land(repo)

    assert out["refused"] == land_deadline.REFUSED
    assert "queued_for_resume" not in out
    assert land_resume.load(None, RUN) is None


def test_an_expired_entry_halts_as_before(repo, queue, monkeypatch):
    _prepare(repo)
    assert land_resume.enqueue(None, RUN, {"run_id": RUN}, "land_run.commit")
    entry = land_resume.load(None, RUN)
    monkeypatch.setattr(time, "time", lambda: entry["enqueued_at"] + land_resume.MAX_AGE_SECS + 1.0)
    _stall_at(monkeypatch, "land_run.commit", 1)

    out = _land(repo)

    assert out["refused"] == land_deadline.REFUSED and out["leg"] == "land_run.commit"
    assert "queued_for_resume" not in out


def test_a_halted_mode_deadline_enqueues_nothing(repo, queue, monkeypatch):
    def deadline(mode, root, repo_root, params):
        raise land_deadline.LandDeadlineExceeded("land_run_halt._land.commit", [], land_deadline.BOUND_BUDGET)

    monkeypatch.setattr(land_run, "_dispatch_mode", deadline)
    monkeypatch.setattr(land_run.contract, "mode_of", lambda params: "halted")

    out = _land(repo)

    assert out["refused"] == land_deadline.REFUSED
    assert "queued_for_resume" not in out
    assert land_resume.load(None, RUN) is None

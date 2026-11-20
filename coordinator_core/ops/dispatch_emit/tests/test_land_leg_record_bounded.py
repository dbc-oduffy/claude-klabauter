"""land_leg_record.write is best-effort: a blocked replace returns None to its caller inside the write
deadline, an unblocked write lands as before, and a late halt worker never replaces a `landed` record."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_leg_record

RUN = "warp-bounded-run-001"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _file(root: Path) -> Path:
    return root / "state" / "warp" / RUN / land_leg_record.FILE_NAME


def _outcome(root: Path) -> str:
    return json.loads(_file(root).read_text(encoding="utf-8"))["outcome"]


def _join_workers() -> None:
    for t in threading.enumerate():
        if t.name.startswith("bounded-io:"):
            t.join(5.0)


def _write(root: Path, outcome: str):
    return land_leg_record.write(root, RUN, outcome=outcome, leg="probe", bound_by=None, legs=[])


def test_unblocked_write_lands_as_before(repo: Path) -> None:
    rel = _write(repo, land_leg_record.OUTCOME_HALT)
    assert rel == f"state/warp/{RUN}/{land_leg_record.FILE_NAME}"
    assert _outcome(repo) == land_leg_record.OUTCOME_HALT
    assert not list(_file(repo).parent.glob("*.tmp"))


def test_blocked_replace_returns_none_within_the_deadline(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    real = os.replace

    def blocking(src, dst, *a, **kw):
        if str(dst).endswith(land_leg_record.FILE_NAME):
            gate.wait(10)
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", blocking)
    try:
        assert _write(repo, land_leg_record.OUTCOME_HALT) is None
        assert not gate.is_set()
    finally:
        gate.set()
        _join_workers()
    assert _outcome(repo) == land_leg_record.OUTCOME_HALT


def test_a_halt_never_replaces_a_landed_record(repo: Path) -> None:
    assert _write(repo, land_leg_record.OUTCOME_LANDED) is not None
    assert _write(repo, land_leg_record.OUTCOME_HALT) is not None
    assert _outcome(repo) == land_leg_record.OUTCOME_LANDED
    assert not list(_file(repo).parent.glob("*.tmp"))


def test_a_landed_record_replaces_a_halt_one(repo: Path) -> None:
    _write(repo, land_leg_record.OUTCOME_HALT)
    _write(repo, land_leg_record.OUTCOME_LANDED)
    assert _outcome(repo) == land_leg_record.OUTCOME_LANDED


def test_a_late_halt_worker_cannot_overwrite_a_newer_landed(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()
    real = os.replace

    def blocking(src, dst, *a, **kw):
        if str(dst).endswith(land_leg_record.FILE_NAME) and json.loads(Path(src).read_text(encoding="utf-8"))["outcome"] == land_leg_record.OUTCOME_HALT:
            gate.wait(10)
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", blocking)
    try:
        assert _write(repo, land_leg_record.OUTCOME_HALT) is None
        assert _write(repo, land_leg_record.OUTCOME_LANDED) is None
    finally:
        gate.set()
        _join_workers()
    assert _outcome(repo) == land_leg_record.OUTCOME_LANDED
    assert not list(_file(repo).parent.glob("*.tmp"))

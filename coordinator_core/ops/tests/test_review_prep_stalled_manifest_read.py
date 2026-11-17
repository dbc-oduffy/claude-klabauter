"""review prep: a stalled run-record read is re-read once and never reported as an absent one."""

from __future__ import annotations

import threading

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
from coordinator_core.ops.dispatch_emit.ask_contract import LEG_FOOTPRINT_FILE, record_dir
from coordinator_core.ops.tests.test_review_prep import _write, repo  # noqa: F401
from coordinator_core.ops.tests.test_review_prep_run_id import _stage

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-stall"


def _stall(monkeypatch, name: str, gate: threading.Event, times: int) -> list:
    """Block the first `times` reads of a file called `name` on `gate`."""
    real = hrr._read_file
    seen: list = []

    def gated(path):
        if path.name == name:
            seen.append(path)
            if len(seen) <= times:
                gate.wait(10)
        return real(path)

    monkeypatch.setattr(hrr, "_read_file", gated)
    return seen


def test_one_stall_past_the_deadline_still_returns_the_write_set(repo, monkeypatch):
    _write(repo, ["a/one.py"], 3)
    _stage(repo, ["a/one.py"], run_id=RUN)
    monkeypatch.setattr(hrr, "JSON_READ_DEADLINE_MS", 30.0)
    gate = threading.Event()
    threading.Timer(0.3, gate.set).start()
    seen = _stall(monkeypatch, "manifest.json", gate, times=1)
    paths, _held = rp.declared_paths_and_held(repo, RUN, own_session="sess-own")
    assert "a/one.py" in paths
    assert len(seen) >= 2  # the stalled read, then the bounded re-read


def test_leg_footprint_stall_is_re_read_too(repo, monkeypatch):
    _write(repo, ["a/one.py"], 3)
    _stage(repo, ["a/one.py"], footprint=["a/one.py"], run_id=RUN)
    monkeypatch.setattr(hrr, "JSON_READ_DEADLINE_MS", 30.0)
    gate = threading.Event()
    threading.Timer(0.3, gate.set).start()
    _stall(monkeypatch, LEG_FOOTPRINT_FILE, gate, times=1)
    paths, _held = rp.declared_paths_and_held(repo, RUN, leg=True, own_session="sess-own")
    assert "a/one.py" in paths


def test_a_read_that_stays_stalled_is_named_stalled_with_ms(repo, monkeypatch):
    _write(repo, ["a/one.py"], 3)
    _stage(repo, ["a/one.py"], run_id=RUN)
    monkeypatch.setattr(hrr, "JSON_READ_DEADLINE_MS", 30.0)
    monkeypatch.setattr(rp, "RUN_RECORD_REREAD_DEADLINE_MS", 80.0)
    gate = threading.Event()
    _stall(monkeypatch, "manifest.json", gate, times=99)
    try:
        with pytest.raises(rp.RunRecordReadStalled, match=r"no readable manifest for run 'ask-stall': stalled \d+ ms"):
            rp.declared_paths_and_held(repo, RUN, own_session="sess-own")
        reply = rp._handler({"run_id": RUN, "base": "a" * 40, "worktree": True}, repo)
        assert reply["error"].startswith(rp.REVIEW_PREP_BUSY_PREFIX) and "stalled" in reply["error"]
    finally:
        gate.set()


def test_absent_and_malformed_are_named_distinctly(repo):
    with pytest.raises(ValueError, match=r"no readable manifest for run 'ask-gone': absent"):
        rp.declared_paths_and_held(repo, "ask-gone", own_session="sess-own")
    d = record_dir(repo, "ask-bad")
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match=r"no readable manifest for run 'ask-bad': malformed"):
        rp.declared_paths_and_held(repo, "ask-bad", own_session="sess-own")
    (d / "manifest.json").write_text("[1]", encoding="utf-8")
    with pytest.raises(ValueError, match=r": malformed"):
        rp.declared_paths_and_held(repo, "ask-bad", own_session="sess-own")


def test_absent_manifest_is_not_re_read(repo, monkeypatch):
    calls: list = []
    real = hrr.bounded_json_state

    def counting(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(hrr, "bounded_json_state", counting)
    assert rp._read_run_record(record_dir(repo, "ask-gone2") / "manifest.json")[1] == "absent"
    assert len(calls) == 1

"""review prep: a manifest.json read that outlasts both read bounds is a retryable busy reply, never a refusal; absent and malformed still refuse with their cause.

Measured shape (Machine-a 2026-11-16): the warm engine stopped answering for ~13 s while review prep's
manifest worker read stayed pending past JSON_READ_DEADLINE_MS plus RUN_RECORD_REREAD_DEADLINE_MS,
yielding `no readable manifest for run ...: stalled <N> ms`. The stall is a read that completes late,
never a sharing error or a missing file, so the fake delays `_read_file` and then returns the real bytes.
"""

from __future__ import annotations

import subprocess
import threading

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.tests.test_review_prep import _write, repo  # noqa: F401
from coordinator_core.ops.tests.test_review_prep_run_id import RUN, _params, _stage

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

# Past both bounds at default arguments, so the first read and the one re-read both return STALLED.
STALL_S = (hrr.JSON_READ_DEADLINE_MS + rp.RUN_RECORD_REREAD_DEADLINE_MS) / 1000.0 + 0.4


def _delay_manifest_read(monkeypatch, seconds: float) -> threading.Event:
    """Hold every read of a file named manifest.json for `seconds`, then return its real bytes."""
    real = hrr._read_file
    release = threading.Event()

    def delayed(path):
        if path.name == "manifest.json":
            release.wait(seconds)
        return real(path)

    monkeypatch.setattr(hrr, "_read_file", delayed)
    return release


def _count_spawns(monkeypatch) -> list:
    spawned: list = []
    real_init = subprocess.Popen.__init__

    def counting(self, *a, **k):
        spawned.append(a[0] if a else k.get("args"))
        real_init(self, *a, **k)

    monkeypatch.setattr(subprocess.Popen, "__init__", counting)
    return spawned


def test_a_manifest_read_past_both_bounds_is_a_retryable_busy_reply_not_a_halt(repo, monkeypatch):
    _write(repo, ["a/one.py"], 3)
    _stage(repo, ["a/one.py"])
    release = _delay_manifest_read(monkeypatch, STALL_S)
    try:
        reply = rp._handler(_params(repo, "prep-stall", run_id=RUN), repo)
        assert reply["error"].startswith(rp.REVIEW_PREP_BUSY_PREFIX), reply["error"]
        assert "stalled" in reply["error"] and "absent" not in reply["error"]
        assert reply["slices"] == []
    finally:
        release.set()
    again = rp._handler(_params(repo, "prep-stall", run_id=RUN), repo)
    assert again["error"] is None, again["error"]
    assert again["slices"]


def test_the_stalled_op_and_its_retry_spawn_no_more_than_a_prompt_one(repo, monkeypatch):
    _write(repo, ["a/one.py"], 3)
    _stage(repo, ["a/one.py"])
    fast_params, slow_params = _params(repo, "prep-fast", run_id=RUN), _params(repo, "prep-slow", run_id=RUN)
    prompt = _count_spawns(monkeypatch)
    assert rp._handler(fast_params, repo)["error"] is None
    baseline = len(prompt)
    assert baseline > 0

    slow = _count_spawns(monkeypatch)
    release = _delay_manifest_read(monkeypatch, STALL_S)
    try:
        stalled = rp._handler(slow_params, repo)
        assert stalled["error"].startswith(rp.REVIEW_PREP_BUSY_PREFIX), stalled["error"]
        assert slow == []
    finally:
        release.set()
    assert rp._handler(slow_params, repo)["error"] is None
    assert len(slow) <= baseline


def test_an_absent_manifest_still_refuses_as_absent_with_no_spawn(repo, monkeypatch):
    params = _params(repo, "prep-absent", run_id="ask-gone")
    spawned = _count_spawns(monkeypatch)
    reply = rp._handler(params, repo)
    assert reply["slices"] == []
    assert "no readable manifest for run 'ask-gone': absent" in reply["error"]
    assert spawned == []


def test_a_malformed_manifest_still_refuses_as_malformed_with_no_spawn(repo, monkeypatch):
    base = record_dir(repo, "ask-bad")
    base.mkdir(parents=True, exist_ok=True)
    params = _params(repo, "prep-bad", run_id="ask-bad")
    spawned = _count_spawns(monkeypatch)
    for body in ("{not json", "[1]"):
        (base / "manifest.json").write_text(body, encoding="utf-8")
        reply = rp._handler(params, repo)
        assert reply["slices"] == []
        assert "no readable manifest for run 'ask-bad': malformed" in reply["error"]
    assert spawned == []

"""Tests for coordinator_core.group_em.slot_wait; injected clock/sleep, tmp_path only."""

from __future__ import annotations

import json

from coordinator_core.group_em import admission_post, nomination, slot_wait


class _Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = 0

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps += 1
        self.t += s


def _holder(monkeypatch, record):
    monkeypatch.setattr(nomination, "box_holder", lambda directory=None, now=None: record)


def _lines(tmp_path):
    p = admission_post.admission_path(tmp_path)
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


def _wait(tmp_path, clk, peak=4096, timeout=10.0):
    return slot_wait.request_and_wait(
        "s1", "run1", peak, timeout_s=timeout, poll_s=1.0, directory=tmp_path,
        sleep=clk.sleep, clock=clk.clock,
    )


def test_no_grant_times_out_closed(tmp_path, monkeypatch):
    _holder(monkeypatch, {"session_id": "gem", "live": True})
    monkeypatch.setattr(slot_wait.slot_grant, "check_slot_grant", lambda *a, **k: (False, None))
    clk = _Clock()
    assert _wait(tmp_path, clk) == {"admitted": False, "reason": "timeout"}
    assert clk.sleeps >= 1
    (line,) = _lines(tmp_path)
    assert line["phase"] == "REQUEST" and line["declared_peak_mb"] == 4096.0


def test_grant_mid_wait_admits(tmp_path, monkeypatch):
    _holder(monkeypatch, {"session_id": "gem", "live": True})
    clk = _Clock()
    monkeypatch.setattr(
        slot_wait.slot_grant, "check_slot_grant", lambda *a, **k: (clk.t >= 3.0, None)
    )
    assert _wait(tmp_path, clk) == {"admitted": True, "reason": "granted"}
    assert clk.t == 3.0


def test_no_holder_fails_closed(tmp_path, monkeypatch):
    _holder(monkeypatch, None)
    r = _wait(tmp_path, _Clock())
    assert r == {"admitted": False, "reason": "no_box_holder"}
    assert not (tmp_path / "box").exists()


def test_threshold_admits_without_a_line(tmp_path, monkeypatch):
    _holder(monkeypatch, {"session_id": "gem", "live": True})
    r = _wait(tmp_path, _Clock(), peak=slot_wait.SLOT_THRESHOLD_MB)
    assert r["admitted"] is True
    assert not (tmp_path / "box").exists()


def test_unsafe_run_label_refused_before_posting(tmp_path, monkeypatch):
    _holder(monkeypatch, {"session_id": "gem", "live": True})
    r = slot_wait.request_and_wait(
        "s1", "bad run.1", 4096, timeout_s=10.0, directory=tmp_path, sleep=_Clock().sleep, clock=_Clock().clock
    )
    assert r == {"admitted": False, "reason": "bad_run"}
    assert not (tmp_path / "box").exists()


def test_invalid_peak_refused(tmp_path):
    assert _wait(tmp_path, _Clock(), peak=None) == {"admitted": False, "reason": "bad_peak"}


def test_never_raises(tmp_path, monkeypatch):
    _holder(monkeypatch, {"session_id": "gem", "live": True})

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(slot_wait.slot_grant, "check_slot_grant", boom)
    r = _wait(tmp_path, _Clock())
    assert r["admitted"] is False and r["reason"].startswith("error:")

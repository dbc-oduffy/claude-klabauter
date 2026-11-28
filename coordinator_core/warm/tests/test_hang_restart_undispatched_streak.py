"""A run of served ops all answered undispatched with no busy worker decides one restart
(source undispatched-wedge) even while note_progress fires per op; a short or broken streak
records nothing, and a restart inside the backoff is suppressed."""

from __future__ import annotations

import pytest

from coordinator_core.warm import hang_restart

pytestmark = pytest.mark.complete

_N = hang_restart.UNDISPATCHED_WEDGE_THRESHOLD
_UND = hang_restart.UNDISPATCHED_OUTCOME


def _log(svc):
    path = svc / hang_restart.HANG_RESTART_LOG_FILENAME
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _feed(svc, exits, n, *, busy=0, outcome=_UND, progress=False):
    verdicts = []
    for _ in range(n):
        if progress:
            hang_restart.note_progress()
        verdicts.append(
            hang_restart.note_undispatched(
                svc, server_pid=4242, busy_workers=busy, outcome=outcome, exit_fn=exits.append
            )
        )
    return verdicts


def test_below_threshold_records_nothing_and_touches_no_filesystem(tmp_path):
    svc = tmp_path / "svc-below"
    exits: list = []
    verdicts = _feed(svc, exits, _N - 1)
    assert verdicts == [None] * (_N - 1)
    assert exits == []
    assert not svc.exists()


def test_full_streak_decides_exactly_one_restart(tmp_path):
    svc = tmp_path / "svc-full"
    svc.mkdir()
    exits: list = []
    verdicts = _feed(svc, exits, _N)
    assert verdicts[-1] == "restart"
    assert verdicts[:-1] == [None] * (_N - 1)
    assert exits == [4242]
    lines = _log(svc)
    assert len(lines) == 1
    assert "action=restart" in lines[0] and "source=undispatched-wedge" in lines[0]


def test_busy_worker_breaks_the_streak(tmp_path):
    svc = tmp_path / "svc-busy"
    svc.mkdir()
    exits: list = []
    _feed(svc, exits, _N - 1)
    _feed(svc, exits, 1, busy=1)
    assert _feed(svc, exits, _N - 1) == [None] * (_N - 1)
    assert exits == [] and _log(svc) == []


def test_other_outcome_breaks_the_streak(tmp_path):
    svc = tmp_path / "svc-other"
    svc.mkdir()
    exits: list = []
    _feed(svc, exits, _N - 1)
    _feed(svc, exits, 1, outcome="ok")
    _feed(svc, exits, 1, outcome=None)
    assert _feed(svc, exits, _N - 1) == [None] * (_N - 1)
    assert exits == [] and _log(svc) == []


def test_note_progress_per_op_does_not_reset_the_streak(tmp_path):
    svc = tmp_path / "svc-progress"
    svc.mkdir()
    exits: list = []
    verdicts = _feed(svc, exits, _N, progress=True)
    assert verdicts[-1] == "restart"
    assert exits == [4242]
    assert "action=restart" in _log(svc)[0]


def test_second_streak_inside_backoff_is_suppressed_without_exit(tmp_path):
    svc = tmp_path / "svc-backoff"
    svc.mkdir()
    exits: list = []
    assert _feed(svc, exits, _N)[-1] == "restart"
    assert _feed(svc, exits, _N)[-1] == "suppressed"
    assert exits == [4242]
    lines = _log(svc)
    assert len(lines) == 2
    assert "action=suppressed reason=backoff" in lines[1]
    assert "source=undispatched-wedge" in lines[1]


def test_publish_hold_suppresses(tmp_path, monkeypatch):
    svc = tmp_path / "svc-hold"
    svc.mkdir()
    monkeypatch.setattr(hang_restart.breadcrumb, "publish_marker_held", lambda *_a, **_k: True)
    exits: list = []
    assert _feed(svc, exits, _N)[-1] == "suppressed"
    assert exits == []
    assert "reason=publish-hold" in _log(svc)[0]


def test_unreadable_svc_dir_never_raises_and_restarts(tmp_path, monkeypatch):
    svc = tmp_path / "svc-broken"
    monkeypatch.setattr(hang_restart, "_tail_lines", lambda *_a: (_ for _ in ()).throw(OSError("stalled")))
    exits: list = []
    assert _feed(svc, exits, _N)[-1] == "restart"
    assert exits == [4242]

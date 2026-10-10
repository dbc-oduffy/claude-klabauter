"""Contract tests for coordinator_core._engine_landing."""

from __future__ import annotations

import json
import os
import time

from coordinator_core import _engine_landing as el


def _root(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_miss_path_is_one_stat(tmp_path, monkeypatch):
    root = _root(tmp_path)
    calls = []
    real = os.stat
    monkeypatch.setattr(os, "stat", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    assert el.swap_in_progress(root) is False
    assert len(calls) == 1


def test_hit_then_clear(tmp_path):
    root = _root(tmp_path)
    path = el.begin_swap(root, deadline_s=30)
    assert path == root / ".git" / el.MARKER_NAME
    assert el.swap_in_progress(root) is True
    el.end_swap(root)
    assert el.swap_in_progress(root) is False
    el.end_swap(root)


def test_stale_deadline_reads_clear(tmp_path):
    root = _root(tmp_path)
    (root / ".git" / el.MARKER_NAME).write_text(
        json.dumps({"deadline_epoch": time.time() - 5}), encoding="utf-8"
    )
    assert el.swap_in_progress(root) is False
    assert el.wait_until_clear(root, sleep=lambda s: None) is True


def test_non_directory_git_yields_none_and_never_blocks(tmp_path):
    (tmp_path / ".git").write_text("gitdir: elsewhere", encoding="utf-8")
    assert el.marker_path(tmp_path) is None
    assert el.begin_swap(tmp_path, deadline_s=30) is None
    el.end_swap(tmp_path)
    assert el.swap_in_progress(tmp_path) is False
    assert el.wait_until_clear(tmp_path, sleep=lambda s: None) is True


def test_wait_returns_when_marker_removed(tmp_path):
    root = _root(tmp_path)
    el.begin_swap(root, deadline_s=30)
    ticks = []

    def sleeper(s):
        ticks.append(s)
        if len(ticks) == 3:
            el.end_swap(root)

    assert el.wait_until_clear(root, sleep=sleeper) is True
    assert len(ticks) == 3


def test_budget_overrun_returns_false_without_raising(tmp_path, capsys, monkeypatch):
    root = _root(tmp_path)
    monkeypatch.setattr(el, "_reported_overrun", False)
    el.begin_swap(root, deadline_s=30)
    assert el.wait_until_clear(root, budget_s=0.1, sleep=lambda s: None) is False
    assert el.wait_until_clear(root, budget_s=0.1, sleep=lambda s: None) is False
    assert capsys.readouterr().err.count("still live") == 1


def test_begin_swap_replaces_an_existing_marker(tmp_path):
    root = _root(tmp_path)
    el.begin_swap(root, deadline_s=1)
    el.begin_swap(root, deadline_s=30)
    assert json.loads((root / ".git" / el.MARKER_NAME).read_text(encoding="utf-8"))[
        "deadline_epoch"
    ] > time.time() + 10
    assert not list((root / ".git").glob("*.tmp"))


def test_corrupt_marker_reads_clear(tmp_path):
    root = _root(tmp_path)
    marker = root / ".git" / el.MARKER_NAME
    for body in ("not json", "{}", '{"deadline_epoch": null}'):
        marker.write_text(body, encoding="utf-8")
        assert el.swap_in_progress(root) is False


def test_budgets_stay_under_load_norm():
    assert el.SWAP_GRACE_S < el.WAIT_BUDGET_S <= 2.0

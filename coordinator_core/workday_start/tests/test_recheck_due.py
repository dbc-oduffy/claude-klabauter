"""Branches of the recheck-due marker reader, under tmp_path."""
from datetime import date, timedelta

from coordinator_core.workday_start.recheck_due import recheck_due

TODAY = date(2026, 10, 15)


def _mark(root, sub, stamp, stem="x-recheck-due-"):
    d = root / sub
    d.mkdir(exist_ok=True)
    (d / f"{stem}{stamp}.md").write_text("body", encoding="utf-8")


def _off(n):
    return (TODAY + timedelta(days=n)).isoformat()


def test_no_directories(tmp_path):
    assert recheck_due(tmp_path, TODAY) == (0, "")


def test_today_and_window_edge_included_next_day_omitted(tmp_path):
    for n in (0, 7, 8):
        _mark(tmp_path, "state", _off(n))
    code, text = recheck_due(tmp_path, TODAY)
    assert code == 0
    assert text.splitlines() == [
        f"state/x-recheck-due-{_off(0)}.md: due in 0d",
        f"state/x-recheck-due-{_off(7)}.md: due in 7d",
    ]


def test_overdue_sorted_first_across_both_dirs(tmp_path):
    _mark(tmp_path, "state", _off(2))
    _mark(tmp_path, "tasks", _off(-3))
    lines = recheck_due(tmp_path, TODAY)[1].splitlines()
    assert lines[0].endswith("overdue 3d") and lines[0].startswith("tasks/")
    assert lines[1].endswith("due in 2d")


def test_undated_and_impossible_date_reported_after_dated(tmp_path):
    _mark(tmp_path, "state", "nodate")
    _mark(tmp_path, "state", "2026-13-45")
    _mark(tmp_path, "state", _off(1))
    lines = recheck_due(tmp_path, TODAY)[1].splitlines()
    assert lines[0].endswith("due in 1d")
    assert lines[1:] == [
        "state/x-recheck-due-2026-13-45.md: undated name (unparseable)",
        "state/x-recheck-due-nodate.md: undated name (unparseable)",
    ]


def test_non_markers_ignored(tmp_path):
    _mark(tmp_path, "state", _off(0), stem="other-")
    (tmp_path / "state" / f"a-recheck-due-{_off(0)}.txt").write_text("x")
    assert recheck_due(tmp_path, TODAY) == (0, "")

"""Tests coordinator/lib/check-provisional-expiry.py's expiry-window computation."""

from __future__ import annotations

import datetime
import importlib.util
import os
import sys

import pytest

_SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "lib",
    "check-provisional-expiry.py",
)


def _load_module():
    spec = importlib.util.spec_from_file_location("check_provisional_expiry", _SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_provisional_expiry"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


def _write_plan(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_expired_provisional_on_non_terminal_status_is_flagged(tmp_path, mod):
    path = _write_plan(
        tmp_path,
        "expired.md",
        "---\ntitle: X\nstatus: draft\nprovisional_until: 2026-07-01\n---\nbody\n",
    )
    today = datetime.date(2026, 7, 14)
    expired = mod.find_expired([path], today=today)
    assert len(expired) == 1
    got_path, expiry, days_overdue, status = expired[0]
    assert got_path == path
    assert expiry == datetime.date(2026, 7, 1)
    assert days_overdue == 13
    assert status == "draft"


def test_revisit_by_synonym_is_read(tmp_path, mod):
    path = _write_plan(
        tmp_path,
        "revisit.md",
        "---\ntitle: X\nstatus: executing\nrevisit_by: 2026-07-01\n---\nbody\n",
    )
    today = datetime.date(2026, 7, 2)
    expired = mod.find_expired([path], today=today)
    assert len(expired) == 1
    assert expired[0][3] == "executing"


def test_future_provisional_until_not_flagged(tmp_path, mod):
    path = _write_plan(
        tmp_path,
        "future.md",
        "---\ntitle: X\nstatus: draft\nprovisional_until: 2026-12-01\n---\nbody\n",
    )
    today = datetime.date(2026, 7, 14)
    assert mod.find_expired([path], today=today) == []


def test_terminal_status_suppresses_expiry(tmp_path, mod):
    path = _write_plan(
        tmp_path,
        "implemented.md",
        "---\ntitle: X\nstatus: implemented\nprovisional_until: 2026-01-01\n---\nbody\n",
    )
    today = datetime.date(2026, 7, 14)
    assert mod.find_expired([path], today=today) == []


def test_no_provisional_field_is_silent(tmp_path, mod):
    path = _write_plan(
        tmp_path,
        "plain.md",
        "---\ntitle: X\nstatus: draft\n---\nbody\n",
    )
    today = datetime.date(2026, 7, 14)
    assert mod.find_expired([path], today=today) == []


def test_expiry_exactly_today_is_flagged(tmp_path, mod):
    path = _write_plan(
        tmp_path,
        "today.md",
        "---\ntitle: X\nstatus: draft\nprovisional_until: 2026-07-14\n---\nbody\n",
    )
    today = datetime.date(2026, 7, 14)
    expired = mod.find_expired([path], today=today)
    assert len(expired) == 1
    assert expired[0][2] == 0


def test_both_provisional_until_and_revisit_by_raises_value_error(tmp_path, mod):
    # At most one of the two synonym keys may be set; the detector must fail
    # loud rather than silently prefer provisional_until over revisit_by.
    path = _write_plan(
        tmp_path,
        "both.md",
        "---\ntitle: X\nstatus: draft\nprovisional_until: 2026-07-01\nrevisit_by: 2026-07-02\n---\nbody\n",
    )
    with pytest.raises(ValueError):
        mod.find_expired([path], today=datetime.date(2026, 7, 14))


def test_unparseable_date_raises_value_error(tmp_path, mod):
    path = _write_plan(
        tmp_path,
        "bad.md",
        "---\ntitle: X\nstatus: draft\nprovisional_until: not-a-date\n---\nbody\n",
    )
    with pytest.raises(ValueError):
        mod.find_expired([path], today=datetime.date(2026, 7, 14))


def test_main_exit_codes(tmp_path, mod, capsys, monkeypatch):
    expired_path = _write_plan(
        tmp_path,
        "expired.md",
        "---\ntitle: X\nstatus: draft\nprovisional_until: 2020-01-01\n---\nbody\n",
    )
    clean_path = _write_plan(
        tmp_path,
        "clean.md",
        "---\ntitle: X\nstatus: draft\n---\nbody\n",
    )
    missing_path = str(tmp_path / "missing.md")

    assert mod.main([expired_path]) == 1
    out = capsys.readouterr().out
    assert "EXPIRED:" in out
    assert expired_path in out

    assert mod.main([clean_path]) == 0

    assert mod.main([missing_path]) == 2


def test_no_argv_fails_loud_instead_of_silent_cwd_glob(mod):
    # The argv-less default silently globbed docs/plans/*.md relative to cwd,
    # indistinguishable on stderr from a genuine empty-after-filtering result.
    # Callers always pass an explicit path; require one.
    assert mod.main([]) == 2


def test_directory_mode_scans_one_level(tmp_path, mod):
    _write_plan(
        tmp_path,
        "a.md",
        "---\ntitle: A\nstatus: draft\nprovisional_until: 2020-01-01\n---\nbody\n",
    )
    _write_plan(
        tmp_path,
        "b.md",
        "---\ntitle: B\nstatus: draft\n---\nbody\n",
    )
    assert mod.main([str(tmp_path)]) == 1


def test_nested_status_does_not_shadow_top_level_status(tmp_path, mod):
    path = _write_plan(
        tmp_path,
        "nested.md",
        "---\ntitle: X\nstatus: implemented\nprovisional_until: 2026-07-01\n"
        "grouping_approvals:\n  do:\n    status: pending\n---\nbody\n",
    )
    assert mod.find_expired([path], today=datetime.date(2026, 7, 14)) == []


def test_missing_target_exits_2(tmp_path, mod):
    assert mod.main([str(tmp_path / "missing.md")]) == 2


def test_unreadable_file_raises_value_error(tmp_path, mod):
    directory = tmp_path / "adir.md"
    directory.mkdir()
    with pytest.raises(ValueError):
        mod._read_frontmatter_scalars(str(directory))

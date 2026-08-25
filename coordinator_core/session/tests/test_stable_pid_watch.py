"""
coordinator_core.session.tests.test_stable_pid_watch — coverage for the
K-006 F0-hazard watch (coordinator_core.session.stable_pid_watch).

Never touches the real ``.git/coordinator-sessions/`` corpus (~50-70 live
sessions writing to it) — every case builds its own ``tmp_path``-rooted
session hub and passes it explicitly via ``sessions_dir=``.
"""

from __future__ import annotations

import json

from coordinator_core.session.stable_pid_watch import (
    STATUS_CLEAN,
    STATUS_EMPTY,
    STATUS_MISS,
    scan_stable_pid_misses,
)


def _write_session(root, sid, meta):
    sdir = root / sid
    sdir.mkdir(parents=True)
    (sdir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return sdir


def test_zero_misses_reports_clean(tmp_path):
    root = tmp_path / "coordinator-sessions"
    _write_session(root, "s1", {"stable_pid": "1234", "stable_pid_start_epoch": "1700000000"})
    _write_session(root, "s2", {"stable_pid": "5678", "stable_pid_lstart": "Tue Jul 14 15:26:28 2026"})

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_CLEAN
    assert result["checked"] == 2
    assert result["misses"] == []


def test_one_session_missing_stable_pid_alerts(tmp_path):
    root = tmp_path / "coordinator-sessions"
    _write_session(root, "s1", {"stable_pid": "1234", "stable_pid_start_epoch": "1700000000"})
    _write_session(root, "s2", {"stable_pid": "", "stable_pid_start_epoch": ""})

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_MISS
    assert result["checked"] == 2
    assert result["misses"] == [{"session": "s2", "reason": "empty"}]


def test_stable_pid_present_without_birth_witness_counts_as_miss(tmp_path):
    # Mirrors session_live's own Layer-1 fall-through: stable_pid present but
    # BOTH stable_pid_lstart and stable_pid_start_epoch absent falls through
    # to Layer 2 recency, the same F0 exposure as an empty stable_pid.
    root = tmp_path / "coordinator-sessions"
    _write_session(root, "s1", {"stable_pid": "9999"})

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_MISS
    assert result["checked"] == 1
    assert result["misses"] == [{"session": "s1", "reason": "no_witness"}]


def test_stable_pid_present_with_only_lstart_is_not_a_miss(tmp_path):
    root = tmp_path / "coordinator-sessions"
    _write_session(root, "s1", {"stable_pid": "9999", "stable_pid_lstart": "Tue Jul 14 15:26:28 2026"})

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_CLEAN
    assert result["misses"] == []


def test_stable_pid_present_with_only_start_epoch_is_not_a_miss(tmp_path):
    root = tmp_path / "coordinator-sessions"
    _write_session(root, "s1", {"stable_pid": "9999", "stable_pid_start_epoch": "1700000000"})

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_CLEAN
    assert result["misses"] == []


def test_empty_corpus_does_not_crash(tmp_path):
    root = tmp_path / "coordinator-sessions"
    root.mkdir()

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_EMPTY
    assert result["checked"] == 0
    assert result["misses"] == []


def test_missing_sessions_dir_does_not_crash(tmp_path):
    result = scan_stable_pid_misses(sessions_dir=tmp_path / "does-not-exist")

    assert result["status"] == STATUS_EMPTY
    assert result["checked"] == 0
    assert result["misses"] == []


def test_unreadable_meta_json_counts_as_miss(tmp_path):
    root = tmp_path / "coordinator-sessions"
    sdir = root / "s1"
    sdir.mkdir(parents=True)
    (sdir / "meta.json").write_text("{not valid json", encoding="utf-8")

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_MISS
    assert result["checked"] == 1
    assert result["misses"] == [{"session": "s1", "reason": "empty"}]


def test_non_session_subdir_without_meta_json_is_not_counted(tmp_path):
    root = tmp_path / "coordinator-sessions"
    stray = root / "logs"
    stray.mkdir(parents=True)
    (stray / "notes.txt").write_text("not a session", encoding="utf-8")

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_EMPTY
    assert result["checked"] == 0


def test_touched_txt_without_meta_json_counts_as_miss(tmp_path):
    # AC8/C4 (2026-08-22): a session dir that ran but left NO meta.json at
    # all — the population a sibling chunk (C1, edit-hook session-bootstrap
    # removal) grows — must still be COUNTED, not fall out of the
    # denominator and read falsely CLEAN. touched.txt is this repo's own
    # signal that a session genuinely ran here.
    root = tmp_path / "coordinator-sessions"
    sdir = root / "s1"
    sdir.mkdir(parents=True)
    (sdir / "touched.txt").write_text("some/file.py\n", encoding="utf-8")

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_MISS
    assert result["checked"] == 1
    assert result["misses"] == [{"session": "s1", "reason": "no_meta_json"}]


def test_dir_with_neither_meta_json_nor_touched_txt_is_not_counted(tmp_path):
    # The pre-existing behaviour the AC8 branch must preserve: genuine hub
    # debris (no meta.json, no touched.txt) stays out of the denominator.
    root = tmp_path / "coordinator-sessions"
    sdir = root / "s1"
    sdir.mkdir(parents=True)
    (sdir / "some_other_file.txt").write_text("debris", encoding="utf-8")

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_EMPTY
    assert result["checked"] == 0

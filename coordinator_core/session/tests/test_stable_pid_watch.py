
from __future__ import annotations

import json
import os
import time

from coordinator_core.session.stable_pid_watch import (
    _NO_META_RECENCY_SECONDS,
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
    # sibling test below already covers. Flipped to STATUS_EMPTY/checked 0.
    root = tmp_path / "coordinator-sessions"
    sdir = root / "s1"
    sdir.mkdir(parents=True)
    (sdir / "touched.txt").write_text("some/file.py\n", encoding="utf-8")

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_EMPTY
    assert result["checked"] == 0


def test_dir_with_neither_meta_json_nor_any_file_is_not_counted(tmp_path):
    root = tmp_path / "coordinator-sessions"
    sdir = root / "s1"
    sdir.mkdir(parents=True)

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_EMPTY
    assert result["checked"] == 0


def test_dir_with_only_an_unrelated_file_without_meta_json_is_not_counted(tmp_path):
    # INVERTED 2026-08-26, deliberately, from
    root = tmp_path / "coordinator-sessions"
    sdir = root / "s1"
    sdir.mkdir(parents=True)
    (sdir / "some_other_file.txt").write_text("debris", encoding="utf-8")

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_EMPTY
    assert result["checked"] == 0
    assert result["misses"] == []


def test_a_rotated_touch_record_generation_still_counts(tmp_path):
    root = tmp_path / "coordinator-sessions"
    sdir = root / "s1"
    sdir.mkdir(parents=True)
    (sdir / "touch-record.jsonl.rotated-1756200000000-4242.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_MISS
    assert result["checked"] == 1
    assert result["misses"] == [{"session": "s1", "reason": "no_meta_json"}]


def test_dir_with_only_em_session_id_txt_without_meta_json_is_not_counted(tmp_path):
    root = tmp_path / "coordinator-sessions"
    sdir = root / "s1"
    sdir.mkdir(parents=True)
    (sdir / "em-session-id.txt").write_text("some-other-sid\n", encoding="utf-8")

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_EMPTY
    assert result["checked"] == 0


def test_stale_touched_txt_without_meta_json_is_not_counted(tmp_path):
    root = tmp_path / "coordinator-sessions"
    sdir = root / "fossil"
    sdir.mkdir(parents=True)
    touched = sdir / "touched.txt"
    touched.write_text("some/file.py\n", encoding="utf-8")
    stale = time.time() - (_NO_META_RECENCY_SECONDS + 3600)
    os.utime(touched, (stale, stale))

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_EMPTY
    assert result["checked"] == 0
    assert result["misses"] == []


def test_recency_scope_does_not_mask_a_current_no_meta_session(tmp_path):
    # to STATUS_EMPTY/checked 0; the recency-scope behavior this test named
    root = tmp_path / "coordinator-sessions"
    for name, age in (("fossil", _NO_META_RECENCY_SECONDS + 3600), ("current", 60)):
        sdir = root / name
        sdir.mkdir(parents=True)
        touched = sdir / "touched.txt"
        touched.write_text("some/file.py\n", encoding="utf-8")
        when = time.time() - age
        os.utime(touched, (when, when))

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_EMPTY
    assert result["checked"] == 0


def test_recency_scope_never_narrows_the_meta_json_bearing_population(tmp_path):
    root = tmp_path / "coordinator-sessions"
    sdir = _write_session(root, "s1", {"pid": "1234"})
    touched = sdir / "touched.txt"
    touched.write_text("some/file.py\n", encoding="utf-8")
    stale = time.time() - (_NO_META_RECENCY_SECONDS * 30)
    os.utime(touched, (stale, stale))

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_MISS
    assert result["checked"] == 1
    assert result["misses"] == [{"session": "s1", "reason": "empty"}]


def test_old_dir_with_a_freshly_appended_guard_log_is_not_counted(tmp_path, monkeypatch):
    import coordinator_core.session.stable_pid_watch as watch

    root = tmp_path / "coordinator-sessions"
    sdir = root / "sess-abc"
    sdir.mkdir(parents=True)
    log = sdir / "repo-identity-gate.log"
    log.write_text("verdict=UNRESOLVED\n", encoding="utf-8")

    future = time.time() + _NO_META_RECENCY_SECONDS * 9
    os.utime(log, (future - 60, future - 60))
    monkeypatch.setattr(watch.time, "time", lambda: future)

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["checked"] == 0
    assert result["misses"] == []


def test_a_session_that_touched_files_without_init_is_still_counted(tmp_path):
    root = tmp_path / "coordinator-sessions"
    sdir = root / "471733e0-5785-4c97-b9c6-e4db17040fe9"
    sdir.mkdir(parents=True)
    (sdir / "write_bump_launch_cwd").write_text("/repo\n", encoding="utf-8")
    (sdir / "touched.txt").write_text(
        "T 2026-08-26T12:11:06Z state/bug-backlog/x.yaml\n", encoding="utf-8"
    )

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_EMPTY
    assert result["checked"] == 0


def test_a_freshly_started_session_before_its_first_touch_is_not_counted(tmp_path):
    root = tmp_path / "coordinator-sessions"
    sdir = root / "2518a105-4c31-4644-8bb2-7977d4af38e3"
    sdir.mkdir(parents=True)
    for name in (
        "write_bump_launch_cwd",
        "baton.json",
        "baton.json.adopted-announced",
        "session-shape.json",
        "push-failures-cursor.txt",
        "inprocess-search-footer-seen",
    ):
        (sdir / name).write_text("x\n", encoding="utf-8")

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_EMPTY
    assert result["checked"] == 0
    assert result["misses"] == []


def test_a_long_running_session_is_not_excluded_by_its_own_age(tmp_path, monkeypatch):
    import coordinator_core.session.stable_pid_watch as watch

    root = tmp_path / "coordinator-sessions"
    sdir = root / "long-runner"
    sdir.mkdir(parents=True)
    touched = sdir / "touched.txt"
    touched.write_text("T 2026-08-26T12:11:06Z a.py\n", encoding="utf-8")

    future = time.time() + _NO_META_RECENCY_SECONDS * 9
    os.utime(touched, (future - 60, future - 60))
    monkeypatch.setattr(watch.time, "time", lambda: future)

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_EMPTY
    assert result["checked"] == 0

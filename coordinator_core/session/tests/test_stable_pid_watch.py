"""
coordinator_core.session.tests.test_stable_pid_watch — coverage for the
K-006 F0-hazard watch (coordinator_core.session.stable_pid_watch).

Never touches the real ``.git/coordinator-sessions/`` corpus (~50-70 live
sessions writing to it) — every case builds its own ``tmp_path``-rooted
session hub and passes it explicitly via ``sessions_dir=``.
"""

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


def test_dir_with_neither_meta_json_nor_any_file_is_not_counted(tmp_path):
    # The pre-existing behaviour the AC8 branch must preserve: genuine hub
    # debris (no meta.json, no files at all) stays out of the denominator.
    # Widened 2026-08-25 (C5, docs/plans/2026-08-25-the-legacy-touch-record-
    # is-retired-by-repointing-its-writers.md § AC6): the miss check now
    # keys on `liveness.newest_record_mtime`, which treats ANY regular file
    # (except `em-session-id.txt`) as a candidate record rather than the
    # single `touched.txt` literal — so a dir carrying an unrelated file now
    # DOES count as a session record (see the next test); only a dir with NO
    # eligible file at all still reads uncounted.
    root = tmp_path / "coordinator-sessions"
    sdir = root / "s1"
    sdir.mkdir(parents=True)

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_EMPTY
    assert result["checked"] == 0


def test_dir_with_unrelated_record_file_without_meta_json_counts_as_miss(tmp_path):
    # Widened 2026-08-25 (C5, AC6): keying on the newest record file rather
    # than the `touched.txt` literal means ANY regular record file in a
    # meta.json-less dir now counts — a future record dialect rename can
    # only DEFER this signal, never DISABLE it (AC6's own "widen, do not
    # swap"). This is the accepted trade-off named in that chunk's brief:
    # this cadence watch is a lower-stakes false-positive surface than the
    # claim-liveness enumeration path (`live_session_verdicts`), which keeps
    # its own separate `_NON_SESSION_DIR_NAMES` denylist untouched by this
    # widening.
    root = tmp_path / "coordinator-sessions"
    sdir = root / "s1"
    sdir.mkdir(parents=True)
    (sdir / "some_other_file.txt").write_text("debris", encoding="utf-8")

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_MISS
    assert result["checked"] == 1
    assert result["misses"] == [{"session": "s1", "reason": "no_meta_json"}]


def test_dir_with_only_em_session_id_txt_without_meta_json_is_not_counted(tmp_path):
    # `em-session-id.txt` is excluded from `newest_record_mtime`'s scan
    # (AC6) — the ownership backpointer, written once and never refreshed,
    # must not itself manufacture a "session record" signal.
    root = tmp_path / "coordinator-sessions"
    sdir = root / "s1"
    sdir.mkdir(parents=True)
    (sdir / "em-session-id.txt").write_text("some-other-sid\n", encoding="utf-8")

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_EMPTY
    assert result["checked"] == 0


def test_stale_touched_txt_without_meta_json_is_not_counted(tmp_path):
    # 2026-08-26 recency scope. The measured failure this pins: on host
    # `machine-b` the unscoped branch counted 223 fossil dirs — 216 of them
    # given a touched.txt retroactively by the C6 touch-corpus migration
    # (380b3e329, 2026-07-31), none of them ever `core.init`-ed, none of
    # them removable because reap's fail-closed-to-keep arm cannot judge a
    # dir with no readable meta.json. The probe read "223 of 226 sessions
    # missing stable_pid capture — K-006 is live again" while every one of
    # the 8 real sessions on the box was armed.
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
    # The other half of the same pin: the scope must not become a way for a
    # live bootstrap-without-init to read CLEAN. A fossil and a session
    # whose touched.txt is inside the window sit side by side; only the
    # current one is counted, and the denominator is 1, not 2.
    root = tmp_path / "coordinator-sessions"
    for name, age in (("fossil", _NO_META_RECENCY_SECONDS + 3600), ("current", 60)):
        sdir = root / name
        sdir.mkdir(parents=True)
        touched = sdir / "touched.txt"
        touched.write_text("some/file.py\n", encoding="utf-8")
        when = time.time() - age
        os.utime(touched, (when, when))

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_MISS
    assert result["checked"] == 1
    assert result["misses"] == [{"session": "current", "reason": "no_meta_json"}]


def test_recency_scope_never_narrows_the_meta_json_bearing_population(tmp_path):
    # Negative-spec: the scope applies ONLY to the no_meta_json branch. A
    # dir carrying an unstamped meta.json is a miss no matter how old its
    # touched.txt is — that population is reapable on its own last_activity
    # and never accumulates, so it needs no recency guard and must not get
    # one.
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

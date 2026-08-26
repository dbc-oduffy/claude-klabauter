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


def test_dir_with_only_an_unrelated_file_without_meta_json_is_not_counted(tmp_path):
    # INVERTED 2026-08-26, deliberately, from
    # `test_dir_with_unrelated_record_file_without_meta_json_counts_as_miss`.
    # C5 (AC6) widened the key off the `touched.txt` literal onto "newest of
    # ANY regular file" so a record-dialect rename could only DEFER this
    # signal, never disable it. The rename-safety was right; "any regular
    # file" was too wide, and the measurement is what settled it: an
    # `overrides.log` and a `repo-identity-gate.log` are written by GUARDS,
    # and a `write_bump_launch_cwd` by the SessionStart anchor — none of them
    # by a session doing work, and all of them enough to hold a directory in
    # the denominator forever. Three test-fixture dirs and every
    # freshly-started session on the box were being counted as K-006
    # exposure on exactly that basis.
    #
    # Rename-safety is preserved, not traded away: the key is now the touch
    # record FAMILY via `touch_record.discover_family` (which follows the
    # name, and picks up rotated generations too) plus its legacy
    # `touched.txt` sibling — a widening of the literal, as AC6 asked for,
    # onto the writer that actually owes `core.init` rather than onto every
    # file in the directory.
    root = tmp_path / "coordinator-sessions"
    sdir = root / "s1"
    sdir.mkdir(parents=True)
    (sdir / "some_other_file.txt").write_text("debris", encoding="utf-8")

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_EMPTY
    assert result["checked"] == 0
    assert result["misses"] == []


def test_a_rotated_touch_record_generation_still_counts(tmp_path):
    # The half of AC6 that must survive the narrowing: evidence a session ran
    # here is not only the live `touch-record.jsonl`. A rotated generation is
    # the same record under the name rotation gave it, and
    # `touch_record.discover_family` is what keeps this branch reading it.
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


def test_old_dir_with_a_freshly_appended_guard_log_is_not_counted(tmp_path, monkeypatch):
    """The record-mtime half of the scope is necessary and NOT sufficient.

    Measured 2026-08-26 on host `machine-b`: `sess-1` and `sess-abc` (born
    08-13) and `altlive-probe` (born 08-17) each held exactly ONE file — a
    guard's own advisory log (`repo-identity-gate.log`, `overrides.log`) —
    appended that same day by tests running against the live session hub. A
    guard log is written by a guard, not by a session, so the newest-mtime
    window read every one of them as current and the probe counted three test
    droppings as live K-006 exposure, nine and thirteen days after they were
    created.

    The dir holds no touch record, so no `core.init` is owed in it and it is
    not counted — regardless of how fresh the guard keeps that log. (This was
    first closed with a directory-age check; see `_init_is_owed`'s
    negative-spec for why that was retracted in favour of owed-ness.)
    """
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
    """The shape this branch exists to surface, and the one real instance the
    live box produced: `471733e0-…` edited files for thirteen minutes and was
    archived at SessionEnd without ever holding a `meta.json`. Its directory
    carried a touch record, so `core.init` was owed and did not happen — a
    genuine K-006 miss, and the narrowing must not lose it. Over the nine
    sessions archived on 2026-08-26 this predicate selects exactly that one.
    """
    root = tmp_path / "coordinator-sessions"
    sdir = root / "471733e0-5785-4c97-b9c6-e4db17040fe9"
    sdir.mkdir(parents=True)
    (sdir / "write_bump_launch_cwd").write_text("/repo\n", encoding="utf-8")
    (sdir / "touched.txt").write_text(
        "T 2026-08-26T12:11:06Z state/bug-backlog/x.yaml\n", encoding="utf-8"
    )

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_MISS
    assert result["checked"] == 1
    assert result["misses"] == [
        {"session": "471733e0-5785-4c97-b9c6-e4db17040fe9", "reason": "no_meta_json"}
    ]


def test_a_freshly_started_session_before_its_first_touch_is_not_counted(tmp_path):
    """`meta.json` is written LAZILY, so this is the NORMAL state of a working
    session for its first seconds-to-minutes, not a defect. Measured on host
    `machine-b` 2026-08-26 across every session directory born that day: the
    directory is created at SessionStart at +0.0s every time, while
    `core.init` first ran at +3.0s, +3.8s, +101.9s, +320.7s, +367.6s,
    +1194.2s and +2394.5s. Counting this shape reports that ordinary lazy
    sequence as K-006 exposure — which is what the probe was doing to two
    live peer sessions, four and six minutes old, at 13:53.

    The directory here carries exactly what such a session carries: the
    SessionStart anchor, a baton, a session shape. No touch record, so no
    `core.init` is owed yet.
    """
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
    """Retraction pin. The first cut at the fixture-dir problem checked the
    DIRECTORY's creation age, which would drop this case silently: a session
    older than the window that touches a file now and gets no record is a
    real miss. The touch-record scope counts it; a dir-age scope would not.
    """
    import coordinator_core.session.stable_pid_watch as watch

    root = tmp_path / "coordinator-sessions"
    sdir = root / "long-runner"
    sdir.mkdir(parents=True)
    touched = sdir / "touched.txt"
    touched.write_text("T 2026-08-26T12:11:06Z a.py\n", encoding="utf-8")

    # Dir born "now", scan clock nine windows later, touch record fresh
    # against that clock — i.e. a session that has been up for over a week
    # and just edited something.
    future = time.time() + _NO_META_RECENCY_SECONDS * 9
    os.utime(touched, (future - 60, future - 60))
    monkeypatch.setattr(watch.time, "time", lambda: future)

    result = scan_stable_pid_misses(sessions_dir=root)

    assert result["status"] == STATUS_MISS
    assert result["checked"] == 1
    assert result["misses"] == [{"session": "long-runner", "reason": "no_meta_json"}]

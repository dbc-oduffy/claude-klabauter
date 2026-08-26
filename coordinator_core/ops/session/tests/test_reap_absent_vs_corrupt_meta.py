"""
Tests for the ABSENT-vs-CORRUPT split in
``coordinator_core.ops.session.reap._reap_stale_sessions`` (sub-reap (i)).

WHY THIS EXISTS. `_read_meta_json` returns None for a MISSING meta.json and for
a CORRUPT one on the same code path, and the caller deferred on both. Because
that defer fires BEFORE the staleness computation, a session dir carrying no
meta.json could never be reaped at any age — the 24h threshold was unreachable.
The reaper is the documented backstop for a session that died without a
SessionEnd and so was never archived, and this made it the reason those dirs are
stranded permanently instead of merely un-archived.

Traced by doe-claude-em (2026-08-26) across two trees: of the >30d unreaped
session dirs, 37 of 37 in DoE-claude and 9 of 9 in this repo carry no meta.json,
and none carries one. Absent meta.json is the whole discriminant for the stuck
population, not a partial signal. Same root as the backfill landed at
`6bf7fc291`: a session with no record is outside `live_session_ids`' scan scope,
so it is invisible to the liveness skip AND unreapable — the backfill stops the
inflow, and this stops the pool being permanent.

The asymmetry is the point, and each half is pinned below: CORRUPT stays
deferred because a meta.json that exists and will not parse is genuinely
ambiguous, and fail-closed-to-keep is the right answer to ambiguity. ABSENT with
a 24h-cold newest file is not ambiguous, and sub-reap (ii)
(`_reap_stale_agents`) already keys staleness on newest-contained-file mtime
with no meta involved.

Negative-spec:
    Does NOT loosen the threshold. The mtime fallback is held to the same
    `_SESSION_STALE_SECONDS` every other session is; a record-less dir touched
    within 24h is kept, and that case is pinned.
    Does NOT exercise the op handler, its cadence gate, or sub-reaps
    (ii)/(iii)/(iv) — unit coverage of the one branch, against a tmp_path tree.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from coordinator_core.ops.session import reap


def _session_dir(sessions_dir: Path, sid: str, *, age_seconds: float,
                 meta: str | None) -> Path:
    """Build a session dir whose newest file is `age_seconds` old.

    `meta=None` writes no meta.json at all (the absent case); a string writes
    it verbatim, so a caller can plant unparseable bytes for the corrupt case.
    """
    sdir = sessions_dir / sid
    sdir.mkdir(parents=True)
    (sdir / "touched.txt").write_text("T x", encoding="utf-8")
    if meta is not None:
        (sdir / "meta.json").write_text(meta, encoding="utf-8")
    stamp = time.time() - age_seconds
    for entry in sdir.iterdir():
        os.utime(entry, (stamp, stamp))
    os.utime(sdir, (stamp, stamp))
    return sdir


def _reap(sessions_dir: Path):
    return reap._reap_stale_sessions(sessions_dir, frozenset(), None)


class TestAbsentMetaIsReapableCorruptMetaIsNot:
    def test_record_less_dir_past_the_threshold_is_reaped(self, tmp_path):
        """The defect: before the split this dir deferred forever, because the
        meta-is-None defer fired before the staleness check could run."""
        sessions_dir = tmp_path / "coordinator-sessions"
        sdir = _session_dir(
            sessions_dir, "no-meta-old",
            age_seconds=reap._SESSION_STALE_SECONDS + 3600, meta=None,
        )

        reaped, deferred, failed = _reap(sessions_dir)

        assert reaped == ["no-meta-old"], (
            "a session dir with no meta.json, cold for over 24h, was not "
            "reaped — the absent case is still riding the corrupt case's "
            "fail-closed-to-keep defer, so the stuck pool stays permanent"
        )
        assert failed == []
        assert not sdir.exists()
        assert (sessions_dir / ".archive").is_dir()

    def test_record_less_dir_inside_the_threshold_is_kept(self, tmp_path):
        """The fallback is held to the SAME 24h threshold, not a looser one.
        This is the case that matters for a live session editing through
        Write/Edit before its backfill has fired: recent mtime, so kept."""
        sessions_dir = tmp_path / "coordinator-sessions"
        sdir = _session_dir(
            sessions_dir, "no-meta-fresh", age_seconds=60, meta=None,
        )

        reaped, deferred, failed = _reap(sessions_dir)

        assert reaped == []
        assert sdir.exists(), (
            "a record-less session touched a minute ago was reaped — the "
            "mtime fallback has been let past the staleness threshold"
        )

    def test_corrupt_meta_is_still_deferred_however_cold(self, tmp_path):
        """The half that must NOT change: present-but-unparseable is ambiguous
        at any age, and ambiguity keeps."""
        sessions_dir = tmp_path / "coordinator-sessions"
        sdir = _session_dir(
            sessions_dir, "corrupt-meta-old",
            age_seconds=reap._SESSION_STALE_SECONDS * 10,
            meta="{not json at all",
        )

        reaped, deferred, failed = _reap(sessions_dir)

        assert reaped == [], (
            "a corrupt meta.json was reaped — the absent/corrupt split has "
            "collapsed in the other direction, and fail-closed-to-keep no "
            "longer covers the genuinely ambiguous case"
        )
        assert sdir.exists()
        assert [d["id"] for d in deferred] == ["corrupt-meta-old"]
        assert "unreadable" in deferred[0]["reason"]

    def test_empty_record_less_dir_is_deferred_not_reaped(self, tmp_path):
        """An empty dir has no evidence of age in either direction. `max()`
        over no entries is not a staleness answer, so it reads as the 0.0
        unknown-timestamp sentinel and keeps."""
        sessions_dir = tmp_path / "coordinator-sessions"
        sdir = sessions_dir / "empty-dir"
        sdir.mkdir(parents=True)
        stamp = time.time() - reap._SESSION_STALE_SECONDS * 10
        os.utime(sdir, (stamp, stamp))

        reaped, deferred, failed = _reap(sessions_dir)

        assert reaped == []
        assert sdir.exists()
        assert [d["id"] for d in deferred] == ["empty-dir"]


class TestNonSessionStoresAreNeverReaped:
    """The co-located stores under the hub are not sessions, and the absent-meta
    fallback above is what made this skip load-bearing.

    Before that fallback they were protected BY ACCIDENT: none carries a
    meta.json, so each hit the unconditional fail-closed defer and was kept for
    the wrong reason. Routing absent-meta to a newest-file-mtime check removed
    that accidental protection, and a quiet repo -- a weekend, an untouched
    clone -- would have seen `decisions/` (thousands of records) or
    `reconcile-history/` (the file DR-300 is about) go 24h cold and be archived
    as stale sessions. The EMPTY claim stores would have read 0.0 and survived,
    which is what would have made the loss look partial and random rather than
    systematic.

    Pinned on a store that CARRIES FILES and is COLD, because an empty one
    passes for the unrelated reason tested above."""

    def test_cold_populated_non_session_store_is_not_reaped(self, tmp_path):
        sessions_dir = tmp_path / "coordinator-sessions"
        store = _session_dir(
            sessions_dir, "decisions",
            age_seconds=reap._SESSION_STALE_SECONDS * 30, meta=None,
        )
        victim = _session_dir(
            sessions_dir, "real-stale-session",
            age_seconds=reap._SESSION_STALE_SECONDS + 3600, meta=None,
        )

        reaped, deferred, failed = _reap(sessions_dir)

        assert store.exists(), (
            "`decisions/` was archived as a stale session -- the co-located "
            "stores are being walked as session candidates, and the "
            "absent-meta fallback removed the accidental defer that hid it"
        )
        assert "decisions" not in reaped
        assert reaped == ["real-stale-session"], (
            "the store skip must not also suppress genuine reaping"
        )
        assert not victim.exists()

    def test_every_denylisted_name_is_skipped_not_just_the_sampled_one(self, tmp_path):
        """Asserted against the shared frozenset rather than a local list, so a
        name added to `session.liveness` is covered here without an edit --
        the two must agree on what is not a session."""
        from coordinator_core.session.liveness import _NON_SESSION_DIR_NAMES

        sessions_dir = tmp_path / "coordinator-sessions"
        planted = []
        for name in sorted(_NON_SESSION_DIR_NAMES):
            if name.startswith("."):
                continue  # dot-prefixed names are skipped a line earlier
            _session_dir(
                sessions_dir, name,
                age_seconds=reap._SESSION_STALE_SECONDS * 30, meta=None,
            )
            planted.append(name)

        reaped, deferred, failed = _reap(sessions_dir)

        assert planted, "denylist yielded no non-dot names to plant"
        assert reaped == [], (
            "denylisted store(s) reaped: %s" % sorted(set(reaped) & set(planted))
        )
        for name in planted:
            assert (sessions_dir / name).exists()


    def test_underscore_prefixed_store_is_skipped_without_being_denylisted(self, tmp_path):
        """`_`-prefix means "not a session" by the same convention sub-reap (ii)
        uses for `.archive/_agents-<aid>-<date>`.

        Pinned on a name that is deliberately NOT in `_NON_SESSION_DIR_NAMES`,
        because the point is coverage WITHOUT a denylist entry. The live case is
        `_branch-overrides/overrides.log` in a sibling tree that runs this
        engine: an append-only override audit trail no code in this repo
        writes, so it could not have been anticipated by name from here."""
        sessions_dir = tmp_path / "coordinator-sessions"
        from coordinator_core.session.liveness import _NON_SESSION_DIR_NAMES

        assert "_branch-overrides" not in _NON_SESSION_DIR_NAMES, (
            "this test is meaningless once the name is denylisted -- it exists "
            "to pin the prefix rule, not the entry"
        )
        store = _session_dir(
            sessions_dir, "_branch-overrides",
            age_seconds=reap._SESSION_STALE_SECONDS * 50, meta=None,
        )

        reaped, deferred, failed = _reap(sessions_dir)

        assert reaped == []
        assert store.exists(), (
            "an `_`-prefixed store was archived as a stale session -- a "
            "co-located record following the hub's own not-a-session "
            "convention is only safe if it was remembered by name"
        )

"""
coordinator_core.session.stable_pid_watch — cadence watch over whether ANY
session's ``stable_pid`` capture is missing.

Purpose: K-006 (``state/kill-ledger.md``) deregistered
``hooks.session_heartbeat`` — the sole discharge of the F0 hazard (a session
whose ``stable_pid`` capture misses, running only ``Bash``/``PowerShell`` for
30 minutes, reads DEAD on the Layer-2 recency path in
``coordinator_core/session/liveness.py::session_live`` and becomes
reap-eligible while alive). Deregistration was ruled safe ONLY because
``stable_pid`` capture currently never misses (10/10 live + 354/354 archived
sessions since 2026-08-10). That 0% is load-bearing and was, before this
module, completely unwatched — the ledger's own risk paragraph was unenforced
prose. This module is the artifact that discharges it (CLAUDE.md § north
star: name the artifact, "the operator remembers" is not one).

Threshold — ANY single miss alerts, deliberately NOT a rate/percentage. The
newest known miss is 2026-08-08 (64%, 32/50); the population of interest is
"is this reachable AT ALL", not "how often" — a single occurrence is a regime
change back toward a state K-006's ruling assumed was gone. A percentage
threshold would need a denominator (miss rate over what window, against what
expected baseline) this watch has no principled way to justify.

What counts as a miss — mirrors ``coordinator_core.session.liveness.session_live``'s
own Layer-1 decision exactly, not a re-derivation:
  - ``stable_pid`` empty/absent -> Layer 1 never engages, session_live falls
    straight to Layer 2 recency -> F0-exposed -> MISS.
  - ``stable_pid`` present but BOTH ``stable_pid_lstart`` and
    ``stable_pid_start_epoch`` are empty/absent -> session_live's own comment
    ("Birth-instant witness absent != process dead — fall through to Layer 2
    (A-F1) only when BOTH lstart and start_epoch are missing") means this
    ALSO falls through to Layer 2 -> the identical F0 exposure -> MISS.
  - ``stable_pid`` present with at least one witness -> Layer 1 engages ->
    not F0-exposed -> not a miss (regardless of whether the process is
    actually alive — aliveness is not this watch's question).
  - No ``meta.json`` at all, in a dir carrying a TOUCH RECORD whose mtime is
    within ``_NO_META_RECENCY_SECONDS`` (C4, 2026-08-22; keying widened off
    the `touched.txt` literal 2026-08-25, C5; recency-scoped and re-keyed
    onto owed-ness 2026-08-26) -> a session that reached the call which
    writes the record and did not get it, so Layer 1 provably never engaged
    -> MISS (reason ``no_meta_json``) — closes the gap where a session moved
    entirely out of "meta.json present, unstamped" into "no meta.json at all"
    would otherwise fall out of this watch's denominator and read CLEAN while
    the exposed population grows.
    A dir with no touch record is NOT counted, and that is the load-bearing
    half: ``meta.json`` is written LAZILY, so a working session legitimately
    has none for seconds to tens of minutes after it starts. See
    ``_init_is_owed`` for the measurement, and for why a directory-age check
    was tried for this job and retracted.

Why the ``no_meta_json`` branch is recency-scoped, and why the scope is
NOT a softening of AC8 (measured 2026-08-26 on host `machine-b`, 375
top-level dirs): unscoped, this branch counted **223** dirs and the probe
read "223 of 226 sessions missing stable_pid capture — K-006 is live
again". None of the 223 was a capture failure. Two systemic causes, both
measured:

  1. **The marker was planted retroactively.** ``touched.txt`` in **216**
     of the 223 (the marker the branch keyed on before C5) has a
     birth time of 2026-07-31 21:47 — one bulk event,
     the C6 touch-corpus migration (``380b3e329``, "migrate the poisoned
     touch corpus, once, behind a backup and a manifest"). The dirs
     themselves were born across 2026-07-16..07-31 and hold no
     ``started_at`` and no ``head_at_start``, so ``core.init`` never ran
     in them. The premise this branch rests on — "touched.txt is this
     repo's own signal that a session genuinely ran here" — is simply
     false for a corpus a migration back-filled.
  2. **The reaper biases the population it leaves behind.**
     ``ops/session/reap.py::_reap_stale_sessions`` archives a dir whose
     ``meta.json`` says 24h-idle, but a dir with no readable ``meta.json``
     hits its ``fail-closed-to-keep`` arm and is **deferred forever**. So
     the top-level hub converges, by construction, on "every no-meta dir
     ever created, plus today's live sessions" — 223 fossils against 8
     live sessions, all 8 of them armed. A ratio computed over that
     population measures the reaper's retention rule, not capture health.

C5's widening off the `touched.txt` literal makes the scope MORE load-
bearing, not less: keying on any record file admits dirs the literal
never reached, so the fossil corpus can only grow. Recency is the axis
that separates the two populations; the marker's name never was.

The threshold is ``ops/session/reap.py::_AGENT_STALE_SECONDS``' 24h,
deliberately reused rather than re-picked: that is already this repo's
answer to "how long before a record-bearing dir is no longer current",
and a dir past it is one the reaper itself would have taken had
it been reapable. A session that genuinely bootstraps without ``init``
still appears in the denominator on the day it happens — which is when a
regression is actionable — so AC8's gap stays closed for the live
population it was written about.

Cost constraint (cadence-path, non-negotiable per CLAUDE.md § Load norm): NO
process spawns, NO corpus walk beyond the session dirs themselves. One
``Path.iterdir()`` over ``<git-common-dir>/coordinator-sessions/`` plus one
``meta.json`` read per session dir — the same read primitive
(``core.read_meta_field``) the liveness path already uses, so this watch
never becomes a second parser of that file's shape.

Negative-spec:
  - NEVER raises. Every path — sessions dir absent, unreadable meta.json,
    unexpected exception per-entry — folds into the returned dict; a
    malformed single session's meta.json degrades that ONE entry to
    "unreadable" (still conservatively counted as a miss — an unreadable
    meta.json cannot prove Layer 1 is armed) rather than aborting the scan.
  - Does NOT determine session liveness/aliveness. This watch answers "is
    the capture mechanism intact", not "is this session alive" — orthogonal
    to ``session_live``, which this module never calls.
  - Does NOT mutate any session state, does NOT restore the heartbeat, does
    NOT write ``last_activity``. Read-only probe.

Spec backlink: state/kill-ledger.md § K-006; coordinator_core/session/liveness.py
::session_live Layer 1 comment (the exact fall-through logic mirrored above).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from coordinator_core.session import core
from coordinator_core.session import liveness

# Mirrors ops/session/reap.py::_AGENT_STALE_SECONDS (24h). Not imported from
# there: reap is an op module whose import pulls asyncio/shutil/the op
# registry onto a cadence path that must stay a directory walk and a stat.
# Kept as a named local with the backlink instead.
_NO_META_RECENCY_SECONDS: int = 24 * 3600

STATUS_MISS = "MISS"
STATUS_CLEAN = "CLEAN"
STATUS_EMPTY = "EMPTY"


def _touch_record_family(sdir: Path) -> list[Path]:
    """Every on-disk file backing this session's touch record, or `[]`.

    Routed through ``touch_record.discover_family`` — the same family-aware
    seam ``scope.py::_read_touch_record_as_legacy_lines`` reads. AC11
    retires the legacy ``touched.txt`` sibling this once unioned in on its
    own stated terms ("retires when C8's writer does"): `ab177e43f`
    repointed `claims.atomic_dedup_append` off the old dialect and
    `227b513e7` deleted the corresponding read-side union, so there is no
    second dialect left to widen for. Existence only — nothing here decodes
    a line, so the branch stays a directory walk and a stat.
    """
    from coordinator_core.session import touch_record

    return list(touch_record.discover_family(touch_record.sink_path(sdir)))


def _init_is_owed(sdir: Path, now: float) -> bool:
    """`True` iff this meta.json-less directory has reached the event that
    OWES it a ``core.init``, and reached it recently.

    ``meta.json`` is written LAZILY, not at directory creation, and this
    function is the whole reason the branch below is not a race detector.
    Measured on host `machine-b`, 2026-08-26, over every session directory
    born that day: the directory itself is created at SessionStart by
    ``bash_guards/_write_bump_session_start`` at **+0.0s, every time**, while
    ``started_at`` — the file ``core.init`` writes once and never rewrites,
    so its birth IS the instant ``init`` first ran — landed at +3.0s, +3.8s,
    +101.9s, +320.7s, +367.6s, +1194.2s and **+2394.5s**. Forty minutes, on a
    working session. One directory born 13:47 and one born 13:49 had no
    ``meta.json`` while one born 13:51 did, which is not a "too young" race
    and not a defect either — it is three sessions at different points in the
    same lazy sequence. A watch with no owed-ness predicate reports that
    ordinary sequence as K-006 exposure, and a watch that cries wolf is one
    nobody reads.

    What owes the record is a TOUCH: ``session/scope.py::touch`` calls
    ``core.init`` before it appends, and (on an engine carrying `6bf7fc291`)
    so does ``hooks/track_touched_files``. So a directory carrying a touch
    record and no ``meta.json`` is a session that reached the call and did
    not get the file — the genuine defect. A directory with no touch record
    has not reached it, and is owed nothing yet.

    Validated against the population rather than asserted: over the nine
    sessions archived on 2026-08-26, this predicate selects exactly one —
    `471733e0-…`, which edited files for thirteen minutes and died without
    ever holding a ``meta.json`` — and rejects the other eight. Over the live
    hub it rejects both freshly-born meta-less directories the probe was
    flagging, and every one of the three test-fixture directories, whose only
    file is a guard's advisory log.

    The recency half is unchanged in purpose and re-keyed onto the touch
    record's OWN mtime rather than ``liveness.newest_record_mtime``'s
    newest-of-any-file: that is what kept the 223-dir fossil corpus out (its
    back-filled ``touched.txt`` is dated 2026-07-31) while no longer letting
    an unrelated file — a guard log appended today — hold a directory in the
    window on a session's behalf.

    Negative-spec:
        - Does NOT key on the DIRECTORY's own creation age. That was this
          module's 2026-08-26 first cut and it is retracted here: it excludes
          a session running longer than the window that edits a file and gets
          no record — a real miss, silently dropped — and it buys nothing the
          touch-record scope does not already buy. Two scopes where one is
          correct is not defense in depth; it is a false negative with a
          second name.
        - Does NOT reach for ``started_at``/``head_at_start`` as the marker.
          They are absent for exactly the defect this branch must catch.
    """
    family = _touch_record_family(sdir)
    if not family:
        return False
    newest = None
    for member in family:
        try:
            candidate = int(member.stat().st_mtime)
        except OSError:
            continue
        if newest is None or candidate > newest:
            newest = candidate
    if newest is None:
        # Present but unstattable — a watch must not drop a directory it
        # cannot measure out of its own denominator.
        return True
    return (now - newest) <= _NO_META_RECENCY_SECONDS


def scan_stable_pid_misses(
    sessions_dir: Optional[Path | str] = None,
    cwd: Optional[str] = None,
) -> dict[str, Any]:
    """Scan every session dir for a Layer-1-disarming ``stable_pid`` miss.

    Args:
        sessions_dir: explicit ``coordinator-sessions`` root to scan (tests
            pass a ``tmp_path``-rooted directory here). ``None`` ->
            ``core.sessions_dir(cwd)``.
        cwd: forwarded to ``core.sessions_dir`` when ``sessions_dir`` is
            ``None``; unused otherwise.

    Returns a dict, always shaped:
        {
          "status": STATUS_MISS | STATUS_CLEAN | STATUS_EMPTY,
          "checked": int,
          "misses": [{"session": <dirname>, "reason": "empty" | "no_witness" | "unreadable" | "no_meta_json"}],
          "summary": <one-line human string>,
        }

    STATUS_EMPTY (checked == 0, misses == []) covers BOTH "no sessions dir
    yet" and "sessions dir exists but is empty" — distinct from STATUS_CLEAN
    (checked > 0, misses == []) so a caller can tell "nothing to check" apart
    from "checked N sessions, all armed". Neither is treated as MISS.
    """
    root = Path(sessions_dir) if sessions_dir is not None else _resolve_sessions_dir(cwd)

    if root is None or not root.is_dir():
        return {
            "status": STATUS_EMPTY,
            "checked": 0,
            "misses": [],
            "summary": "No coordinator-sessions directory found — nothing to check.",
        }

    try:
        entries = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return {
            "status": STATUS_EMPTY,
            "checked": 0,
            "misses": [],
            "summary": "coordinator-sessions directory unreadable — nothing to check.",
        }

    checked = 0
    misses: list[dict[str, str]] = []
    now = time.time()
    for sdir in entries:
        if sdir.name in liveness._NON_SESSION_DIR_NAMES:
            # Latent gap surfaced by widening the meta-json-less branch below
            # off the single `touched.txt` literal (C5, AC6): a known
            # non-session infra dir (`logs`, `.commit-ledger`, ...) that
            # happens to hold some unrelated file used to read as "not
            # counted" only by luck (it never carried a file literally named
            # `touched.txt`). Reusing `liveness`'s own denylist -- the same
            # one `live_session_verdicts` filters the claim-liveness
            # enumeration through -- keeps this cadence watch from mistaking
            # a known infra dir for a meta-less session, without growing a
            # second name list to keep in sync.
            continue
        meta_path = sdir / "meta.json"
        if not meta_path.is_file():
            # No meta.json at all is normally not a session record (e.g. a
            # stray non-session subdirectory under the hub) — not counted.
            # EXCEPT a dir carrying a record file (2026-08-22, C4,
            # docs/plans/2026-08-22-track-touched-files-pays-only-for-the-
            # append.md; widened off the `touched.txt` literal 2026-08-25,
            # C5, docs/plans/2026-08-25-the-legacy-touch-record-is-retired-
            # by-repointing-its-writers.md § AC6): a record file is this
            # repo's own signal that a session genuinely ran here, so a
            # meta.json-less dir bearing one is NOT "not a session record"
            # — it is exactly the population this watch exists to keep
            # visible. Keyed on the touch-record FAMILY (`touch_record.
            # discover_family` plus its legacy `touched.txt` sibling) rather
            # than a single literal, so a future record rename only DEFERS
            # this signal, never DISABLES it. Conservatively counted as a
            # miss (this module's own contract, see the "unreadable" branch
            # below) rather than silently dropped from the denominator,
            # which is the AC8 gap this branch closes.
            # Recency-scoped (2026-08-26): only while that record is newer
            # than _NO_META_RECENCY_SECONDS. Unscoped, this branch counted a
            # 223-dir fossil corpus a bulk migration back-filled — see this
            # module's docstring for the measurement.
            # Scoped to directories that have reached the event which OWES
            # them a `core.init` — a touch — and reached it inside the
            # window. `meta.json` is written LAZILY, so "no meta.json yet" is
            # the NORMAL state of a working session for anywhere from three
            # seconds to forty minutes after it starts; counting that is
            # reporting a race as a hazard. See `_init_is_owed` for the
            # measurement and for why this replaced both the
            # newest-of-any-file mtime key and the directory-age check.
            if not _init_is_owed(sdir, now):
                continue
            checked += 1
            misses.append({"session": sdir.name, "reason": "no_meta_json"})
            continue
        checked += 1
        try:
            stable_pid = core.read_meta_field(str(sdir), "stable_pid")
            if not stable_pid:
                misses.append({"session": sdir.name, "reason": "empty"})
                continue
            lstart = core.read_meta_field(str(sdir), "stable_pid_lstart")
            start_epoch = core.read_meta_field(str(sdir), "stable_pid_start_epoch")
            if not lstart and not start_epoch:
                misses.append({"session": sdir.name, "reason": "no_witness"})
        except Exception:
            # Conservative: an unreadable/unparseable meta.json cannot prove
            # Layer 1 is armed for this session, so it counts as a miss
            # rather than being silently skipped.
            misses.append({"session": sdir.name, "reason": "unreadable"})

    if checked == 0:
        return {
            "status": STATUS_EMPTY,
            "checked": 0,
            "misses": [],
            "summary": "coordinator-sessions directory has no session records — nothing to check.",
        }

    if misses:
        named = ", ".join(f"{m['session']} ({m['reason']})" for m in misses)
        return {
            "status": STATUS_MISS,
            "checked": checked,
            "misses": misses,
            "summary": (
                f"{len(misses)} of {checked} session(s) missing stable_pid capture — "
                f"F0 hazard (K-006) is live again: {named}."
            ),
        }

    return {
        "status": STATUS_CLEAN,
        "checked": checked,
        "misses": [],
        "summary": f"Checked {checked} session(s); stable_pid capture intact on all.",
    }


def _resolve_sessions_dir(cwd: Optional[str]) -> Optional[Path]:
    try:
        resolved = core.sessions_dir(cwd)
    except Exception:
        return None
    if not resolved:
        return None
    return Path(resolved)

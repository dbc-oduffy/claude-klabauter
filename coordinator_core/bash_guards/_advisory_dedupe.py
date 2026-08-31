"""coordinator_core.bash_guards._advisory_dedupe -- per-session, per-(guard,
shape) advisory dedupe: suppress a NON-BLOCKING advisory's second-and-later
firing in the same session down to its terse alternative, keyed on the exact
guard that fired plus a fingerprint of the advisory text it composed.

Item 7, state/handoffs/2026-07-30-boot-context-bloat-non-orientation-surfaces.md
(baseline: state/audits/2026-08-14-boot-payload-baseline.md § "Item 7 --
bash-spawn guard advisories" -- 40 firings / 26,475 chars across 6 sessions,
NO per-session dedupe anywhere in this package before this module).

WHY FILESYSTEM STATE -- this package's hooks are spawn-per-call: every
PreToolUse invocation is a fresh process with no memory of the last one, so
an in-process cache dedupes nothing across tool calls. Cross-process state
under the repo's own gitdir is the only option short of standing up a
resident daemon this repo does not have.

KEY GRANULARITY -- (guard_name, sha256(normalized additionalContext)[:16]),
NOT guard name alone. A single registered guard (`guard_plumbing_and_loops.
check`, for one) composes several textually distinct advisories from one
`GuardEntry` slot -- `head-tail-plumbing`, `for-loop`, `while-read`,
`powershell`, each its own `_generic_advisory(shape_label, ...)` call with
its own prose. Deduping on `guard_name` alone would let the FIRST of those
shapes silently swallow every OTHER shape that guard ever composes in the
same session -- exactly the "a different shape in the same session still
fires" property the item's own spec requires. Hashing `additionalContext`
sidesteps needing this module to import, extend, or even know about the
guard package's own shape-classification internals (`_shape_classifier.py`,
explicitly out of scope for this change; the shape distinction falls out of
the message text a guard already composes, for free, with zero coupling to
how that text was built).

NORMALIZATION -- `advisory_dedupe_key` strips any line matching the shared
`_helpers.COMMAND_LINE_LABEL` label (leading whitespace and inter-field
spacing tolerant) from `additionalContext` before hashing, so the
shape-identifying prose is what's fingerprinted, not the per-invocation
command text `_generic_advisory` (`guard_plumbing_and_loops.py`) inlines.
`_platform_verdict.platform_verdict_for_shape` never echoes the command at
all, so this normalization is a no-op there. Two calls of the SAME guard
against the SAME OR a DIFFERENT command with the same shape prose collide
onto one key (dedupe fires); two calls whose non-command prose genuinely
differs (a different shape) mint different keys and both fire. See
``advisory_dedupe_key``'s own docstring for the exact construction.

DEGRADE, DON'T SILENCE -- a repeat firing is never fully suppressed. The
caller (``dispatch.py``) swaps the envelope for
``degrade_advisory_envelope``'s shortened form (the terse "Use instead"/
rewrite span only, prose dropped) and RETURNS it, rather than `continue`ing
the guard chain -- see that function's own docstring for why the return
(vs. suppress-and-continue) also fixes a guard-precedence bug: a
`continue` on a suppressed slot let a LOWER-precedence guard win it.

FAIL OPEN, UNCONDITIONALLY -- the single most important property here,
exactly as `_write_bump_marker.py`'s own docstring insists for its marker.
A dedupe bug must never silently swallow a guard's advisory: an
unresolvable session id, an unresolvable/unwritable gitdir, a malformed
envelope, or ANY exception anywhere in this module's public surface all
resolve to "not yet advised this session" -- i.e. EMIT, never suppress.
`already_advised`/`mark_advised`/`degrade_advisory_envelope` never raise;
every branch degrades to that fail-open answer.

NEVER CALLED FOR A BLOCK. This module has no notion of "block" at all --
callers (``dispatch.py``) are responsible for only ever consulting it on an
envelope already confirmed to be a non-hard-deny (``permissionDecision !=
"deny"``) advisory. Nothing here inspects ``permissionDecision`` itself;
that is a caller-side gate, deliberately, so this module cannot itself
develop an opinion about what counts as a block.

STORAGE -- reuses ``_write_bump_marker.resolve_gitdir`` (not reimplemented;
same worktree-safe ``git rev-parse --git-dir`` resolution, same per-process
memo) rather than inventing a second git-dir resolver. Markers live under
``<gitdir>/advisory-dedupe/<session_id>/<guard_name>__<hash16>`` -- one
empty file per (session, guard, shape) triple, siblings of
`_write_bump_marker.py`'s own `<gitdir>/allow-xrepo-write-<session-id>`
marker but namespaced into their own subdirectory rather than living
directly in `gitdir`'s root, because this module can accumulate MANY
markers per session (one per distinct shape a session's commands trip) where
the write-bump marker is a single clear-once flag. `gitdir` (not
`state/subagent-share/`) is the right root here, NOT
`coordinator_core.guard_advisory_counter`'s per-session
`state/subagent-share/<session_id>/advisory-fire-counts.jsonl` hub -- that
module's own docstring is an explicit, standing PM ruling
("KEEP IT COUNT-AND-LOG, NEVER AN ENFORCEMENT INPUT ... Widening that would
need a fresh PM ruling") against ever reading its record back to change
behaviour. This module is a DIFFERENT record for a DIFFERENT purpose
(enforcement-adjacent suppression, not audit count) and deliberately does
not touch that counter's files at all.

REAPING -- opportunistic, mtime-based, on the WRITE path only (never the
read path -- `already_advised` never deletes anything, matching
`_write_bump_marker.marker_present`'s own "never opens/deletes on read"
discipline). `mark_advised` calls `_maybe_sweep_stale_session_dirs` once per
call (throttled by `_LAST_SWEEP_SENTINEL`, a dedicated file under
`<gitdir>/advisory-dedupe/` that nothing else touches -- see that
constant's own docstring for why the throttle clock had to be decoupled
from the root directory's own mtime), which removes sibling
`<gitdir>/advisory-dedupe/<other-session-id>/` directories whose mtime is
older than `_STALE_SESSION_DIR_AGE_SECONDS` (48h). This package has no
SessionEnd hook wired to THIS module (the precedent module's own SessionEnd
sweep, `_write_bump_marker.sweep_stale_markers`, is itself "hygiene only,
never load-bearing" and lives in DoE-claude's hook wiring, out of this
dispatch's file scope) -- age-based best-effort cleanup on the write path is
the self-contained alternative that needs no new cross-repo hook
registration. Exactly like its precedent's sweep, this is pure hygiene: a
stale directory this sweep fails to remove, or never gets the chance to
see, is residue (harmless -- an empty marker file for a session id that
will never recur), never a correctness dependency for
`already_advised`/`mark_advised`, which answer correctly whether or not the
sweep has ever run.

Backlink: state/lessons/2026-08-01-adding-suppression-to-an-emitter-
silently-breaks-every-gate-that-samples-it.yaml -- that lesson's
prescription is discharged here (the sampling harnesses in this package's
own test suite mint a fresh uuid4 session id per probe, so this module's
per-session suppression never shadows one probe's firing from another's),
but only incidentally: no call site here deliberately arranges for it. A
future reader tidying away the per-probe fresh-session-id convention would
silently reopen the exact class of bug that lesson documents.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core._hook_envelope import COORDINATOR_PROVENANCE_MARKER
from coordinator_core.bash_guards._helpers import COMMAND_LINE_LABEL
from coordinator_core.bash_guards._write_bump_marker import resolve_gitdir

# `_alternative_liveness` is imported LAZILY, inside `terse_alternative_text`
# below, rather than at module scope: `_alternative_liveness` transitively
# imports `dispatch` (via `block_disarm_marker_sentinel_creation` ->
# `_blanket_disarm` -> `dispatch.GuardBand`), and `dispatch` imports THIS
# module at module scope -- a top-level import here would be a circular
# import, breaking on whichever module happens to load first.

#: Matches a `COMMAND_LINE_LABEL`-labeled line (any leading whitespace, any
#: spacing between the label and the value) so it can be stripped from
#: `additionalContext` before hashing -- see `advisory_dedupe_key`'s
#: docstring and the module docstring's "NORMALIZATION" note. Built
#: from the shared `_helpers.COMMAND_LINE_LABEL` constant rather than a
#: hand-typed `"Command:"` literal -- a relabel at
#: the builder site now moves this pattern automatically instead of silently
#: reverting dedupe to command-instance keying. `re.MULTILINE` so `^`/`$`
#: anchor per line, not just at the string's ends.
_COMMAND_LINE_RE = re.compile(r"^[ \t]*" + re.escape(COMMAND_LINE_LABEL) + r"[ \t]*.*$", re.MULTILINE)

#: HOMED HERE, not in `_alternative_liveness`, because `terse_alternative_text`
#: runs on the PreToolUse hot path and these two values are all it ever needed
#: from that module. `_alternative_liveness` executes
#: `discover_write_guard_names()` at import time, which pulls in
#: `write_guards.engine` and through it the entire `coordinator_core.ops`
#: registry -- 480-710ms of process time, charged to DR-344's 500ms budget on
#: every repeat firing of the fleet's most-fired advisory. The lazy import that
#: used to sit inside `terse_alternative_text` avoided the CIRCULAR-import
#: problem noted above but not the COST one: deferring an import does not make
#: it cheaper, it only moves when it is paid, and here it was paid on the hot
#: path. `_alternative_liveness` re-exports both names from here, so its own
#: readers are unaffected.
#:
#: Deliberately BROADER than `_alternative_liveness._ALT_CUE_RE` (it also
#: matches a bare mid-sentence "instead") because a false-positive window only
#: WIDENS where backtick spans get a chance to classify, while a false-negative
#: cue silently drops a real alternative -- see that constant's own comment for
#: the full reasoning, which this move does not change.
_CUE_WINDOW_RE = re.compile(r"(Use instead:?|Did you mean|Run this instead|Example:|\binstead\b)", re.IGNORECASE)
_CUE_WINDOW_MAX_CHARS = 600

#: Subdirectory (of the resolved gitdir) markers live under -- namespaced
#: away from `_write_bump_marker.py`'s own root-level `allow-xrepo-write-*`
#: markers so a directory listing of one never needs to filter the other's
#: entries.
_DEDUPE_SUBDIR = "advisory-dedupe"

#: Best-effort write-path reap threshold (see module docstring, "REAPING").
#: 48h, not a shorter window: this machine runs long-lived sessions across a
#: day-plus, and a too-short window would reap a still-live session's own
#: markers, re-enabling the exact repetition this module exists to remove.
_STALE_SESSION_DIR_AGE_SECONDS = 48 * 60 * 60

#: Cost throttle (module docstring, "THROTTLED") -- `_sweep_stale_session_
#: dirs` only runs when `<gitdir>/advisory-dedupe/`'s own mtime is at least
#: this old. 30 minutes: far shorter than the 48h reap window (so a stale
#: directory is never meaningfully delayed in being reaped), but long
#: enough that the O(sibling sessions) listing/stat work this throttles
#: cannot recur more than twice an hour regardless of how many
#: (guard, shape) pairs fire across however many concurrent sessions.
_SWEEP_THROTTLE_SECONDS = 30 * 60


def advisory_dedupe_key(guard_name: str, envelope: Optional[Dict[str, Any]]) -> Optional[str]:
    """Fingerprint the SHAPE of a non-hard-deny advisory ``envelope`` for
    this module's dedupe purposes:
    ``<guard_name>__<sha256(normalized additionalContext)[:16]>``.

    "Normalized" strips TWO per-invocation spans before hashing, because the
    advisory family this dedupe exists for inlines the operator's command
    TWICE.

    1. Any ``Command:``-labeled line (``_COMMAND_LINE_RE``) --
       ``_generic_advisory`` (``guard_plumbing_and_loops.py``) inlines the
       literal offending command there, and hashing it verbatim would key
       dedupe on the command INSTANCE rather than the guard's SHAPE
       (module docstring, "CORRECTED").
    2. The terse-alternative span (``terse_alternative_text``, i.e. the
       ``Example:``/``Use instead:`` rewrite block) -- the SECOND inlining,
       and the one that kept dedupe inert after fix 1. That block is built
       FROM the operator's command, so it varies per invocation, is not
       ``Command:``-labeled, and survived normalization straight into the
       hash. Measured by doe-claude-em 2026-08-18 off 28 sessions'
       ``.git/advisory-dedupe/`` markers: the command-echoing shapes
       accumulated a fresh key per firing, while the shapes that never echo
       (``_platform_verdict.platform_verdict_for_shape``) held at a 1-2 key
       ceiling -- the control case, dedupe collapsing as designed.

    Stripping the alternative rather than the whole tail is deliberate: two
    firings of one guard whose explanation is identical and whose suggested
    rewrite differs ARE the same shape, which is what dedupe keys on. The
    explanatory prose that distinguishes one guard's advisory from another's
    stays in the hash. Reuses ``terse_alternative_text``'s own span
    arithmetic rather than a second cue-window implementation.

    Builders that echo neither are unaffected -- both strips are no-ops when
    their span is absent.

    Returns ``None`` (never dedupe-eligible) whenever ``envelope`` is not a
    dict, carries no ``hookSpecificOutput`` dict, or that dict's
    ``additionalContext`` is missing/empty/non-string -- there is no advisory
    text to fingerprint, so this is not a shape this module has an opinion
    about (the caller's own hard-deny gate is a separate, prior check; this
    function does not re-derive it). Never raises.
    """
    if not isinstance(envelope, dict):
        return None
    hsp = envelope.get("hookSpecificOutput")
    if not isinstance(hsp, dict):
        return None
    ctx = hsp.get("additionalContext")
    if not isinstance(ctx, str) or not ctx:
        return None
    if not guard_name:
        return None
    normalized = _COMMAND_LINE_RE.sub("", ctx)
    alternative = terse_alternative_text(normalized)
    if alternative:
        normalized = normalized.replace(alternative, "")
    digest = hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()[:16]
    return "%s__%s" % (guard_name, digest)


def terse_alternative_text(text: str) -> Optional[str]:
    """Isolate the terse offered-alternative span of one guard's rendered
    ``text`` -- everything from the first cue-word occurrence
    (``_alternative_liveness._CUE_WINDOW_RE``: ``"Use instead:"``,
    ``"Did you mean"``, ``"Run this instead"``, ``"Example:"``, or a bare
    ``"instead"``) through the next paragraph break or
    ``_CUE_WINDOW_MAX_CHARS``, whichever comes first -- reusing the SAME
    shipped cue-window arithmetic ``_cue_windows`` already applies (this
    function differs only in returning from the cue match's START, not its
    END, so the cue phrase itself stays in the returned text rather than
    being consumed by it).

    Returns ``None`` when no cue word is present at all -- there is no
    alternative span to isolate, and a caller (``degrade_advisory_envelope``)
    must not synthesize one.
    """
    match = _CUE_WINDOW_RE.search(text)
    if match is None:
        return None
    start = match.start()
    blank = text.find("\n\n", start)
    end = blank if blank != -1 else len(text)
    end = min(end, start + _CUE_WINDOW_MAX_CHARS)
    return text[start:end].rstrip()


def degrade_advisory_envelope(envelope: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Shorten a REPEAT-firing advisory ``envelope`` to carry only its terse
    alternative, dropping the explanatory prose that already fired once this
    session -- degrade, never silence.

    This is strictly NEW, shorter content relative to the first firing (the
    register contract in ``docs/wiki/guard-messaging.md`` § Register: "one
    fact, stated once, plus a terse alternative" -- the first firing
    delivers both, the repeat delivers only the alternative), so it is not a
    second delivery of the same prose. Returning a real envelope here
    (rather than suppressing to ``None``) is also what fixes the
    guard-precedence bug this finding named: the caller now RETURNS this
    shortened envelope instead of ``continue``-ing the guard chain, so a
    lower-precedence guard can no longer win the slot a higher-precedence
    one already claimed.

    Returns ``None`` when the terse alternative cannot be isolated (no cue
    word present in either prose field, or ``envelope`` carries no prose at
    all) -- the caller's own contract is to fall back to the FULL envelope
    in that case, never to silence (module docstring, "FAIL OPEN,
    UNCONDITIONALLY" -- degradation must fail open exactly like dedupe
    itself). Never raises.
    """
    try:
        if not isinstance(envelope, dict):
            return None
        hso = envelope.get("hookSpecificOutput")
        if not isinstance(hso, dict):
            return None
        for field in ("additionalContext", "permissionDecisionReason"):
            text = hso.get(field)
            if not isinstance(text, str) or not text:
                continue
            terse = terse_alternative_text(text)
            if terse is None:
                continue
            marker_prefix = (
                COORDINATOR_PROVENANCE_MARKER + " " if text.startswith(COORDINATOR_PROVENANCE_MARKER) else ""
            )
            new_hso = dict(hso)
            new_hso[field] = marker_prefix + terse
            new_envelope = dict(envelope)
            new_envelope["hookSpecificOutput"] = new_hso
            return new_envelope
        return None
    except Exception:  # noqa: BLE001 -- fail open: caller falls back to full envelope
        return None


#: `session_id` charset gate -- `session_id`
#: is used directly as a path COMPONENT (`_session_dedupe_dir`), then
#: `mkdir(parents=True)`/`touch()`'d into. `_write_bump_marker.py`'s
#: precedent module never needed a sanitizer for its own marker because it
#: embeds the session id inside a FILENAME (`f"{MARKER_PREFIX}{session_id}"`,
#: traversal-inert), not a directory-path component -- that safety does not
#: transfer to this module's different on-disk shape. Not agent-controllable
#: today (`session_id` is harness-supplied, never derived from tool input),
#: so this is defense-in-depth, not a live hole. Same charset every other
#: session-id-shaped identifier in this package already uses.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _valid_session_id(session_id: str) -> bool:
    """``True`` iff `session_id` is safe to use as a single path component.
    Fails OPEN like every other predicate in this module -- an invalid id
    means "treat as not-yet-advised" (``already_advised`` returns ``False``)
    and "skip the write" (``mark_advised`` no-ops), never a raise."""
    return bool(_SESSION_ID_RE.match(session_id))


def _session_dedupe_dir(gitdir: Path, session_id: str) -> Path:
    return gitdir / _DEDUPE_SUBDIR / session_id


def already_advised(gitdir: Optional[Path], session_id: str, shape_key: Optional[str]) -> bool:
    """``True`` iff this exact ``(session_id, shape_key)`` marker already
    exists under the resolved ``gitdir``.

    Fail-open, unconditionally (module docstring): ``gitdir is None``, an
    empty ``session_id``/``shape_key``, or any ``OSError`` probing the
    marker path all return ``False`` -- "not yet advised", never a raise.
    """
    if gitdir is None or not session_id or not shape_key:
        return False
    if not _valid_session_id(session_id):
        return False
    try:
        return (_session_dedupe_dir(gitdir, session_id) / shape_key).exists()
    except OSError:
        return False


def mark_advised(gitdir: Optional[Path], session_id: str, shape_key: Optional[str]) -> None:
    """Record that ``(session_id, shape_key)`` has now fired once, so a
    later identical firing in the same session is suppressed by
    ``already_advised``.

    Fail-open, unconditionally: never raises. A failed write (unwritable
    gitdir, disk full, a vanished parent) leaves no marker behind, which
    means the NEXT identical firing simply re-advises rather than staying
    silently suppressed forever -- the safe direction for this module's own
    fail-open contract (module docstring: "an advisory must never be
    silently swallowed" -- and a missing marker can only ever cause an
    EXTRA advisory, never a missing one).

    Opportunistically reaps stale sibling session directories, throttled to
    at most once per `_SWEEP_THROTTLE_SECONDS` (module docstring,
    "THROTTLED") -- best-effort, itself fail-open, never lets a reap
    failure block this call's own write.
    """
    if not session_id or not shape_key:
        return
    if gitdir is None:
        return
    if not _valid_session_id(session_id):
        return
    try:
        target_dir = _session_dedupe_dir(gitdir, session_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / shape_key).touch(exist_ok=True)
    except OSError:
        pass
    try:
        _maybe_sweep_stale_session_dirs(gitdir, session_id)
    except OSError:
        pass


#: Dedicated throttle sentinel -- a plain
#: empty file under `<gitdir>/advisory-dedupe/`, written ONLY by
#: `_maybe_sweep_stale_session_dirs` itself. `mark_advised`'s own `mkdir` of
#: a NEW session directory also bumps the `advisory-dedupe/` root's own
#: mtime, which used to BE the throttle clock -- under this repo's
#: documented 50-70-concurrent-session load norm, new sessions arrive
#: continuously, so that shared clock was reset by the very activity it was
#: meant to throttle and the sweep effectively never ran (corroborated: 30
#: unreaped session directories on disk). A sentinel file nothing else ever
#: touches decouples "when did the sweep last run" from "when was a sibling
#: entry last created".
_LAST_SWEEP_SENTINEL = ".last-sweep"


def _maybe_sweep_stale_session_dirs(gitdir: Path, current_session_id: str) -> int:
    """Throttling wrapper around `_sweep_stale_session_dirs` (module
    docstring, "THROTTLED") -- only actually sweeps when
    `<gitdir>/advisory-dedupe/_LAST_SWEEP_SENTINEL`'s own mtime is at least
    `_SWEEP_THROTTLE_SECONDS` old (or the sentinel does not exist yet), so
    the O(sibling sessions) listing/stat cost cannot recur on every
    `mark_advised` call. The sentinel is a DEDICATED file nothing else ever
    touches -- unlike the root directory's own mtime, it is
    never bumped by an unrelated `mkdir` of a new sibling session directory,
    so the throttle actually throttles under load instead of being reset by
    the very traffic it exists to bound. Fail-open: a missing or unwritable
    root/sentinel sweeps immediately (nothing to throttle -- either there is
    no sweep work yet, or a stale timestamp read should never itself
    suppress reaping). Never raises.

    Piggybacks `coordinator_core.telemetry.log_rotation.rotate_all_known_
    sinks` on the SAME throttle -- this is the one cadence site in the
    package that (a) runs on ordinary bash-guard-advisory traffic, not a
    rare/manual path, and (b) is already rate-limited to once per
    `_SWEEP_THROTTLE_SECONDS` (30 min) independent of the fleet's session
    count, so wiring a second, unrelated cadence primitive here costs
    nothing beyond what the stale-session sweep already pays. At the
    documented 50-70-concurrent-session load norm this throttle interval
    fires far more often than the rotation module's own ~3-4-day-per-
    generation sizing needs, so the log cannot blow past
    `log_rotation._ROTATE_THRESHOLD_BYTES` between firings. Best-effort and
    independently fail-open (see that module's own "Never raises"
    negative-spec) -- a rotation failure never blocks or is blocked by the
    stale-session-dir sweep.
    """
    root = gitdir / _DEDUPE_SUBDIR
    sentinel = root / _LAST_SWEEP_SENTINEL
    try:
        sentinel_mtime = sentinel.stat().st_mtime
    except OSError:
        removed = _sweep_stale_session_dirs(gitdir, current_session_id)
        _rotate_known_logs(gitdir)
        _touch_sweep_sentinel(root, sentinel)
        return removed
    if (time.time() - sentinel_mtime) < _SWEEP_THROTTLE_SECONDS:
        return 0
    removed = _sweep_stale_session_dirs(gitdir, current_session_id)
    _rotate_known_logs(gitdir)
    _touch_sweep_sentinel(root, sentinel)
    return removed


def _rotate_known_logs(gitdir: Path) -> None:
    """Best-effort rotation of the four known unbounded log sinks under
    `<gitdir>/coordinator-sessions/logs/` -- see
    `coordinator_core.telemetry.log_rotation` module docstring. Swallows
    every exception; a rotation defect must never surface through the
    advisory-dedupe path it piggybacks on.
    """
    try:
        from coordinator_core.telemetry.log_rotation import rotate_all_known_sinks

        rotate_all_known_sinks(gitdir / "coordinator-sessions" / "logs")
    except Exception:
        pass


def _touch_sweep_sentinel(root: Path, sentinel: Path) -> None:
    """Reset the throttle clock after a sweep attempt -- best-effort,
    fail-open: an unwritable sentinel just means the NEXT `mark_advised`
    call re-attempts the sweep sooner than `_SWEEP_THROTTLE_SECONDS`, which
    only costs extra listing work, never a correctness regression."""
    try:
        root.mkdir(parents=True, exist_ok=True)
        sentinel.touch(exist_ok=True)
        os.utime(sentinel, None)
    except OSError:
        pass


def _sweep_stale_session_dirs(gitdir: Path, current_session_id: str) -> int:
    """Best-effort age-based reap of sibling session directories under
    ``<gitdir>/advisory-dedupe/`` (module docstring, "REAPING"). Removes any
    entry OTHER than ``current_session_id`` whose most-recently-modified
    marker is older than ``_STALE_SESSION_DIR_AGE_SECONDS``.

    Never raises: any ``OSError`` (listing, stat, unlink -- a race, a
    permission error, a vanished entry) is swallowed per-entry and sweeping
    continues with the rest, exactly like `_write_bump_marker.sweep_stale_
    markers`'s own per-entry fail-open contract. Returns the count of
    session directories actually removed (0 on every fail-open path);
    callers do not need this return value today but it keeps the function
    testable in isolation.
    """
    root = gitdir / _DEDUPE_SUBDIR
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    now = time.time()
    removed = 0
    for entry in entries:
        if entry.name == current_session_id:
            continue
        try:
            if not entry.is_dir():
                continue
            newest_mtime = entry.stat().st_mtime
            try:
                for child in entry.iterdir():
                    child_mtime = child.stat().st_mtime
                    if child_mtime > newest_mtime:
                        newest_mtime = child_mtime
            except OSError:
                pass
            if (now - newest_mtime) < _STALE_SESSION_DIR_AGE_SECONDS:
                continue
            for child in entry.iterdir():
                try:
                    child.unlink()
                except OSError:
                    continue
            entry.rmdir()
            removed += 1
        except OSError:
            continue
    return removed

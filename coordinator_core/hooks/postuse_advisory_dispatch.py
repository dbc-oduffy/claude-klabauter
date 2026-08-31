"""
coordinator_core.hooks.postuse_advisory_dispatch — PostToolUse advisory dispatcher op.

Purpose: Folds six PostToolUse advisory checks — context-pressure, runtime-tripwire,
a one-time first-Agent-dispatch sidecar advisory, the unauthorized-handoff nudge, the
workflow-monitor arming advisory, and the Group EM watch arming advisory — into a
single in-process op, eliminating bash.exe spawns per tool call on Windows.
Context-pressure, runtime-tripwire, and the Group EM watch arming check fire on ALL
PostToolUse events (no tool_name gate — all three are universal, cheap to
short-circuit); the first-Agent-dispatch advisory fires only on tool_name == "Agent",
and only once per session; the unauthorized-handoff nudge only on tool_name == "Write"
with a handoff/spinoff file_path; the workflow-monitor arming advisory only on
tool_name == "Workflow", and only once per task id per session. The latter three
narrow themselves internally — no handler-level tool_name gate is applied to the
universal three.

The session-scoped checks run concurrently via asyncio.gather. Whichever fire have
their additionalContext texts merged with a blank-line separator into ONE
post_advisory() call (a PostToolUse hook must emit at most one JSON object). When none
fire, no_advisory() is returned.

Port of: postuse-advisory-dispatch.sh (DoE 2f8b8450, 2026-07-16). The first-Agent-
dispatch sidecar advisory, the workflow-monitor arming advisory, and the Group EM
watch arming advisory have no bash-era equivalent — added directly here. The
unauthorized-handoff nudge is a fan-in of DoE's separate PostToolUse(Write)
registration, whose logic already lived in this engine
(coordinator_core.hooks.nudge_unauthorized_handoff, still registered as its own op for
direct callers) — folding it here drops Write's registration count by one.

Translation notes:
    check_context_pressure      → _check_context_pressure_sync (session_id + transcript_path)
    check_runtime_tripwire      → _check_runtime_tripwire_sync (session_id + agent_id)
    (new) first-Agent-dispatch  → _check_first_agent_dispatch_sync (session_id + tool_name)
    (fan-in) unauthorized-handoff → nudge_unauthorized_handoff.advisory_text
                                    (tool_name + file_path + content + transcript_path)
    (new) workflow-monitor-arm  → _check_workflow_monitor_arm_sync
                                    (session_id + transcript_path + tool_name)
    (new) group-em-watch-arm    → _check_group_em_watch_arm_sync
                                    (session_id + transcript_path)
    jq merge logic               → plain string concatenation + post_advisory()
    All six return str advisory text or "" — "" means "did not fire".

Spec backlink: pln-pcore-04-advisory-hook-ops-mak-b219a8 § C7,
docs/plans/2026-08-31-the-group-em-tick-carries-standing-obligations.md § C10
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import time

from coordinator_core.ipc import register_op
from coordinator_core.hooks._envelope import no_advisory, post_advisory
from coordinator_core.hooks._payload import field
from coordinator_core.hooks import nudge_unauthorized_handoff
from coordinator_core.session.autonomous_sentinel import sentinel_path  # noqa: F401 -- back-compat monkeypatch target, see below
from coordinator_core.session.context_usage_sidecar import read_usage
from coordinator_core.session.mode_resolution import resolve_mode

# `sentinel_path` above is no longer called by this module's own logic (both
# call sites now go through `resolve_mode("autonomous", ...)`, C3 of
# 2026-08-28-the-fleet-gets-one-file-and-the-floor-moves-to-the-reader.md) --
# kept as a live module attribute solely because
# coordinator_core/hooks/tests/test_postuse_context_pressure.py's `_under_sentinel`
# helper (out of this chunk's file scope) monkeypatches BOTH
# `autonomous_sentinel.sentinel_path` (which resolve_mode's `_autonomous_session_value`
# actually reads, module-qualified, so this patch is what changes behaviour) AND this
# module's own `sentinel_path` name, redundantly. Removing this import raises
# AttributeError on that second, now-inert patch. Do not remove without updating that
# test file in its own chunk.


def _tempfile():
    """Function-local accessor for the `tempfile` module.

    Used from 5 functions in this module (advisory state I/O, the Sonnet-
    generation monitor, and the three PostToolUse checks) — a shared
    accessor avoids repeating `import tempfile` at every call site while
    keeping the module import out of eager module-load cost.

    Deliberately does NOT memoize the module reference into a module-level
    variable, unlike `ipc.py`'s/`lifecycle.py`'s `_log()`: `sys.modules`
    already caches the real `tempfile` import, so a repeated `import
    tempfile` here is a cheap dict lookup, not a re-import. `_log()`
    memoizes because `logging.getLogger(__name__)` does real construction
    work beyond the module lookup on every call — a cost this accessor
    doesn't have. Review: code-reviewer (P3 nit).
    """
    import tempfile
    return tempfile


#: Characters with no single form that both cmd.exe and a POSIX shell read
#: literally. `$` and backtick still expand INSIDE POSIX double quotes; a double
#: quote closes the quoting in both; a newline ends the command outright.
_ARG_UNSAFE_CHARS = '"$`\n\r'


def _portable_arg(value: str) -> str | None:
    """One argument that BOTH cmd.exe and a POSIX shell tokenize identically,
    or None when no such form exists for this value.

    `shlex.quote` was wrong here. It emits POSIX single-quote syntax, which is
    meaningful only to a POSIX shell -- on a Windows-first repo that is a
    POSIX-only primitive, and it worked only because the harness happened to
    pipe this command through bash on the one box it was measured on. An
    undocumented harness detail is not a portability argument.

    The portable form is a double-quoted path with FORWARD slashes:
      - cmd.exe honours double quotes, and normalisation leaves it no
        backslash to treat as an escape.
      - A POSIX shell honours double quotes and never sees a backslash to eat,
        which was the original corruption (a Windows path arriving as C:Users).
      - Windows path APIs and Python's os module both accept forward slashes,
        so the receiving watcher is unaffected.

    Returns None for a value carrying a character that cannot be made safe in
    both shells at once. The caller then emits NOTHING. A silent advisory is
    recoverable; a subtly wrong command line that aims a watcher at the wrong
    path is not, and neither is one that lets a filename interpolate a shell
    expression.
    """
    normalized = str(value).replace('\\', '/')
    if any(ch in normalized for ch in _ARG_UNSAFE_CHARS):
        return None
    return '"' + normalized + '"'

#: Generator-provenance declaration: every durable-state write in this
#: module (advisory-hook-state-<sid>.json, rt-bark-once-<sid>,
#: first-agent-dispatch-advisory-<sid>) lands under tempfile.gettempdir() —
#: never a tracked repo artifact.
GENERATES: list = []

# ---------------------------------------------------------------------------
# Anthropic-side fixed auto-compaction ceiling on the 1M-context tier.
#
# This is NOT one of our tunables — it is a fixed cost/attention ceiling
# Anthropic applies on the 1M window (observed 2026-07-13, unchanged since),
# decoupled from window size. It is why the red band in
# _check_context_pressure_sync sits below 50%: on a 1M window a flat 50%
# coincides EXACTLY with this ceiling (500_000 tokens), firing the warning
# level with the cut instead of ahead of it and defeating the whole point of a
# pre-emptive advisory — a handoff needs runway to compose before an
# involuntary, lossy auto-compaction lands. The band sat at 47, then briefly at
# 45, both on 2026-08-30: auto-compaction was observed firing at 47, and then
# again at 47 with the band already at 45 — a warning at 45 has only two points
# of runway before the cut, which is not enough to compose a handoff in. PM
# ruling 2026-08-30 moved it to 43. Referenced by comment rather than by
# arithmetic: the bands are
# PM-set percentages, not values derived from this constant, and deriving them
# from it would silently move them if Anthropic moves the ceiling.
# ---------------------------------------------------------------------------
_AUTO_COMPACT_CEILING_TOKENS_1M = 500_000

# ---------------------------------------------------------------------------
# Durable per-session advisory state (file-backed).
#
# Regression note (fixed here): B-F1 re-plumbed these guards from /tmp sentinel
# files to in-memory module-level structures so this op stayed COMPUTE_ONLY
# (opened no file for write). That missed a load-bearing fact: this op is
# dispatched via a FRESH process per PostToolUse fire (DR-215 retired the
# resident daemon — coordinator_core/ipc.py:6-9,232-235). In-memory state
# never survives past the single call that created it, so every guard
# re-initialized empty on every fire and NONE of throttle / bark-once / dedup
# ever suppressed anything. Most visible as "COMPACTION OCCURRED" re-firing on
# every subsequent PostToolUse call for the rest of the session, because the
# sentinel file was no longer deleted (see the Phase 1 block below) and the
# in-memory consumption marker never persisted.
#
# Fix: persist state to a JSON sidecar in tempdir, keyed by session_id — same
# tempdir + session_id convention already used by the PreCompact sentinel this
# op reads (compaction-occurred-{session_id} / compaction-state-{session_id}.md,
# written by hooks.context_pressure_precompact). Writing this file makes the op
# MUTATING per DR-208 § 2 ("ambiguous cases — cache writes, lock files, temp
# files, advisory markers — classify MUTATING and fail-closed"); reclassified
# accordingly in coordinator_core/authz/classification.py. DR-208's
# single-writer-queue cost for MUTATING ops is an HTTP-exposure-time concern
# only (op_scopes.py: "HTTP/UDS gating vacated by DR-215 ... MUTATING ops are
# serial-by-construction in the in-process model") — there is no HTTP surface
# today, so this reclassification carries no runtime cost on the hot
# PostToolUse path this whole op exists to keep spawn-free.
#
# Concurrency note: writes are tmp-file + os.replace (atomic rename), so a
# reader never observes a partially-written file. Two near-simultaneous hook
# fires can still race a read-modify-write (last writer wins) — for a soft
# advisory signal this means "may fire twice instead of once" in the rare
# race window, never corruption. Deliberately no flock-based locking (Windows
# portability cost) for what remains a soft, best-effort signal.
#
# Disjoint files, not one shared file: context-pressure state
# (throttle/advisory/critical) and the runtime-tripwire bark-once sentinel are
# read/written by two different functions that run CONCURRENTLY within the
# same process via asyncio.gather + asyncio.to_thread (see the op handler
# below) — sharing one file across them would race a lost update between the
# two threads' independent load/mutate/save cycles.
# ---------------------------------------------------------------------------


def _advisory_state_path(tmpdir: str, session_id: str) -> str:
    return os.path.join(tmpdir, f"advisory-hook-state-{session_id}.json")


def _load_advisory_state(tmpdir: str, session_id: str) -> dict:
    """Read durable per-session context-pressure state.

    Never raises — missing/corrupt state degrades to an empty dict, which
    re-arms every guard. That is the fail-open direction: an advisory may
    re-fire once, but never gets stuck permanently suppressed by a corrupt
    state file.
    """
    try:
        with open(_advisory_state_path(tmpdir, session_id), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_advisory_state(tmpdir: str, session_id: str, state: dict) -> None:
    """Atomically persist durable per-session context-pressure state.

    Never raises — a failed write degrades to state loss (next call re-arms
    the guards it couldn't persist), not a crash.
    """
    path = _advisory_state_path(tmpdir, session_id)
    try:
        fd, tmp_path = _tempfile().mkstemp(dir=tmpdir, prefix=".advisory-hook-state-", suffix=".tmp")
    except Exception:
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(state, fh)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def _mark_advisory_fired(state: dict, transcript_hash: str, *, critical: bool) -> None:
    """Record a bark-once firing in `state` (mutated in place).

    A critical firing also counts as an advisory firing (mirrors the original
    _ADVISORY_FIRED.add() + _CRITICAL_FIRED.add() pairing) so a session that
    jumps straight past the advisory threshold to critical doesn't then also
    emit the advisory text on a later call for the same transcript_hash.
    """
    fired = state.setdefault("advisory_fired", [])
    if transcript_hash not in fired:
        fired.append(transcript_hash)
    if critical:
        cfired = state.setdefault("critical_fired", [])
        if transcript_hash not in cfired:
            cfired.append(transcript_hash)


# ---------------------------------------------------------------------------
# resolve_subagent_identity (pure function — mirrors coordinator-session.sh)
# ---------------------------------------------------------------------------


def _resolve_subagent_identity(agent_id: str, session_id: str) -> str:
    """Translate a subagent-side agent_id to the canonical EM-side id.

    Three paths (mirrors coordinator-session.sh resolve_subagent_identity):
        (a) Bare hex  ^[a-f0-9]{12,}$  — unnamed agent fast path; return unchanged.
        (b) Named teammate  ^a(.+)-[a-f0-9]{16}$  — build "<name>@session-<short8>".
        (c) Anything else → "" (fail-closed).

    Pure function — no filesystem I/O, no side effects.
    """
    if re.match(r"^[a-f0-9]{12,}$", agent_id):
        return agent_id

    m = re.match(r"^a(.+)-[a-f0-9]{16}$", agent_id)
    if m:
        name = m.group(1)
        if len(session_id) < 8:
            return ""
        short = session_id[:8]
        return f"{name}@session-{short}"

    return ""


# ---------------------------------------------------------------------------
# runtime_threshold_minutes (mirrors lib/runtime-thresholds.sh)
# ---------------------------------------------------------------------------


def _runtime_threshold_minutes(model: str) -> int:
    """Return runtime threshold minutes for a given model ID.

    Mirrors runtime-thresholds.sh runtime_threshold_minutes():
        [1m]/  -1m variants → RUNTIME_TRIPWIRE_OPUS_MIN   (default 25)
        *opus*              → RUNTIME_TRIPWIRE_OPUS_MIN   (default 25)
        *sonnet*            → RUNTIME_TRIPWIRE_SONNET_MIN (default 12)
        *haiku*             → RUNTIME_TRIPWIRE_HAIKU_MIN  (default 10)
        unknown / empty     → RUNTIME_TRIPWIRE_OPUS_MIN   (default 25)

    Env-var overrides are honoured (same names as the bash originals).
    """
    if "[1m]" in model or "-1m" in model:
        return int(os.environ.get("RUNTIME_TRIPWIRE_OPUS_MIN", "25"))
    if "opus" in model:
        return int(os.environ.get("RUNTIME_TRIPWIRE_OPUS_MIN", "25"))
    if "sonnet" in model:
        return int(os.environ.get("RUNTIME_TRIPWIRE_SONNET_MIN", "12"))
    if "haiku" in model:
        return int(os.environ.get("RUNTIME_TRIPWIRE_HAIKU_MIN", "10"))
    return int(os.environ.get("RUNTIME_TRIPWIRE_OPUS_MIN", "25"))


# ---------------------------------------------------------------------------
# _check_context_pressure_sync
# (mirrors context-pressure-advisory.sh check_context_pressure)
# ---------------------------------------------------------------------------


def _check_context_pressure_sync(session_id: str, transcript_path: str) -> str:
    """Blocking context-pressure advisory check.

    Returns non-empty advisory text when a threshold fires; "" otherwise.
    Never raises — fail-open on all I/O errors.

    Phase 1: Post-compaction sentinel bridge.
        /tmp/compaction-occurred-{session_id}  — sentinel written by PreCompact hook.
        /tmp/compaction-state-{session_id}.md  — session state snapshot.
        Consumption is delete-on-read (os.unlink) — the once-only firing guard
        for a SPECIFIC compaction event, not a per-session eternal flag; a long
        session can compact more than once, and each PreCompact fire rewrites a
        fresh sentinel that must be picked up again. False-positive guard: if
        transcript hasn't shrunk >=15% since PreCompact fired, treat as a false
        alarm and consume silently.

    Phase 2: Throttled (5 min) threshold warnings, sidecar-sourced.
        Two bands and only two: 40% of window (orange — consider a handoff if
        the work cannot close in ~3% more) and 43% (red — handoff now, ahead of
        compaction). Nothing fires below 40, and a session with no usable
        reading gets silence, not an escalating UNKNOWN notice.

        The percentage comes from
        `coordinator_core.session.context_usage_sidecar.read_usage` — the
        registered statusline is that sidecar's sole writer — never from the
        transcript (see the module Anti-scope comment above Phase 2 below).
        Self-throttle + bark-once guards persisted to a durable per-session JSON
        state file (_load_advisory_state / _save_advisory_state) — survives the
        fresh-process-per-fire execution model (see the module-level state-
        management comment above _advisory_state_path). Bark-once guards keyed
        by a hash of transcript_path (stable for the life of a session — this
        never opens the transcript).
    """
    if not session_id:
        return ""

    # Review: code-reviewer (B-F3) — use tempfile.gettempdir() throughout;
    #   docstring motivates Windows portability and /tmp/ does not exist there.
    tmpdir = _tempfile().gettempdir()

    # -----------------------------------------------------------------------
    # Phase 1: Post-compaction sentinel bridge
    # -----------------------------------------------------------------------
    compaction_sentinel = os.path.join(tmpdir, f"compaction-occurred-{session_id}")
    compaction_state = os.path.join(tmpdir, f"compaction-state-{session_id}.md")

    if os.path.isfile(compaction_sentinel):
        # Consume-by-delete (restored — B-F1 had replaced this with an
        # in-memory-only marker to keep this op COMPUTE_ONLY; that marker never
        # survived the fresh-process-per-fire model, so the advisory re-fired on
        # every subsequent call. Deleting the sentinel is the once-only firing
        # guard for THIS compaction event specifically — a per-session eternal
        # "already consumed" flag would be wrong here, since a long session can
        # compact more than once and each PreCompact fire rewrites a fresh
        # sentinel that must be picked up again).
        #
        # Read pre-compaction transcript size BEFORE deleting, so a delete
        # failure can't strand us mid-read.
        pre_size = 0
        try:
            with open(compaction_sentinel, encoding="utf-8") as fh:
                line = fh.readline().strip()
                if line.isdigit():
                    pre_size = int(line)
        except Exception:
            # pre_size stays 0, which the false-positive guard below treats
            # as "no baseline to compare against" -- degrades to skipping
            # the shrink check rather than blocking the compaction advisory.
            pass

        try:
            os.unlink(compaction_sentinel)
        except Exception:
            # Delete failed -- fail open toward "fires again next call" rather
            # than silently losing the advisory forever.
            pass

        # False-positive guard: post_size must be < pre_size * 0.85 to count as real.
        # Mirrors context-pressure-advisory.sh.
        if pre_size > 0 and transcript_path and os.path.isfile(transcript_path):
            try:
                post_size = os.path.getsize(transcript_path)
                threshold = pre_size * 85 // 100
                if post_size >= threshold:
                    # No meaningful shrink — consume silently (clean up the
                    # state snapshot too) and exit.
                    try:
                        os.unlink(compaction_state)
                    except Exception:
                        pass
                    return ""
            except Exception:
                # getsize() failed -- fall through and treat the guard as
                # inconclusive rather than blocking the advisory.
                pass

        # Read + consume state snapshot (if present).
        state_content = ""
        if os.path.isfile(compaction_state):
            try:
                with open(compaction_state, encoding="utf-8") as fh:
                    state_content = fh.read()
            except Exception:
                # state_content stays "" -- the generic COMPACTION OCCURRED
                # message below still fires without the snapshot appended,
                # which is strictly better than failing the advisory.
                pass
            try:
                os.unlink(compaction_state)
            except Exception:
                pass

        if state_content:
            preamble = (
                "COMPACTION OCCURRED: Context was compressed. Tasks survived "
                "(use TaskList/TaskGet to re-orient). Re-read any active plan files "
                "to restore continuity. Key decisions should already be on disk — "
                "verify by checking your task list. Check metadata.tried_and_abandoned "
                "on tasks for failed approaches before retrying anything."
                "\n\n--- PRE-COMPACTION STATE SNAPSHOT ---\n"
            )
            postamble = "\n--- END SNAPSHOT ---"
            return preamble + state_content + postamble
        else:
            return (
                "COMPACTION OCCURRED: Context was compressed. Tasks survived "
                "(use TaskList/TaskGet to re-orient). Re-read any active plan files "
                "to restore continuity. Key decisions should already be on disk — "
                "verify by checking your task list. Check metadata.tried_and_abandoned "
                "on tasks for failed approaches before retrying anything."
            )

    # -----------------------------------------------------------------------
    # Phase 2: Throttled threshold warnings, sourced from the context-usage
    # sidecar (coordinator_core.session.context_usage_sidecar) — never the
    # transcript. Anti-scope: no tail scan, no count_tokens call, no default
    # window, and no estimate of any kind. Sidecar-absence (or an unusable
    # reading) is silence, full stop.
    # -----------------------------------------------------------------------
    throttle_seconds = 300  # 5 minutes

    # Durable throttle/bark-once state — file-backed (see the module-level
    # comment above _advisory_state_path for why in-memory doesn't work here).
    cp_state = _load_advisory_state(tmpdir, session_id)
    last_check = cp_state.get("throttle_last_check", 0.0)
    if time.time() - last_check < throttle_seconds:
        return ""  # fast path — checked recently

    # Update throttle timestamp (even if no advisory fires) and persist now —
    # this write must land even if we return early below.
    cp_state["throttle_last_check"] = time.time()
    _save_advisory_state(tmpdir, session_id, cp_state)

    # --- Bark-once key: a hash of transcript_path, not its contents. This
    # never opens the transcript — transcript_path is stable for the life of
    # a session, so the hash is effectively a per-session dedup key, same
    # shape as before the sidecar rewire.
    try:
        transcript_hash = (
            hashlib.md5(transcript_path.encode()).hexdigest() if transcript_path else session_id
        )
    except Exception:
        transcript_hash = session_id

    # --- Autonomous run detection (session-wins key via the resolve_mode seam) ---
    autonomous_run = resolve_mode("autonomous", session_id)

    # --- compaction_warnings variant selector (fleet-wins key via resolve_mode).
    # SELECTOR ONLY — never an off switch. No value of this key returns "" here
    # where the function would otherwise return advisory text; it only picks
    # which non-empty variant fires (see mode_resolution module docstring).
    # Scope, since PM ruling 2026-08-29: this key governs the red band ONLY. The
    # 40 band is informational for every session regardless, so there is no
    # variant left there to select — see that branch's own comment.
    # Cost: one stat + one small json.loads via read_fleet_mode() on this hot
    # path (fires every PostToolUse turn boundary, every session) — documented
    # never-raise/fail-open, 27.6us median / 69.0us p99. See
    # coordinator_core.session.fleet_mode.read_fleet_mode's docstring for the
    # budget this call site draws against.
    compaction_warnings_variant = resolve_mode("compaction_warnings", session_id)

    reading = read_usage(session_id, now=time.time())
    context_window_block = reading.context_window if reading is not None else None

    used_percentage = (
        context_window_block.get("used_percentage")
        if isinstance(context_window_block, dict)
        else None
    )

    # --- Unmeasured is SILENT. Not a ladder, not a streak, not a one-time
    # heads-up. A session with no usable reading gets nothing from this check,
    # for the whole session.
    #
    # This replaces a bounded UNKNOWN escalation (1st/3rd/10th consecutive
    # miss). PM ruling, 2026-08-18, after that ladder ran unbounded across the
    # fleet: the reader had been resolving a path nothing wrote, so EVERY
    # interactive session took this branch on every fire and the escalation was
    # the only context signal anyone ever saw. The ruling is not "the ladder
    # was mistuned" — it is that an advisory with no measurement behind it is
    # noise by construction, and noise on this channel costs an EM's attention
    # on every tool call. The operator already has a live percentage in the
    # terminal status line; that surface is where an unmeasurable reading shows
    # up (rendered as an em-dash), and it costs nothing to ignore.
    # `bool` is an `int` and would read as 1%; NaN/inf survive an isinstance
    # check and then raise on int(); a negative is a figure the harness should
    # never emit and must not round toward "this session is empty". All four
    # are no-reading, not zero.
    if (
        isinstance(used_percentage, bool)
        or not isinstance(used_percentage, (int, float))
        or not math.isfinite(used_percentage)
        or used_percentage < 0
    ):
        # The throttle stamp was already persisted above and nothing has
        # mutated cp_state since, so this path saves nothing further — it is
        # the common case for headless sessions and runs once per tool call.
        return ""

    # `round`, not `int`. The statusline renders the same figure with round()
    # and colours its orange band at 40, so truncating here would show the
    # operator an orange "40%" in the terminal with no advisory behind it for
    # every raw value in [39.5, 40.0). The two halves of this contract agree
    # at the boundary or the boundary is not observable.
    # Review: code-reviewer (P2).
    display_pct = round(used_percentage)
    age_note = f" (measured {int(reading.age_seconds)}s ago)" if reading is not None else ""

    # --- The two bands, and there are only two (PM-set, 2026-08-18).
    #
    # 40 — ORANGE. "Consider a handoff if this work cannot close within about
    #      another 5% of window." An orientation signal, not an instruction.
    # 43 — RED. "Go to handoff now, before compaction takes the choice away."
    #
    # Why 43 and not 50 on the 1M tier: auto-compaction fires at a fixed
    # ~500K tokens there (_AUTO_COMPACT_CEILING_TOKENS_1M), so a 50% trigger
    # coincides EXACTLY with the cut instead of landing ahead of it, and a
    # handoff needs runway to compose. Why not 47, and why not 45: this band
    # was 47 until 2026-08-30, moved to 45 that day because auto-compaction was
    # observed firing at 47, and moved again the same day because a compaction
    # was then observed at 47 with the band at 45 — two points of runway is not
    # enough to compose a handoff in. PM ruling 2026-08-30: 43.
    #
    # NOTHING fires below 40. No checkpoint prompts, no "consider wrapping",
    # no informational heads-up at 15/20/25%. That is the PM ruling, stated as
    # a floor rather than a default: a check added here that fires under 40
    # violates it no matter how quiet its wording.
    if display_pct >= 43 and transcript_hash not in cp_state.get("critical_fired", []):
        _mark_advisory_fired(cp_state, transcript_hash, critical=True)
        _save_advisory_state(tmpdir, session_id, cp_state)
        # The autonomous variant REPLACES the recommendation rather than
        # appending to it. Under the sentinel these messages are
        # informational-only and carry no `/handoff` recommendation — the mode
        # exists so a session rides through compaction instead of stopping, so
        # appending a checkpoint clause to text that still says "run /handoff"
        # delivers the exact nudge the PM switched the mode on to remove, at
        # the moment a long run is most likely to take it.
        # (Reported by doe-claude-41, observed firing twice in one session.)
        if autonomous_run or compaction_warnings_variant == "informational":
            # The mode clause is chosen by WHICH side selected this variant, not
            # by the variant itself. `autonomous_run` is the session's own
            # sentinel, so naming it is a fact about this session. The fleet key
            # is fleet-wins with no session pair, so it selects this text for
            # sessions that are NOT autonomous -- opening those with "Autonomous
            # run:" would assert something about the reader that is not true.
            mode_clause = (
                "Autonomous run:" if autonomous_run else "Informational mode:"
            )
            return (
                f"CONTEXT PRESSURE — INFORMATIONAL: ~{display_pct}% of window"
                f" used{age_note}, measured from the harness's own context_window"
                f" block. {mode_clause} compaction from here is involuntary and"
                f" lossy, so state that is not on disk is state that is lost."
                f" Commit and checkpoint now; continue the run."
            )
        return (
            f"CONTEXT PRESSURE — HANDOFF NOW: ~{display_pct}% of window used{age_note},"
            f" measured from the harness's own context_window block."
            f" This is the point to run /handoff, not to finish one more thing first —"
            f" the handoff itself consumes context, and compaction from here is"
            f" involuntary and lossy."
        )

    if display_pct >= 40 and transcript_hash not in cp_state.get("advisory_fired", []):
        _mark_advisory_fired(cp_state, transcript_hash, critical=False)
        _save_advisory_state(tmpdir, session_id, cp_state)
        # PM ruling 2026-08-29: 40 is INFORMATIONAL, the red band is STANDARD.
        # The two
        # bands were never the same kind of signal, and the mode key was doing
        # the wrong job by flipping both together. 40 is an orientation reading
        # -- "you are here, checkpoint so this is resumable" -- and there is no
        # posture, autonomous or not, in which the right response to it is to
        # stop and hand off; the earlier wording recommended exactly that. The
        # red band keeps the hard call above, because that is the last band
        # with runway to compose a handoff.
        #
        # `compaction_warnings` and `autonomous_run` are deliberately NOT read
        # in this branch: with 40 informational for everyone there is nothing
        # left here for either to select between. The key still governs the
        # red band.
        return (
            f"CONTEXT PRESSURE — INFORMATIONAL: ~{display_pct}% of window"
            f" used{age_note}, measured from the harness's own context_window"
            f" block. Checkpoint state to disk at the next natural boundary so"
            f" the run is resumable. The hard call comes at 43%."
        )

    _save_advisory_state(tmpdir, session_id, cp_state)
    return ""


# _check_runtime_tripwire_sync
# (mirrors runtime-tripwire-advisory.sh check_runtime_tripwire)
# ---------------------------------------------------------------------------


def _check_runtime_tripwire_sync(session_id: str, agent_id: str) -> str:
    """Blocking runtime-tripwire advisory check.

    Returns non-empty WRAP-SHAPE advisory text when the firing session is a subagent
    that has exceeded its model-specific runtime threshold; "" on all early-exit paths.
    Never raises — fail-open on all I/O errors.

    Repo root comes from `coordinator_core.git.repo_root.show_toplevel`, whose
    cwd-keyed memo and non-spawning parent walk matter here specifically: this
    check runs from an EMPTY-matcher PostToolUse hook, i.e. once per tool call.
    The seam returns None (never raises) for git-absent / not-a-repo / timeout,
    and never memoizes that failure, so the fail-open "" below stays correct
    and a repo that appears later in the process still resolves.

    Deferred import of the seam is deliberate, but not for import cost: measured
    under lazy op registration (the only mode), `subprocess`
    and this seam module are already in `sys.modules` after a bare import of
    this module (`from coordinator_core.ipc import register_op` pulls in
    `lifecycle` -> `git` -> `git.repo_root`, which imports `subprocess` at
    module level) — hoisting the import to module scope would add zero modules
    and zero interpreter-startup cost. The function-local placement sitting
    inside the `try/except Exception` below is FORWARD defense, not a present
    one: today the seam is already imported successfully at module scope
    before this function can ever run, so an import failure here cannot
    currently occur at all. It buys fail-open (returns "" instead of escaping
    an EMPTY-matcher PostToolUse hook) only should a future change break the
    eager `ipc -> lifecycle -> git.repo_root` chain that currently pre-imports
    it. Review: code-reviewer (P2, F4) — prior comment here claimed a
    module-load cost that does not exist; corrected to the actual reason, and
    the replacement reason itself corrected to state plainly that it is
    forward defense, not an operative one today.

    Subagent detection: SESSION_ID must appear as a dirname under
    .git/coordinator-sessions/.agents/. EM session_ids are not recorded there.
    Resolver-based fallback handles named teammates whose canonical id differs from
    SESSION_ID (mirrors coordinator-session.sh resolve_subagent_identity).

    Canonical text (WRAP-SHAPE DOCTRINE — DO NOT REWORD without updating wiki):
        "stop starting new work", "persist any partial state to disk now",
        "write a successor-handoff stub naming what's left", "return"
        (AC5 grep targets from runtime-tripwire-advisory.sh).
    """
    if not session_id:
        return ""

    # --- Off by default (PM ruling, 2026-08-18) ---
    # This tripwire measures WALL CLOCK, not context, and it emits wrap-shape
    # text — "stop starting new work ... write a successor-handoff stub;
    # return". On a machine where a dispatch spends most of its minutes in
    # spawn tax rather than work, 25 elapsed minutes buys very little progress,
    # so the wrap prescription lands while the window is barely touched. Agents
    # received it on the same advisory channel as the context-pressure text and
    # read it as context pressure — the observed symptom was subagents wrapping
    # up at 15-20% of window "because the hook said to".
    #
    # The PM's floor is that nothing prescribes a checkpoint below 40% of
    # context. A wall-clock trigger cannot honour a context floor, so it is
    # opt-in rather than re-tuned: no minute value makes elapsed time a proxy
    # for occupancy. Set COORDINATOR_RUNTIME_TRIPWIRE=1 to re-arm it (the
    # RUNTIME_TRIPWIRE_*_MIN thresholds still apply when armed) — the
    # compaction-decay effect it was built for is real, and the mechanism is
    # kept whole for the day it is measured against context rather than time.
    if os.environ.get("COORDINATOR_RUNTIME_TRIPWIRE", "") != "1":
        return ""

    # --- Git root ---
    try:
        from coordinator_core.git import repo_root as _repo_root_seam

        git_root = _repo_root_seam.show_toplevel() or ""
    except Exception:
        return ""  # git absent/erroring (e.g. not a repo) -- no subagent context to report

    if not git_root:
        return ""

    agents_dir = os.path.join(git_root, ".git", "coordinator-sessions", ".agents")
    if not os.path.isdir(agents_dir):
        return ""

    # --- Subagent detection (primary path: SESSION_ID dir) ---
    own_agent_id = ""
    if os.path.isdir(os.path.join(agents_dir, session_id)):
        own_agent_id = session_id

    # --- Resolver-based fallback for named teammates (additive, not replacement) ---
    # Mirrors runtime-tripwire-advisory.sh.
    if not own_agent_id and agent_id:
        canonical = _resolve_subagent_identity(agent_id, session_id)
        if canonical and os.path.isdir(os.path.join(agents_dir, canonical)):
            own_agent_id = canonical

    if not own_agent_id:
        return ""

    # --- EM session id from back-pointer ---
    em_sid_file = os.path.join(agents_dir, own_agent_id, "em-session-id.txt")
    if not os.path.isfile(em_sid_file):
        return ""

    try:
        with open(em_sid_file, encoding="utf-8") as fh:
            em_sid = fh.readline().strip()
    except Exception:
        return ""  # back-pointer unreadable -- no EM session to correlate against

    if not em_sid:
        return ""

    # --- Read dispatch record from EM's dispatched-agents.txt ---
    # Record shape: agentId\tmodel\tsubagent_type\tdispatched-at
    dispatch_file = os.path.join(
        git_root, ".git", "coordinator-sessions", em_sid, "dispatched-agents.txt"
    )
    if not os.path.isfile(dispatch_file):
        return ""

    own_row = ""
    try:
        with open(dispatch_file, encoding="utf-8") as fh:
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if parts and parts[0] == own_agent_id:
                    own_row = line.rstrip("\n")
                    break
    except Exception:
        return ""  # dispatch record unreadable -- no dispatch context to report

    if not own_row:
        return ""

    parts = own_row.split("\t")
    model = parts[1] if len(parts) > 1 else ""
    dispatched_at_str = parts[3] if len(parts) > 3 else ""

    # Backward-compat: legacy 3-col records have no col 4 — skip timing check.
    if not dispatched_at_str or not dispatched_at_str.isdigit():
        return ""

    dispatched_at = int(dispatched_at_str)
    if dispatched_at == 0:
        return ""

    # --- Compute elapsed minutes ---
    now = int(time.time())
    elapsed_sec = now - dispatched_at
    elapsed_min = elapsed_sec // 60

    # --- Threshold check ---
    threshold_min = _runtime_threshold_minutes(model)
    if elapsed_min < threshold_min:
        return ""

    # --- Bark-once guard (durable, keyed on firing session_id) ---
    # B-F1 had re-plumbed this to an in-memory set to keep this op COMPUTE_ONLY;
    # that marker never survived the fresh-process-per-fire execution model (see
    # the module-level state-management comment above _advisory_state_path), so
    # the tripwire never actually suppressed a repeat firing. Restored to a
    # durable file — a plain touch-file sentinel (not the JSON state used by the
    # context-pressure check) since this guard is a single boolean, and using a
    # SEPARATE file from the context-pressure state avoids a lost-update race
    # between the two checks, which run concurrently in the same process via
    # asyncio.gather + asyncio.to_thread (see the op handler below).
    # Review: code-reviewer (B-F3) — use tempfile.gettempdir(); /tmp/ absent on Windows.
    tmpdir = _tempfile().gettempdir()
    rt_bark_sentinel = os.path.join(tmpdir, f"rt-bark-once-{session_id}")
    if os.path.isfile(rt_bark_sentinel):
        return ""
    try:
        with open(rt_bark_sentinel, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(str(int(time.time())))
    except Exception:
        # Write failed -- fail open toward "fires again next call" rather than
        # silently losing the tripwire forever.
        pass

    # --- Autonomous-run detection (session-wins key via the resolve_mode seam) ---
    autonomous = resolve_mode("autonomous", em_sid)

    # Review: code-reviewer (B-F1) — fire-log append (state/runtime-tripwire-fire-log.tsv)
    #   dropped entirely. Calibration evidence now captured via the durable
    #   rt-bark-once-{session_id} sentinel above (touch-once, not an append log).

    # --- Emit WRAP-SHAPE prescription ---
    # CANONICAL TEXT — DO NOT REWORD without updating docs/wiki/runtime-tripwire.md §3.
    # AC5 grep targets embedded verbatim: "stop starting new work",
    # "persist any partial state to disk", "write a successor-handoff stub", "return".
    if autonomous:
        return (
            f"RUNTIME TRIPWIRE — you've been running ~{elapsed_min} minutes"
            f" (past the ~{threshold_min} min runtime tripwire for {model})."
            f" Past this point, dispatches commonly enter compaction-decay —"
            f" running redundant tests, looking for more things to check,"
            f" oscillating between approaches. Autonomous run active."
            f" Trust-but-verify with the EM as authority: form your own judgment,"
            f" but assume the EM will evaluate it."
            f" Wrap shape (the default): stop starting new work;"
            f" persist any partial state to disk now;"
            f" write a successor-handoff stub naming what's left; return."
            f" If you judge yourself genuinely close to a clean return (≤2-3 min):"
            f" say so explicitly in your return so the EM can decide whether to wait."
        )
    else:
        return (
            f"RUNTIME TRIPWIRE — you've been running ~{elapsed_min} minutes"
            f" (past the ~{threshold_min} min runtime tripwire for {model})."
            f" Past this point, dispatches commonly enter compaction-decay —"
            f" running redundant tests, looking for more things to check,"
            f" oscillating between approaches."
            f" Trust-but-verify with the EM as authority: form your own judgment,"
            f" but assume the EM will evaluate it."
            f" Wrap shape (the default): stop starting new work;"
            f" persist any partial state to disk now;"
            f" write a successor-handoff stub naming what's left; return."
            f" If you judge yourself genuinely close to a clean return (≤2-3 min):"
            f" say so explicitly in your return so the EM can decide whether to wait."
        )


# ---------------------------------------------------------------------------
# _check_first_agent_dispatch_sync
#
# New (no bash-era equivalent): a one-time-per-session advisory telling the
# dispatching EM that coordinator-themed subagents write their full findings
# to an on-disk sidecar as part of their design — a fact the EM would
# otherwise only learn by chance (a manual `ls` of state/subagent-share/)
# after concluding a dispatched agent's work was lost when its return
# message was merely lost or truncated.
#
# Gate is tool_name == "Agent", not a subagent_type prefix check: the
# dispatch-time PostToolUse payload this op receives carries no
# subagent_type field (see the DoE stub's stdin→params mapping — only
# session_id / transcript_path / agent_id / tool_name reach this op), and
# adding one would require editing hooks.json's `input:` row, which is out
# of scope here. This fires once on the first Agent-tool dispatch of ANY
# type per session — a deliberate widening, not a narrower coordinator-only
# trigger.
# ---------------------------------------------------------------------------


def _first_agent_dispatch_sentinel_path(tmpdir: str, session_id: str) -> str:
    return os.path.join(tmpdir, f"first-agent-dispatch-advisory-{session_id}")


def _check_first_agent_dispatch_sync(session_id: str, tool_name: str) -> str:
    """One-time-per-session advisory: coordinator-themed (and other) subagents
    write their full findings to an on-disk sidecar, not only their return
    message. Fires once, on the first Agent-tool PostToolUse of a session.

    Returns non-empty advisory text when it fires; "" on every early-exit path
    (non-Agent tool call, sentinel already present/written, or session_id
    absent). Never raises — fail-open on all I/O errors, same posture as the
    other two checks in this module (see the durable-state comment above
    _advisory_state_path).
    """
    if not session_id or tool_name != "Agent":
        return ""

    # Review: code-reviewer (B-F3) — use tempfile.gettempdir(); /tmp/ absent on Windows.
    tmpdir = _tempfile().gettempdir()
    sentinel = _first_agent_dispatch_sentinel_path(tmpdir, session_id)
    if os.path.isfile(sentinel):
        return ""

    return (
        "COORDINATOR SIDECAR ADVISORY: coordinator-themed subagents write their"
        " full findings to a sidecar file on disk as part of their design, not"
        " only in their return message to you."
        f" Sidecar directory for this session: state/subagent-share/{session_id}/"
        " If a dispatched agent's reply is missing, truncated, or it goes idle"
        " without reporting, read the sidecar there before assuming the work"
        " was lost or re-dispatching it."
    )


# ---------------------------------------------------------------------------
# _check_workflow_monitor_arm_sync
#
# New (no bash-era equivalent): a once-per-task advisory telling the
# dispatching EM the exact `Monitor(...)` call to paste to arm the watcher
# built in coordinator_core.workflow_watch (C1a/C1b) against a just-launched
# harness `Workflow` background run, so the EM stops hand-writing monitors
# (or worse, forgetting one and never learning the run ended).
#
# Gate is tool_name == "Workflow" — PostToolUse fires immediately after the
# tool returns its async-launch result, so the launch record this check looks
# for at the transcript tail IS the launch that just happened (see the plan's
# Evidence section, pln 2026-08-30-the-workflow-monitor-outlives-the-run-it-
# watches). taskId/runId/transcriptDir are read from the transcript tail via
# coordinator_core.workflow_watch.tail.TailReader (the same reader C1a
# builds), never from an unmapped `params` field — `_handler` receives
# exactly session_id, transcript_path, agent_id, tool_name, file_path,
# content, and this check reads no field beyond those six.
# ---------------------------------------------------------------------------

# Matches the async-launch result record's field order verbatim as observed
# in the plan's evidence transcript: status, taskId, taskType, runId,
# transcriptDir. Deliberately field-order-sensitive (not a JSON parse of an
# arbitrary object) — the record is embedded inside a larger transcript line
# that is not itself valid standalone JSON, the same reason terminal.py's
# _TASK_NOTIFICATION_RE matches by regex rather than by json.loads.
_ASYNC_LAUNCH_RE = re.compile(
    r'"status"\s*:\s*"async_launched"'
    r'[^{}]*?"taskId"\s*:\s*"(?P<task_id>[^"]*)"'
    r'[^{}]*?"taskType"\s*:\s*"(?P<task_type>[^"]*)"'
    r'[^{}]*?"runId"\s*:\s*"(?P<run_id>[^"]*)"'
    r'[^{}]*?"transcriptDir"\s*:\s*"(?P<transcript_dir>[^"]*)"'
)


def _workflow_monitor_sentinel_path(tmpdir: str, session_id: str, task_id: str) -> str:
    return os.path.join(tmpdir, f"workflow-monitor-armed-{session_id}-{task_id}")


def _workflow_watch_launcher() -> str | None:
    """Absolute path to the installed `workflow-watch` launcher, or None.

    Windows installs one native launcher image per generator-known name as
    `<name>.exe`; POSIX installs the bare extensionless name. Both are probed
    regardless of host, because a settings home synced between a Mac and a
    Windows box carries both images and only one of them is the runnable one
    here -- probing on-disk existence rather than on `os.name` is what makes
    this correct on whichever platform is actually running.

    Returns None when neither is present. The caller emits NOTHING in that
    case: a command naming a launcher that is not installed fails
    command-not-found, which reads to an EM as "this watcher does not exist"
    rather than "reinstall the settings home".
    """
    try:
        from coordinator_core._settings_home import settings_home

        bin_dir = settings_home() / "bin"
    except Exception:
        return None
    for candidate in (bin_dir / "workflow-watch.exe", bin_dir / "workflow-watch"):
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def _check_workflow_monitor_arm_sync(session_id: str, transcript_path: str, tool_name: str) -> str:
    """One-time-per-task advisory: names the exact `Monitor(...)` call to arm
    against a just-launched harness `Workflow` background run.

    Returns non-empty advisory text when it fires; "" on every early-exit path
    (non-Workflow tool call, no async_launched/local_workflow record found
    near the transcript tail, sentinel already present/written, or the
    watcher's wall-clock cap constant is unavailable). Never raises —
    fail-open on all I/O errors, same posture as the other four checks in
    this module (see the durable-state comment above _advisory_state_path).

    Does NOT key on the `wf_` run id (see the plan's Anti-scope) — the
    sentinel and the Monitor call's watcher argv are both keyed on the task
    id; runId is read only to help derive the journal path to render.
    """
    if not session_id or tool_name != "Workflow" or not transcript_path:
        return ""

    try:
        from coordinator_core.workflow_watch.tail import TailReader

        # Review: overengineering-reviewer (F1) — a fresh TailReader starts
        # at offset 0, so an unseeded construction here reads the ENTIRE
        # session transcript on every Workflow PostToolUse event. This call
        # site takes exactly one snapshot (no repeated polling), so
        # seek_to_tail bounds the read to the trailing window instead.
        text = TailReader(transcript_path, seek_to_tail=True).poll()
    except Exception:
        return ""
    if not text:
        return ""

    # Last match wins: PostToolUse fires immediately after the tool returns,
    # so the most recent async_launched record in the tail IS this launch.
    match = None
    for candidate in _ASYNC_LAUNCH_RE.finditer(text):
        match = candidate
    if match is None:
        # Breadcrumb, not silence. Every other failure path in this module
        # surfaces through _text_or_breadcrumb; a regex that stopped matching
        # because the harness reordered or nested these fields would otherwise be
        # indistinguishable from "no Workflow was launched" -- forever, with no
        # signal. (Review: code-reviewer slice 2.)
        print(
            "postuse_advisory_dispatch: workflow_monitor_arm found no "
            "async_launched record in the transcript tail -- if a Workflow did "
            "launch, _ASYNC_LAUNCH_RE no longer matches the harness record shape",
            file=sys.stderr,
        )
        return ""
    if match.group("task_type") != "local_workflow":
        # "Last match wins" assumes the most recent async_launched record is this
        # tool call's own launch. A concurrent background dispatch of another
        # taskType landing later in the same tail window would shadow it, and the
        # real launch sits moments earlier, unseen. Name it rather than returning
        # empty as though nothing happened.
        print(
            "postuse_advisory_dispatch: workflow_monitor_arm saw taskType="
            f"{match.group('task_type')!r}"
            " nearest the tail, not local_workflow -- a concurrent dispatch may "
            "have shadowed this Workflow's own launch record",
            file=sys.stderr,
        )
        return ""

    task_id = match.group("task_id")
    run_id = match.group("run_id")
    # The regex captures a JSON STRING LITERAL out of the raw transcript text,
    # so a Windows path arrives with its separators still escaped
    # (C:\Users\... as two characters each). Feeding that to os.path.join
    # yields a mixed-separator path that is wrong even where it happens to
    # resolve. Decode it back through the JSON rules that escaped it.
    try:
        transcript_dir = json.loads('"' + match.group("transcript_dir") + '"')
    except Exception:
        return ""
    if not task_id or not run_id or not transcript_dir:
        return ""

    # Review: code-reviewer (B-F3) — use tempfile.gettempdir(); /tmp/ absent on Windows.
    tmpdir = _tempfile().gettempdir()
    sentinel = _workflow_monitor_sentinel_path(tmpdir, session_id, task_id)
    if os.path.isfile(sentinel):
        return ""

    # The watcher's own wall-clock cap default (coordinator_core.workflow_watch,
    # C1b) is the single source of truth for this number — the emitted
    # timeout_ms below MUST be the same number as workflow_watch's own --cap
    # default, derived once in one place, so the two cannot drift apart.
    # Imported here, not at module scope. Unconditional -- there is no
    # ImportError branch to fossilize a chunk boundary (review:
    # overengineering-reviewer #4) -- but function-local, because this is a
    # PostToolUse hook: a module-scope import is paid on EVERY tool call in
    # every session, while this constant is read only when tool_name ==
    # "Workflow". Measured at 7.8ms cumulative (python -X importtime), most
    # of it render.py pulling json. Same discipline as _tempfile above.
    from coordinator_core.workflow_watch import DEFAULT_CAP_MS, DEFAULT_CAP_SECONDS

    cap_ms = DEFAULT_CAP_MS
    cap_seconds = DEFAULT_CAP_SECONDS

    # `transcriptDir` as the harness emits it ALREADY ends in the run id
    # (observed: .../subagents/workflows/wf_<id>). Appending run_id again
    # yields .../wf_<id>/wf_<id>/journal.jsonl, a path that never exists —
    # the watcher would then render nothing at all. Append only when the
    # directory does not already name the run, so both shapes resolve.
    # Case-insensitive: Windows filesystems are case-preserving but
    # case-insensitive, so a segment differing only in case is the SAME
    # directory. A case-sensitive compare there would append run_id a second
    # time and name a path that never exists -- the exact failure this
    # conditional exists to prevent. (Review: code-reviewer slice 2.)
    if os.path.basename(transcript_dir.rstrip("/\\")).lower() == run_id.lower():
        journal_path = os.path.join(transcript_dir, "journal.jsonl")
    else:
        journal_path = os.path.join(transcript_dir, run_id, "journal.jsonl")
    # Quote every interpolated path. These are Windows paths on this box
    # (C:\\Users\\...), and a POSIX shell eats the backslashes -- the
    # command then names a path that does not exist, TailReader swallows the
    # OSError, and the watcher polls a file it can never read for the FULL cap
    # before exiting 1. That is silent, and it is the exact "outlives the run"
    # failure this check exists to remove. A path containing a space breaks the
    # unquoted form in any shell, on any host.
    # The watcher is named by the ABSOLUTE settings-home launcher path, never
    # as `python3 -m coordinator_core.workflow_watch`. The bare `-m` form
    # resolves only where `coordinator_core` is already importable -- the
    # engine's own environment, which is where THIS hook runs, which is
    # precisely why the emitted command's failure was invisible to the code
    # emitting it. In a consumer repo the EM pasted it and got
    # `ModuleNotFoundError: No module named 'coordinator_core'`, exit 1, after
    # the advisory's imperative wording had already talked them out of their own
    # monitor -- and a dead watcher and a quiet run look identical. The launcher
    # (coordinator/bin/workflow-watch.py, forwarded into <settings-home>/bin/)
    # self-resolves the engine, so the command runs from any repo, any cwd.
    # Absolute-path-through-the-launcher is the one sanctioned resolution
    # (DoE-claude coordinator/snippets/resolve-coordinator-bin.md).
    # cross-repo/inbox/2026-08-30-doe-claude-em-workflow-watch-command-is-unrunnable-outside-the-engine.md
    watcher_path = _workflow_watch_launcher()
    if watcher_path is None:
        # Silence, not a command naming a launcher that is not on disk. An
        # uninstalled/partially-migrated settings home would otherwise turn one
        # broken command into another, and the EM cannot tell the two apart.
        # Same posture as the unquotable-path branch below.
        print(
            "postuse_advisory_dispatch: workflow_monitor_arm found no "
            "workflow-watch launcher under the settings home -- staying silent "
            "rather than emitting a command that cannot run. Reinstall via "
            "scripts/setup.py to provision it.",
            file=sys.stderr,
        )
        return ""

    formatted = [_portable_arg(v) for v in (watcher_path, transcript_path, journal_path, task_id)]
    if any(arg is None for arg in formatted):
        print(
            "postuse_advisory_dispatch: workflow_monitor_arm cannot emit a "
            "shell-safe command for these paths -- staying silent rather than "
            "emitting one that would tokenize differently per shell",
            file=sys.stderr,
        )
        return ""
    q_watcher, q_transcript, q_journal, q_task = formatted
    monitor_command = (
        f"{q_watcher}"
        f" --transcript {q_transcript}"
        f" --journal {q_journal}"
        f" --task-id {q_task}"
        " --poll-interval 1"
        f" --cap {cap_seconds}"
    )

    # Sentinel LAST, after the advisory is fully composed. Written before
    # composition it is a point of no return: anything raising after it -- the
    # import, the path arithmetic, the quoting -- would leave the sentinel on
    # disk while the caller got nothing, and every later Workflow PostToolUse
    # for the same task id would then short-circuit on os.path.isfile() and stay
    # silent forever. The handler's return_exceptions=True makes that failure
    # invisible, so the suppression would be permanent AND undiagnosed.
    # _check_first_agent_dispatch_sync can write early because only a static
    # string follows it; this leg cannot. (Review: code-reviewer slice 2, P1.)
    try:
        with open(sentinel, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(str(int(time.time())))
    except Exception:
        # Sentinel unwritable — fail open toward silence this call rather than
        # raising or emitting an advisory whose one-time firing can't be
        # recorded. Review: code-reviewer (Finding 3) — if open() succeeded
        # but write() raised mid-write (e.g. disk full), the sentinel file
        # already exists on disk, and every later call in this session would
        # see it and stay silent forever. Best-effort remove it (swallow any
        # error from the remove itself, keeping this path fail-open) so a
        # later Agent dispatch in the same session can retry the write and
        # actually fire once.
        try:
            os.remove(sentinel)
        except Exception:
            pass
        return ""

    return (
        "WORKFLOW MONITOR: this Workflow run is watched by a background hook"
        " check, not by you — arm the watcher instead of hand-writing one."
        f' Paste: Monitor(command="{monitor_command}", timeout_ms={cap_ms},'
        " persistent=false). The watcher enforces its own wall-clock cap"
        " independent of this timeout_ms and exits on its own once the run"
        " reaches a terminal state."
    )


# ---------------------------------------------------------------------------
# _check_group_em_watch_arm_sync
#
# New (no bash-era equivalent): once-per-session advisory that arms
# `coordinator_core.group_em.watch` (C2, docs/plans/2026-08-31-the-group-em-
# tick-carries-standing-obligations.md) for a session that holds the Group EM
# crown for its repo and has never armed that watch -- the population C2's
# own docstring names as undischarged: "a crowned Group EM that armed nothing
# and then stopped ticking". Modelled directly on
# _check_workflow_monitor_arm_sync (same sentinel-guarded, fail-open-to-
# silence contract, same _portable_arg quoting reuse) per this chunk's own
# spec (plan § C10) rather than a second composition path.
#
# CROWN CHECK is a real, current fact: `group_em.nomination.read_record`
# read fresh against this tool call, joined on this session's own id -- never
# cached, never inferred from a prior tick.
#
# NEVER-ARMED DETECTION IS A NAMED, BOUNDED STOPGAP, NOT THE C10 SPEC'S FULL
# ASK. The C10 brief's late-added constraint is explicit that "is a watch
# armed" is the wrong question -- a record of an arming event is a record of
# the last boundary, never of now, and only a COUNT of live subscriptions or
# a standing poller answers "is it live right now". That signal does not
# exist in this repo: it is doe-claude-41's C3 (their plan), scoped out here
# per the C10 brief's "we own the PUSH, they own the RECORD" split, and
# building a second one here would be the duplication that split exists to
# prevent. So this leg answers the narrower, honestly-answerable question in
# C10's own title -- "a crowned session that never armed it" -- by scanning
# THIS session's own transcript for the watch's own one-time `ARMED` line
# (see `coordinator_core.group_em.watch.main`) and staying silent the moment
# it has appeared even once. It cannot and does not claim to catch a watch
# that armed and later died with its session; that gap is the same one C2's
# own docstring names as undischarged by any chunk in this spine.
#
# LAUNCHER GAP, NAMED RATHER THAN WORKED AROUND: unlike workflow-watch, no
# settings-home launcher for `coordinator_core.group_em.watch` exists yet
# (no `group-em-watch(.exe)` under any `<settings-home>/bin/`), and
# `watch.py` itself ships no `argparse`/`__main__` CLI surface to invoke --
# only the importable `main(repo_root, ...)` function. Building either is a
# generator/launcher-chain change, outside this chunk's `writes:` scope
# (coordinator_core/hooks/postuse_advisory_dispatch.py and its test only).
# `_group_em_watch_launcher` therefore always resolves to `None` today, which
# is the correct fail-open-to-silence outcome per `_portable_arg`'s own
# doctrine: a command naming a launcher/entrypoint that cannot run is worse
# than silence. Reported as the concrete follow-up in this chunk's own report
# rather than invented here.
# ---------------------------------------------------------------------------

#: The exact prefix `coordinator_core.group_em.watch.main` prints as its
#: first stdout line on arming (see that module's `main`, `emit(f"ARMED
#: denominator=...")`). Matched as a plain substring, not a regex -- this
#: leg only needs to know the line occurred somewhere in the transcript, not
#: parse its fields.
_GROUP_EM_WATCH_ARMED_MARKER = "ARMED denominator="


def _group_em_watch_arm_sentinel_path(tmpdir: str, session_id: str) -> str:
    return os.path.join(tmpdir, f"group-em-watch-arm-advisory-{session_id}")


def _group_em_watch_launcher() -> str | None:
    """Absolute path to an installed `group-em-watch` launcher, or None.

    Mirrors `_workflow_watch_launcher` exactly (same `.exe`/bare-name probe
    under `<settings-home>/bin/`, same reasoning for probing on-disk
    existence rather than `os.name`). Returns None today -- no such launcher
    has been generated yet (see the module-level comment above this
    function's call site) -- and the caller emits NOTHING in that case,
    never a command naming a launcher that is not installed.
    """
    try:
        from coordinator_core._settings_home import settings_home

        bin_dir = settings_home() / "bin"
    except Exception:
        return None
    for candidate in (bin_dir / "group-em-watch.exe", bin_dir / "group-em-watch"):
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def _check_group_em_watch_arm_sync(session_id: str, transcript_path: str) -> str:
    """Once-per-session advisory: arm `group_em.watch` for a crowned session
    that has never armed it. Returns non-empty advisory text when it fires;
    "" on every early-exit path (no session_id, sentinel already written, no
    git root, no/foreign crown record, transcript unreadable, the watch's own
    ARMED marker already present, or no installed launcher). Never raises —
    fail-open on all I/O errors, same posture as the other checks in this
    module (see the durable-state comment above _advisory_state_path).
    """
    if not session_id:
        return ""

    tmpdir = _tempfile().gettempdir()
    sentinel = _group_em_watch_arm_sentinel_path(tmpdir, session_id)
    if os.path.isfile(sentinel):
        return ""

    try:
        from coordinator_core.git import repo_root as _repo_root_seam

        git_root = _repo_root_seam.show_toplevel() or ""
    except Exception:
        return ""  # git absent/erroring -- no repo to check a crown against
    if not git_root:
        return ""

    try:
        from coordinator_core.group_em import nomination as _group_em_nomination

        record = _group_em_nomination.read_record(git_root)
    except Exception:
        return ""
    if not isinstance(record, dict) or record.get("session_id") != session_id:
        return ""  # not the crown holder for this repo -- nothing to arm

    if not transcript_path or not os.path.isfile(transcript_path):
        return ""  # cannot establish never-armed -- fail toward silence
    try:
        with open(transcript_path, encoding="utf-8", errors="ignore") as fh:
            transcript_text = fh.read()
    except Exception:
        return ""
    if _GROUP_EM_WATCH_ARMED_MARKER in transcript_text:
        return ""  # armed at least once this session -- see module comment
        # on the named decay gap this leg does not attempt to close.

    watcher_path = _group_em_watch_launcher()
    if watcher_path is None:
        print(
            "postuse_advisory_dispatch: group_em_watch_arm found no "
            "group-em-watch launcher under the settings home -- staying "
            "silent rather than emitting a command that cannot run.",
            file=sys.stderr,
        )
        return ""

    formatted = [_portable_arg(v) for v in (watcher_path, git_root)]
    if any(arg is None for arg in formatted):
        print(
            "postuse_advisory_dispatch: group_em_watch_arm cannot emit a "
            "shell-safe command for these paths -- staying silent rather "
            "than emitting one that would tokenize differently per shell",
            file=sys.stderr,
        )
        return ""
    q_watcher, q_repo_root = formatted
    monitor_command = f"{q_watcher} --repo-root {q_repo_root}"

    # Sentinel LAST, after the advisory is fully composed -- same reasoning
    # as _check_workflow_monitor_arm_sync's own sentinel placement: anything
    # raising after an early sentinel write would strand this leg silent
    # forever for the rest of the session.
    try:
        with open(sentinel, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(str(int(time.time())))
    except Exception:
        try:
            os.remove(sentinel)
        except Exception:
            pass
        return ""

    return (
        "GROUP EM WATCH: this session holds the Group EM crown for this repo"
        " and no watch on the standing peer registry has been armed this"
        " session -- arm it now rather than depending on a tick you remember"
        " to re-run."
        f' Paste: Monitor(command="{monitor_command}", persistent=true).'
        " Armed persistent, the watch runs for the life of this session and"
        " never needs re-arming."
    )


# ---------------------------------------------------------------------------
# Failure isolation for the fold
# ---------------------------------------------------------------------------


def _text_or_breadcrumb(label: str, result) -> str:
    """One leg's advisory text, or "" when that leg raised — never a re-raise.

    The disposition is fail-open toward silence for the failing leg ONLY. Every
    caller of this function has already collected its siblings' results, so a
    raise here would convert one leg's transient read failure into a suppressed
    advisory for all of them — the exact property the four separate hook
    processes this op replaced could not lose.

    The breadcrumb names the leg. Without it the drop is indistinguishable from
    a leg that ran and had nothing to say, which for an advisory is silent by
    construction: there is no artifact whose absence anyone would notice.
    """
    if isinstance(result, BaseException):
        print(
            "postuse_advisory_dispatch: leg=%s raised %s: %s — its sibling legs are "
            "unaffected" % (label, type(result).__name__, result),
            file=sys.stderr,
        )
        return ""
    return result or ""


async def _leg_text(label: str, coro) -> str:
    """Await one leg alone, with the same fail-open disposition as the gather path.

    The `session_id`-absent short-circuit above awaits a single leg with no
    siblings to protect, but an advisory hook that raises still fails the whole
    hook rather than passing quietly — so it gets the same treatment as a leg
    inside the gather, and for the same reason.
    """
    try:
        return (await coro) or ""
    except BaseException as exc:  # noqa: BLE001 — advisory hooks fail open; see docstring
        return _text_or_breadcrumb(label, exc)


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


@register_op("hooks.postuse_advisory_dispatch")
async def _handler(params: dict, repo_root=None) -> dict:
    """PostToolUse advisory dispatcher: folds context-pressure + runtime-tripwire +
    the first-Agent-dispatch sidecar advisory + the unauthorized-handoff nudge +
    the workflow-monitor arming advisory + the Group EM watch arming advisory.

    Context-pressure, runtime-tripwire, and the Group EM watch arming check fire on
    ALL PostToolUse events (no tool_name gate — all three are universal). The
    first-Agent-dispatch advisory only fires the first time tool_name == "Agent" in a
    session — its own internal gate plus a durable once-per-session sentinel (see
    _check_first_agent_dispatch_sync), not a handler-level tool_name gate applied to
    the other three. The unauthorized-handoff nudge gates internally on
    tool_name == "Write" plus a handoff/spinoff file_path, the same narrowing shape.
    The workflow-monitor arming advisory gates internally on tool_name == "Workflow"
    plus a durable once-per-task sentinel (see _check_workflow_monitor_arm_sync), the
    same narrowing shape again. Merges whichever fire (blank-line separator,
    cp/rt/first-agent-dispatch/unauthorized-handoff/workflow-monitor-arm/
    group-em-watch-arm order), or returns no_advisory() when none fire.

    Merge contract (mirrors postuse-advisory-dispatch.sh, extended for the third
    through sixth checks):
        N of 6 fire → post_advisory("\\n\\n".join of the N non-empty texts, in
                       cp/rt/first-agent-dispatch/unauthorized-handoff/
                       workflow-monitor-arm/group-em-watch-arm order)
        none fire   → no_advisory()

    Folding the fourth check in retires DoE's separate PostToolUse(Write)
    registration for nudge-unauthorized-handoff.py — one interpreter start per
    Write instead of two (cross-repo/inbox/2026-08-06-doe-claude-em-postuse-fold-
    nudge-unauthorized-handoff.md). It requires DoE's dispatcher stub to map
    tool_input.file_path and tool_input.content into params; absent those, the
    fourth check stays silent and the other three are unaffected.

    Negative-spec:
        Context-pressure, runtime-tripwire, and the Group EM watch arming check DO
        NOT gate on tool_name — PostToolUse fires on every tool and all three checks
        are universal (not tool-name-scoped). The first-Agent-dispatch,
        unauthorized-handoff, and workflow-monitor-arm advisories DO gate on
        tool_name internally ("Agent", "Write", and "Workflow" respectively) — their
        own internal early-exits, not handler-level gates applied to the universal
        three.
        DOES NOT gate the unauthorized-handoff nudge on session_id — its
        predicate is the Write payload alone, so it runs ahead of the
        session-scoped short-circuit rather than being swallowed by it.
        DOES NOT block execution — PostToolUse is advisory only.
        DOES NOT arm the Monitor call itself — the workflow-monitor-arm and
        group-em-watch-arm checks only name the call for the EM to paste; neither
        ever dispatches, spawns, or writes into the shared
        advisory-hook-state-{session_id}.json (each's once-per-task/once-per-session
        sentinel is its own disjoint file, matching the first-Agent-dispatch and
        runtime-tripwire checks' own disjoint sentinels — see the module-level
        state-management comment above _advisory_state_path for why sharing that
        file across concurrent legs is the regression this avoids).
        DOES NOT detect whether the Group EM watch is CURRENTLY live — only
        whether it has never been armed this session (see the module-level comment
        above _check_group_em_watch_arm_sync for the named, cross-plane-scoped gap
        this leaves open).
        WRITES durable per-session dedup/throttle state to tempdir (see the
        module-level state-management comment above _advisory_state_path) —
        this op is classified MUTATING, not COMPUTE_ONLY (reversing the B-F1
        in-memory re-plumb, which never survived the fresh-process-per-fire
        execution model). See coordinator_core/authz/classification.py.

    Spec backlink: pln-pcore-04-advisory-hook-ops-mak-b219a8 § C7,
    docs/plans/2026-08-31-the-group-em-tick-carries-standing-obligations.md § C10
    """
    # asyncio deferred to first use here (not module scope) — this is the only function
    # in the module touching the asyncio namespace at runtime. Spec:
    # docs/plans/2026-07-24-canonical-resolution-engine.md task W0-1.
    import asyncio

    session_id = field(params, "session_id")
    transcript_path = field(params, "transcript_path")
    agent_id = field(params, "agent_id")
    tool_name = field(params, "tool_name")
    file_path = field(params, "file_path")
    content = field(params, "content")

    # The unauthorized-handoff nudge is the one check that does NOT depend on
    # session_id — its predicate is the Write payload alone — so it runs even
    # when session_id is absent, ahead of the session-scoped short-circuit.
    # Built as a plain coroutine object here (not yet awaited/scheduled) so it
    # can be folded into the same asyncio.gather as the other three below when
    # session_id is present — genuinely concurrent, not stacked ahead of them.
    # Review: code-reviewer (P2) — a prior sequential `await` here before
    # asyncio.gather made total latency uh_text-time + gather-time instead of
    # max(all four), contradicting this module's own docstring. No ordering
    # dependency exists between this check and the other three (confirmed: it
    # reads only its own Write-tool params/transcript tail via regex, no
    # shared mutable state or sentinel namespace with cp/rt/first-agent-dispatch).
    uh_coro = nudge_unauthorized_handoff.advisory_text(
        tool_name, file_path, content, transcript_path
    )

    # The other three fail-open when session_id is absent, but short-circuit
    # early here to skip asyncio.to_thread overhead when there's nothing to do.
    if not session_id:
        uh_text = await _leg_text("unauthorized_handoff", uh_coro)
        return post_advisory(uh_text) if uh_text else no_advisory()

    # Run all five checks concurrently — they use disjoint sentinel namespaces.
    #
    # `return_exceptions=True` IS THE FAILURE-ISOLATION BUY-BACK, and it is not the
    # same concern as the ordering/latency argument above. This fold replaced FOUR
    # SEPARATE HOOK PROCESSES, and a process boundary isolates a crash for free: one
    # raising script could not suppress the other three's advisories. A bare `gather`
    # gives that away silently — it propagates the first exception and abandons its
    # siblings' results, so a single unreadable transcript or sentinel takes down all
    # five legs at once. All five read transcripts and sentinel files off a shared
    # disk on a box running ~50 concurrent sessions, so a transient read failure is
    # the expected case, not the exotic one.
    #
    # Found by doe-claude-1d while carrying this property into their hook-transport
    # plan, after `agent_postuse_dispatch` (the PostToolUse(Agent) fan-in) was built
    # with it. The concurrency reasoning above is correct and answers a different
    # question; failure isolation simply was not the axis. Every fan-in in this
    # package owes this buy-back, and it is per-fold — never inherited.
    results = await asyncio.gather(
        asyncio.to_thread(_check_context_pressure_sync, session_id, transcript_path),
        asyncio.to_thread(_check_runtime_tripwire_sync, session_id, agent_id),
        asyncio.to_thread(_check_first_agent_dispatch_sync, session_id, tool_name),
        uh_coro,
        asyncio.to_thread(
            _check_workflow_monitor_arm_sync, session_id, transcript_path, tool_name
        ),
        asyncio.to_thread(_check_group_em_watch_arm_sync, session_id, transcript_path),
        return_exceptions=True,
    )
    labels = (
        "context_pressure",
        "runtime_tripwire",
        "first_agent_dispatch",
        "unauthorized_handoff",
        "workflow_monitor_arm",
        "group_em_watch_arm",
    )
    cp_text, rt_text, ad_text, uh_text, wm_text, ge_text = (
        _text_or_breadcrumb(label, result) for label, result in zip(labels, results)
    )

    texts = [text for text in (cp_text, rt_text, ad_text, uh_text, wm_text, ge_text) if text]
    if texts:
        return post_advisory("\n\n".join(texts))
    return no_advisory()

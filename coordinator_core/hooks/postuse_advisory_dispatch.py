"""
coordinator_core.hooks.postuse_advisory_dispatch — PostToolUse advisory dispatcher op.

Purpose: Folds four PostToolUse advisory checks — context-pressure, runtime-tripwire,
a one-time first-Agent-dispatch sidecar advisory, and the unauthorized-handoff nudge —
into a single in-process op, eliminating bash.exe spawns per tool call on Windows.
Context-pressure and runtime-tripwire fire on ALL PostToolUse events (no tool_name
gate — both checks are universal); the first-Agent-dispatch advisory fires only on
tool_name == "Agent", and only once per session; the unauthorized-handoff nudge only
on tool_name == "Write" with a handoff/spinoff file_path. The latter two narrow
themselves internally — no handler-level tool_name gate is applied to the universal two.

The session-scoped checks run concurrently via asyncio.gather. Whichever fire have
their additionalContext texts merged with a blank-line separator into ONE
post_advisory() call (a PostToolUse hook must emit at most one JSON object). When none
fire, no_advisory() is returned.

Port of: postuse-advisory-dispatch.sh (DoE 2f8b8450, 2026-07-16). The first-Agent-
dispatch sidecar advisory has no bash-era equivalent — added directly here. The
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
    jq merge logic               → plain string concatenation + post_advisory()
    All four return str advisory text or "" — "" means "did not fire".

Spec backlink: pln-pcore-04-advisory-hook-ops-mak-b219a8 § C7
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
# _check_context_pressure_sync sits at 47% and not 50%: on a 1M window a flat
# 50% coincides EXACTLY with this ceiling (500_000 tokens), firing the warning
# level with the cut instead of ahead of it and defeating the whole point of a
# pre-emptive advisory — a handoff needs runway to compose before an
# involuntary, lossy auto-compaction lands. 47% leaves ~30K tokens of that
# runway. Referenced by comment rather than by arithmetic: the bands are
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
        the work cannot close in ~5% more) and 47% (red — handoff now, ahead of
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
    # which non-empty variant fires (see mode_resolution module docstring and
    # this function's own autonomous_run branches below, which already
    # implement the two variants this key selects between).
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
    # 47 — RED. "Go to handoff now, before compaction takes the choice away."
    #
    # Why 47 and not 50 on the 1M tier: auto-compaction fires at a fixed
    # ~500K tokens there (_AUTO_COMPACT_CEILING_TOKENS_1M), so a 50% trigger
    # coincides EXACTLY with the cut instead of landing ahead of it, and a
    # handoff needs runway to compose. 47% of 1M is ~470K — the last point
    # with enough headroom left to write one.
    #
    # NOTHING fires below 40. No checkpoint prompts, no "consider wrapping",
    # no informational heads-up at 15/20/25%. That is the PM ruling, stated as
    # a floor rather than a default: a check added here that fires under 40
    # violates it no matter how quiet its wording.
    if display_pct >= 47 and transcript_hash not in cp_state.get("critical_fired", []):
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
            return (
                f"CONTEXT PRESSURE — INFORMATIONAL: ~{display_pct}% of window"
                f" used{age_note}, measured from the harness's own context_window"
                f" block. Autonomous run: compaction from here is involuntary and"
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
        base = (
            f"CONTEXT PRESSURE — ADVISORY: ~{display_pct}% of window used{age_note},"
            f" measured from the harness's own context_window block."
            f" If the current work cannot close within about another 5% of window,"
            f" start moving toward /handoff. If it can, carry on — the hard call"
            f" comes at 47%."
        )
        if autonomous_run or compaction_warnings_variant == "informational":
            return (
                f"CONTEXT PRESSURE — INFORMATIONAL: ~{display_pct}% of window"
                f" used{age_note}, measured from the harness's own context_window"
                f" block. Autonomous run: checkpoint state to disk at the next"
                f" natural boundary so the run is resumable."
            )
        return base

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
        "COORDINATOR SIDECAR ADVISORY: coordinator-themed subagents write their"
        " full findings to a sidecar file on disk as part of their design, not"
        " only in their return message to you."
        f" Sidecar directory for this session: state/subagent-share/{session_id}/"
        " If a dispatched agent's reply is missing, truncated, or it goes idle"
        " without reporting, read the sidecar there before assuming the work"
        " was lost or re-dispatching it."
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
    the first-Agent-dispatch sidecar advisory + the unauthorized-handoff nudge.

    Context-pressure and runtime-tripwire fire on ALL PostToolUse events (no
    tool_name gate — both are universal). The first-Agent-dispatch advisory only
    fires the first time tool_name == "Agent" in a session — its own internal gate
    plus a durable once-per-session sentinel (see _check_first_agent_dispatch_sync),
    not a handler-level tool_name gate applied to the other two. The
    unauthorized-handoff nudge gates internally on tool_name == "Write" plus a
    handoff/spinoff file_path, the same narrowing shape. Merges whichever fire
    (blank-line separator, cp/rt/first-agent-dispatch/unauthorized-handoff order),
    or returns no_advisory() when none fire.

    Merge contract (mirrors postuse-advisory-dispatch.sh, extended for the third
    and fourth checks):
        N of 4 fire → post_advisory("\\n\\n".join of the N non-empty texts, in
                       cp/rt/first-agent-dispatch/unauthorized-handoff order)
        none fire   → no_advisory()

    Folding the fourth check in retires DoE's separate PostToolUse(Write)
    registration for nudge-unauthorized-handoff.py — one interpreter start per
    Write instead of two (cross-repo/inbox/2026-08-06-doe-claude-em-postuse-fold-
    nudge-unauthorized-handoff.md). It requires DoE's dispatcher stub to map
    tool_input.file_path and tool_input.content into params; absent those, the
    fourth check stays silent and the other three are unaffected.

    Negative-spec:
        Context-pressure and runtime-tripwire DO NOT gate on tool_name —
        PostToolUse fires on every tool and both checks are universal (not
        tool-name-scoped). The first-Agent-dispatch and unauthorized-handoff
        advisories DO gate on tool_name internally ("Agent" and "Write"
        respectively) — their own internal early-exits, not handler-level gates
        applied to the universal two.
        DOES NOT gate the unauthorized-handoff nudge on session_id — its
        predicate is the Write payload alone, so it runs ahead of the
        session-scoped short-circuit rather than being swallowed by it.
        DOES NOT block execution — PostToolUse is advisory only.
        WRITES durable per-session dedup/throttle state to tempdir (see the
        module-level state-management comment above _advisory_state_path) —
        this op is classified MUTATING, not COMPUTE_ONLY (reversing the B-F1
        in-memory re-plumb, which never survived the fresh-process-per-fire
        execution model). See coordinator_core/authz/classification.py.

    Spec backlink: pln-pcore-04-advisory-hook-ops-mak-b219a8 § C7
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

    # Run all four checks concurrently — they use disjoint sentinel namespaces.
    #
    # `return_exceptions=True` IS THE FAILURE-ISOLATION BUY-BACK, and it is not the
    # same concern as the ordering/latency argument above. This fold replaced FOUR
    # SEPARATE HOOK PROCESSES, and a process boundary isolates a crash for free: one
    # raising script could not suppress the other three's advisories. A bare `gather`
    # gives that away silently — it propagates the first exception and abandons its
    # siblings' results, so a single unreadable transcript or sentinel takes down all
    # four legs at once. All four read transcripts and sentinel files off a shared
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
        return_exceptions=True,
    )
    labels = (
        "context_pressure",
        "runtime_tripwire",
        "first_agent_dispatch",
        "unauthorized_handoff",
    )
    cp_text, rt_text, ad_text, uh_text = (
        _text_or_breadcrumb(label, result) for label, result in zip(labels, results)
    )

    texts = [text for text in (cp_text, rt_text, ad_text, uh_text) if text]
    if texts:
        return post_advisory("\n\n".join(texts))
    return no_advisory()

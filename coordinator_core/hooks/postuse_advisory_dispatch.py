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

Port of: postuse-advisory-dispatch.sh (coordinator-claude 2f8b8450, 2026-07-16). The first-Agent-
dispatch sidecar advisory has no bash-era equivalent — added directly here. The
unauthorized-handoff nudge is a fan-in of coordinator-claude's separate PostToolUse(Write)
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
import os
import re
import time
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.hooks._envelope import no_advisory, post_advisory
from coordinator_core.hooks._payload import field
from coordinator_core.hooks import nudge_unauthorized_handoff
from coordinator_core.session.autonomous_sentinel import sentinel_path


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

# ---------------------------------------------------------------------------
# Module-level compiled regex — model detection (context-pressure, Phase 2).
#
# Mirrors context-pressure-advisory.sh:
#   grep -m1 -oE '"model"[[:space:]]*:[[:space:]]*"claude-[^"]*"'
#   | grep -oE 'claude-[^"]*' | head -1
#
# The [^"]* character class is load-bearing: terminates at the closing quote
# AND guarantees model_id contains no embedded quote. Do not widen to .*.
# ---------------------------------------------------------------------------
_MODEL_RE = re.compile(r'"model"\s*:\s*"(claude-[^"]*)"')

# ---------------------------------------------------------------------------
# Anthropic-side fixed auto-compaction ceiling on the 1M-context tier.
#
# This is NOT one of our tunables — it is a fixed cost/attention ceiling
# Anthropic applies on the 1M window (observed 2026-07-13, unchanged since),
# decoupled from window size. Do not re-express the 1M-tier threshold as a
# percentage of context_window: a flat 50%-of-window critical threshold
# coincides EXACTLY with this ceiling on a 1M window (500_000 tokens),
# which fires the warning level with the cut instead of ahead of it and
# defeats the whole point of a pre-emptive advisory (a handoff needs
# runway to compose before an involuntary, lossy auto-compaction lands).
# Anchor the 1M tier to absolute tokens below this ceiling instead — see
# the tier-aware threshold block in _check_context_pressure_sync.
# ---------------------------------------------------------------------------
_AUTO_COMPACT_CEILING_TOKENS_1M = 500_000

# ---------------------------------------------------------------------------
# Context window resolution — two tiers. Whether tier B (family default)
# alone covers a routine new release, or a new tier-A exception is required,
# DIFFERS BY FAMILY — this is not symmetric, and getting the asymmetry
# backwards is exactly how the wrong "sonnet-5" entry happened before:
#
#   - Opus: PM-confirmed 2026-07-24 — presume ALL FUTURE Opus generations
#     get 1M, no per-generation confirmation needed. A future Opus 4.9
#     resolves via the family default with zero maintenance.
#   - Sonnet: PM-confirmed 2026-07-24 — do NOT presume forward. Sonnet has
#     historically had 1M access granted to a generation and later
#     withdrawn, so each Sonnet generation's window must be individually
#     confirmed before it's promoted out of the 200k family default. A
#     future Sonnet 7 stays at 200k until someone actually confirms
#     otherwise — silence is not confirmation.
#
# These figures are specifically the Claude CODE windows (not claude.ai
# chat, which this hook never runs under), per
# https://support.claude.com/en/articles/8606394-how-large-is-the-context-window-on-paid-claude-plans
# (confirmed against this PM's own Max-plan account, 2026-07-24). The PM's
# own quoted list of confirmed-1M-in-Claude-Code models: "Claude Sonnet 5,
# Fable 5, Opus 4.8, Opus 4.7, and Opus 4.6" — note Mythos is NOT on this
# list despite an earlier, wrong version of this table assuming it (same
# unreliable-cached-catalog mistake as the original sonnet-5/Mythos
# conflation); it is deliberately absent from both tables below and falls
# through to the generous safe default if ever encountered. Sonnet 4.6 ALSO
# gets 1M per the source, but ONLY with usage credits enabled on every plan
# tier, Max included (except usage-based Enterprise) — deliberately NOT
# added as an exception, since assuming credits are on would be a guess
# this hook can't verify (this PM does not use credits); it resolves via
# the 200k Sonnet family default.
#
# Tier A — _GENERATION_EXCEPTIONS: named generations whose window diverges
# from their family default. Checked first (most specific wins). Add a row
# here only when a new release's window is PM-confirmed (or sourced from the
# support article above) to genuinely differ from its family — never on an
# assumed/cached model-catalog figure, and never on a plan/credits state this
# hook can't introspect (no live signal — see
# docs/wiki/hook-model-context-window-maintenance.md). Sonnet 5 is the one
# row today.
#
# Tier B — _FAMILY_DEFAULTS: one row per model family, checked after
# exceptions. Opus is safe to generalize forward (see above); Sonnet is not
# — its 200k default is the conservative floor every unconfirmed generation
# falls back to, not a "known convention" the way Opus's 1M is.
# ---------------------------------------------------------------------------
_GENERATION_EXCEPTIONS: tuple[tuple[str, int], ...] = (
    ("[1m]", 1_000_000),
    ("-1m", 1_000_000),
    ("sonnet-5", 1_000_000),  # Claude Code, paid plan, no credits caveat
)

_FAMILY_DEFAULTS: tuple[tuple[str, int], ...] = (
    ("opus", 1_000_000),
    ("fable", 1_000_000),
    ("sonnet", 200_000),
    ("haiku", 200_000),
    # "mythos" deliberately absent -- not on the PM's confirmed list; an
    # encountered "claude-mythos-*" id falls through to the generous safe
    # default (COORDINATOR_DEFAULT_CONTEXT_WINDOW) rather than a guess.
)

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
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
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


def _extract_last_usage_tokens(transcript_path: str, tail_bytes: int = 300_000) -> Optional[int]:
    """Return real context-window occupancy (input + cache_creation + cache_read tokens)
    from the most recent assistant-turn usage block in the transcript, or None if no
    usage block is found in the tail window.

    Reads only the trailing `tail_bytes` of the transcript rather than the whole file —
    a live session's transcript can be tens of MB and only the latest turn's usage figure
    is needed. A tail read may truncate the first captured line mid-record; that partial
    line simply fails json.loads and is skipped, never raises.
    """
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
            data = fh.read()
    except Exception:
        return None

    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return None

    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line or '"usage"' not in line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        usage = record.get("message", {}).get("usage")
        if not isinstance(usage, dict):
            continue
        try:
            total = (
                int(usage.get("input_tokens", 0))
                + int(usage.get("cache_creation_input_tokens", 0))
                + int(usage.get("cache_read_input_tokens", 0))
            )
        except (TypeError, ValueError):
            continue
        if total > 0:
            return total
    return None


def _resolve_context_window(model_id: str) -> int:
    """Resolve the context-window size (in tokens) for a model_id.

    Four-tier resolution, most-specific/override-able first. A routine new
    model release that follows its family's established window size resolves
    at tier 3 with NO code change required — only a release that breaks its
    family's convention needs a tier-2 addition.

      1. COORDINATOR_MODEL_CONTEXT_WINDOWS env var (JSON substring→int map) — an
         ops-level override so any model can be patched same-day without a
         code change/release cycle here.
      2. _GENERATION_EXCEPTIONS — named generations whose window diverges from
         their family default in Claude Code specifically. Sonnet 5 gets
         boosted to 1M there with no credits caveat (support.claude.com
         8606394), diverging from every other Sonnet generation's 200k.
         Sonnet 4.6 ALSO gets 1M per that source, but only with usage
         credits enabled — deliberately not added as an exception, since
         this hook can't verify credits are on and assuming so would be a
         guess (this PM doesn't use credits; Sonnet 4.6 resolves to the
         200k family default here).
      3. _FAMILY_DEFAULTS — one row per model family (opus/fable/sonnet/
         haiku; "mythos" deliberately absent — not on the PM's confirmed
         list). Covers every generation of that family not flagged as an
         exception above — this is what makes an ordinary Opus release
         (an Opus 4.9, ...) maintenance-free. Sonnet is asymmetric: its
         200k default is a conservative floor, not a convention safe to
         extend forward — a new Sonnet generation stays at 200k until
         individually confirmed otherwise (Sonnet has a history of 1M
         access granted then withdrawn), never assumed from the pattern.
         maintenance-free.
      4. COORDINATOR_DEFAULT_CONTEXT_WINDOW env var (default "1000000") for a
         model_id matching no family at all — deliberately generous so a
         genuinely unrecognized model fails toward not panicking.
    """
    override_raw = os.environ.get("COORDINATOR_MODEL_CONTEXT_WINDOWS")
    if override_raw:
        try:
            overrides = json.loads(override_raw)
            if isinstance(overrides, dict):
                for substr, window in overrides.items():
                    if substr in model_id:
                        try:
                            return int(window)
                        except (TypeError, ValueError):
                            continue
        except Exception:
            pass  # malformed override — fall through to tiers 2-4, never raise

    for substr, window in _GENERATION_EXCEPTIONS:
        if substr in model_id:
            return window

    for substr, window in _FAMILY_DEFAULTS:
        if substr in model_id:
            return window

    return int(os.environ.get("COORDINATOR_DEFAULT_CONTEXT_WINDOW", "1000000"))


# ---------------------------------------------------------------------------
# Unrecognised-Sonnet-generation monitoring (C1b, 2026-07-24 invariant intact).
#
# OBSERVABILITY ONLY -- this does NOT change _resolve_context_window's
# resolution logic, invert the Opus/Sonnet asymmetry, or touch
# _GENERATION_EXCEPTIONS/_FAMILY_DEFAULTS. An unrecognised Sonnet model_id
# still correctly resolves to the 200k family default (the conservative,
# intentional behaviour) -- this only notices and reports the fact so a
# human can decide whether it deserves a _GENERATION_EXCEPTIONS row.
#
# The residual gap this closes: every non-excepted Sonnet model_id resolves
# to 200k by design, forever -- that's true of both a brand-new, never-seen
# generation AND an old, already-well-understood one (claude-sonnet-4-5, for
# example, is *supposed* to sit at 200k and always will). Naively logging on
# every non-excepted Sonnet id would fire constantly for that common,
# already-understood case -- pure noise, not a signal a human could act on.
# So the roster below seeds the *currently known* generations as "already
# reviewed", and this fires only the first time a Sonnet model_id this
# process has never durably recorded before is seen -- i.e. a genuinely new
# generation landing unrecognised, not a repeat sighting of an existing one.
#
# Durable across sessions (a new model release is a rare, cross-session
# event, not a per-session one) via a plain JSON list in tempdir, same
# best-effort/fail-open posture as the rest of this module's file-backed
# state: a lost tempdir (reboot, cleaner) just means the roster reseeds and
# the advisory may fire once more than strictly necessary -- never silence
# forever, never a crash.
# ---------------------------------------------------------------------------

_KNOWN_SONNET_GENERATIONS: tuple[str, ...] = (
    "sonnet-4-5",
    "sonnet-4-6",
    "sonnet-5",
)


def _sonnet_monitor_state_path(tmpdir: str) -> str:
    return os.path.join(tmpdir, "sonnet-generation-monitor-seen.json")


def _check_unrecognised_sonnet_generation(tmpdir: str, model_id: str) -> Optional[str]:
    """Return a one-time advisory the first time a never-before-seen Sonnet
    model_id resolves via the 200k family default, or None otherwise.

    Never raises -- fail-open on all I/O errors (degrades to "may re-fire",
    never to a crash or a permanently-suppressed signal).
    """
    if "sonnet" not in model_id:
        return None
    if any(gen in model_id for gen in _KNOWN_SONNET_GENERATIONS):
        return None
    if any(substr in model_id for substr, _ in _GENERATION_EXCEPTIONS):
        return None  # already has an explicit, human-confirmed window -- not a gap

    path = _sonnet_monitor_state_path(tmpdir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            seen = json.load(fh)
        if not isinstance(seen, list):
            seen = []
    except Exception:
        seen = []

    if model_id in seen:
        return None

    seen.append(model_id)
    try:
        fd, tmp_path = _tempfile().mkstemp(
            dir=tmpdir, prefix=".sonnet-generation-monitor-", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(seen, fh)
        os.replace(tmp_path, path)
    except Exception:
        # Best-effort -- if the write fails, this may fire again next time
        # the same model_id is seen, which is the safe direction (never
        # permanently silenced by a write failure).
        pass

    return (
        f"SONNET GENERATION MONITOR: unrecognised Sonnet model_id '{model_id}' resolved via"
        f" the 200k Sonnet family default, not a confirmed _GENERATION_EXCEPTIONS row. Per"
        f" the Opus/Sonnet asymmetry (Opus generalizes forward safely; Sonnet does not --"
        f" see the _GENERATION_EXCEPTIONS/_FAMILY_DEFAULTS comment above), this may actually"
        f" have a larger window (Sonnet 5 has 1M in Claude Code) but has not been"
        f" human-confirmed yet. If confirmed, add a _GENERATION_EXCEPTIONS row for it. This"
        f" fires once per newly-observed model_id on this machine."
    )


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

    Phase 2: Throttled (5 min) threshold warnings.
        Self-throttle + bark-once guards persisted to a durable per-session JSON
        state file (_load_advisory_state / _save_advisory_state) — survives the
        fresh-process-per-fire execution model (see the module-level state-
        management comment above _advisory_state_path). Model detected from
        transcript → context_window → byte thresholds. Bark-once guards keyed by
        transcript hash.
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
    # Phase 2: Throttled threshold warnings
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

    if not transcript_path or not os.path.isfile(transcript_path):
        return ""

    # --- Model detection (mirrors context-pressure-advisory.sh) ---
    # Read line-by-line; stop at the first matching line (grep -m1 behaviour).
    model_id = ""
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _MODEL_RE.search(line)
                if m:
                    model_id = m.group(1)
                    break
    except Exception:
        # model_id stays "" -- the context-window lookup below falls back
        # to its default-model bucket rather than skipping the threshold
        # check entirely.
        pass

    # --- Unrecognised-Sonnet-generation monitor (observability only; see the
    # comment block above _check_unrecognised_sonnet_generation). Checked
    # before threshold logic and, when it fires, takes this call's advisory
    # slot -- it's a durable one-time-per-model_id signal, not a per-session
    # dedup, so it never competes with the normal advisory/critical firings
    # for the same transcript_hash on a later call.
    sonnet_monitor_text = _check_unrecognised_sonnet_generation(tmpdir, model_id)
    if sonnet_monitor_text:
        return sonnet_monitor_text

    # --- Context window by model family (mirrors :171-197) ---
    context_window = _resolve_context_window(model_id)

    # --- Thresholds (tier-aware — mirrors coordinator-claude 07eb7599's shell-side recalibration) ---
    #
    # 1M-context tier: _AUTO_COMPACT_CEILING_TOKENS_1M (500K) is a FIXED
    # Anthropic-side ceiling, not a proportion of the window — a flat 50%
    # threshold on a 1M window lands exactly ON that ceiling (500K = 500K),
    # firing critical level with the auto-compact cut instead of ahead of
    # it. Anchor to absolute tokens below the ceiling instead: critical at
    # 450K (50K headroom for the handoff's own tool calls to finish before
    # the involuntary cut), advisory at 400K ("start wrapping up"). This
    # anchoring is deliberately NOT re-derived from context_window, so a
    # future window-size change on this tier cannot silently reintroduce
    # the collision.
    #
    # Sub-1M tiers (Sonnet/Haiku 200K default): compaction is still
    # observed ~window-proportional here, with no fixed-ceiling collision
    # to guard against — retain the 40%/50%-of-window model unchanged.
    if context_window >= 1_000_000:
        advisory_tokens = 400_000
        critical_tokens = 450_000
    else:
        advisory_tokens = context_window * 40 // 100
        critical_tokens = context_window * 50 // 100

    bytes_per_token = 7

    advisory_bytes = advisory_tokens * bytes_per_token
    critical_bytes = critical_tokens * bytes_per_token

    # Env-var overrides for testing/recalibration.
    try:
        advisory_bytes = int(os.environ["CONTEXT_ADVISORY_THRESHOLD"])
    except (KeyError, ValueError):
        # Unset/non-numeric override -- silently keep the computed default;
        # this is an opt-in testing/recalibration knob, not user-facing
        # config, so an absent or malformed value is the common case.
        pass
    try:
        critical_bytes = int(os.environ["CONTEXT_CRITICAL_THRESHOLD"])
    except (KeyError, ValueError):
        # Same rationale as the advisory-threshold override above: unset/non-numeric
        # is the common case, silently keep the computed default.
        pass

    # --- Transcript file size ---
    try:
        file_size = os.path.getsize(transcript_path)
    except Exception:
        return ""  # transcript vanished/unreadable mid-read (race with rotation) -- no advisory this call

    if file_size == 0:
        return ""

    # --- Transcript hash for in-memory bark-once guards ---
    try:
        transcript_hash = hashlib.md5(transcript_path.encode()).hexdigest()
    except Exception:
        transcript_hash = session_id

    # --- Autonomous run detection (read-only; sentinel written by EM/user, not this op) ---
    autonomous_run = os.path.isfile(str(sentinel_path(session_id)))

    model_str = model_id if model_id else "unknown"

    # Tier-aware description of where auto-compaction fires, for the warning
    # text below. Mirrors coordinator-claude 07eb7599's compact_desc.
    if context_window >= 1_000_000:
        compact_desc = (
            f"auto-compaction fires at a fixed ~{_AUTO_COMPACT_CEILING_TOKENS_1M // 1000}K"
            f" tokens on this 1M tier"
        )
    else:
        compact_desc = "auto-compaction is observed near ~60% of the window"

    # Real usage blocks are ground truth from the Anthropic API and are already
    # being written to the transcript per assistant turn, so no bytes/token
    # calibration constant is needed for the common case — the byte-proxy constant
    # `bytes_per_token` (or any recalibrated replacement) is a fallback-only guess
    # that historically ran ~2x too low on JSON/tool-schema-heavy sessions. Don't
    # let a future session "clean up" this fallback by deleting the primary
    # usage-block path below.
    usage_tokens = _extract_last_usage_tokens(transcript_path)

    if usage_tokens is not None:
        est_pct_tokens = usage_tokens * 100 // context_window

        # --- Critical check first (higher priority) — token-accurate path ---
        #
        # Compared against the absolute critical_tokens threshold (tier-aware,
        # not est_pct_tokens/critical_pct) — on the 1M tier this is the anchored
        # 450K figure, strictly below _AUTO_COMPACT_CEILING_TOKENS_1M, not a
        # recomputed 50%-of-window figure that would collide with it.
        if usage_tokens >= critical_tokens and transcript_hash not in cp_state.get("critical_fired", []):
            _mark_advisory_fired(cp_state, transcript_hash, critical=True)
            _save_advisory_state(tmpdir, session_id, cp_state)

            if autonomous_run:
                return (
                    f"CONTEXT PRESSURE — HIGH ({model_str}): est. ~{est_pct_tokens}% of window used"
                    f" — measured from the transcript's own Anthropic API usage blocks"
                    f" (input + cache_creation + cache_read tokens), not a byte-size estimate."
                    f" Compaction is close — {compact_desc}. Autonomous run active —"
                    f" continuing per PM instruction. Verify all progress is in TaskList and"
                    f" committed to disk. Compaction will compress context but tasks persist."
                    f" (Transcript-derived: {usage_tokens} tokens vs {context_window}-token window)"
                )
            else:
                return (
                    f"CONTEXT PRESSURE — HIGH ({model_str}): est. ~{est_pct_tokens}% of window used"
                    f" — measured from the transcript's own Anthropic API usage blocks"
                    f" (input + cache_creation + cache_read tokens), not a byte-size estimate."
                    f" Note: {compact_desc}."
                    f" If this estimate looks right for the work you've done, consider running"
                    f" /handoff soon — the handoff itself consumes context, so leave headroom."
                    f" (Transcript-derived: {usage_tokens} tokens vs {context_window}-token window)"
                )

        # --- Advisory check — token-accurate path ---
        # Compared against the absolute advisory_tokens threshold (tier-aware,
        # same rationale as the critical check above).
        if usage_tokens >= advisory_tokens and transcript_hash not in cp_state.get("advisory_fired", []):
            _mark_advisory_fired(cp_state, transcript_hash, critical=False)
            _save_advisory_state(tmpdir, session_id, cp_state)

            if autonomous_run:
                return (
                    f"CONTEXT PRESSURE — ADVISORY ({model_str}): est. ~{est_pct_tokens}% of window used"
                    f" — measured from the transcript's own Anthropic API usage blocks"
                    f" (input + cache_creation + cache_read tokens), not a byte-size estimate."
                    f" Context usage is getting heavy. Autonomous run: checkpoint state"
                    f" to disk at the next natural boundary so the run is resumable."
                    f" (Transcript-derived: {usage_tokens} tokens vs {context_window}-token window)"
                )
            else:
                return (
                    f"CONTEXT PRESSURE — ADVISORY ({model_str}): est. ~{est_pct_tokens}% of window used"
                    f" — measured from the transcript's own Anthropic API usage blocks"
                    f" (input + cache_creation + cache_read tokens), not a byte-size estimate."
                    f" Context usage is getting heavy. Consider completing the current"
                    f" task unit, then running /handoff. This is informational — no action required"
                    f" yet."
                    f" (Transcript-derived: {usage_tokens} tokens vs {context_window}-token window)"
                )

        return ""

    # --- No usage block found: FAIL LOUD, do not guess a percentage. ------
    #
    # C1 decision (2026-07-27, executor judgment recorded per plan instruction):
    # this op is dispatched via a FRESH process per PostToolUse fire (see the
    # module-level state-management comment above _advisory_state_path) with
    # no resident daemon -- the whole point of folding these checks into one
    # in-process op was eliminating spawns/round-trips on the hot PostToolUse
    # path. Calling POST /v1/messages/count_tokens here would add a
    # synchronous network round-trip (latency + availability + an API-key
    # requirement this hook process cannot assume it has -- Claude Code
    # subscription auth does not guarantee a usable ANTHROPIC_API_KEY in this
    # process's environment) to a path that already fires on every tool call
    # once throttled. That cost is paid on EVERY fallback fire, not just the
    # rare one, so count_tokens was rejected as the fallback here (it stays
    # available as a manual/offline calibration tool, just not wired into this
    # hot path). The old 7-bytes/token proxy is removed instead of replaced --
    # it was silently confident and ~2x too low on JSON-heavy sessions, which
    # is worse than admitting we don't know. This branch never estimates a
    # percentage; it says plainly that none is available.
    #
    # The old critical/advisory byte thresholds are kept ONLY as a "worth
    # mentioning at all" size gate (large transcript, still no usage data is
    # unusual and worth a heads-up) -- never as an input to a displayed
    # percentage. Same bark-once dedup as before, so this fires at most once
    # per transcript per tier.
    if file_size >= critical_bytes and transcript_hash not in cp_state.get("critical_fired", []):
        _mark_advisory_fired(cp_state, transcript_hash, critical=True)
        _save_advisory_state(tmpdir, session_id, cp_state)

        return (
            f"CONTEXT PRESSURE — UNKNOWN ({model_str}): no Anthropic API usage block was"
            f" found in the trailing transcript window, so context usage cannot be measured."
            f" The transcript is already large enough that this is worth flagging explicitly"
            f" rather than silently guessing a number — the previous byte-size proxy has been"
            f" removed because it ran ~2x too low on JSON/tool-schema-heavy sessions, which is"
            f" worse than no estimate at all. Use your own judgment on how much of the session"
            f" is left, or /handoff proactively if in doubt; the estimate will resume once a"
            f" usage block reappears in the transcript."
            f" (Transcript: {file_size} bytes, no usage-derived token count available)"
        )

    if file_size >= advisory_bytes and transcript_hash not in cp_state.get("advisory_fired", []):
        _mark_advisory_fired(cp_state, transcript_hash, critical=False)
        _save_advisory_state(tmpdir, session_id, cp_state)

        return (
            f"CONTEXT PRESSURE — UNKNOWN ({model_str}): no Anthropic API usage block was"
            f" found in the trailing transcript window, so context usage cannot be measured."
            f" This is informational — the transcript has grown large enough to be worth"
            f" noting, but no percentage is given because none can be measured reliably."
            f" Use your own judgment on session length; the estimate will resume once a usage"
            f" block reappears in the transcript."
            f" (Transcript: {file_size} bytes, no usage-derived token count available)"
        )

    return ""


# ---------------------------------------------------------------------------
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
    under both eager and COORDINATOR_CORE_LAZY_OPS=1 import modes, `subprocess`
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
        with open(rt_bark_sentinel, "w", encoding="utf-8") as fh:
            fh.write(str(int(time.time())))
    except Exception:
        # Write failed -- fail open toward "fires again next call" rather than
        # silently losing the tripwire forever.
        pass

    # --- Autonomous-run detection (read-only sentinel written by EM/user, not this op) ---
    autonomous = os.path.isfile(str(sentinel_path(em_sid)))

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
# subagent_type field (see the coordinator-claude stub's stdin→params mapping — only
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
        with open(sentinel, "w", encoding="utf-8") as fh:
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

    Folding the fourth check in retires coordinator-claude's separate PostToolUse(Write)
    registration for nudge-unauthorized-handoff.py — one interpreter start per
    Write instead of two (cross-repo/inbox/2026-08-06-coordinator-claude-em-postuse-fold-
    nudge-unauthorized-handoff.md). It requires coordinator-claude's dispatcher stub to map
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
        uh_text = await uh_coro
        return post_advisory(uh_text) if uh_text else no_advisory()

    # Run all four checks concurrently — they use disjoint sentinel namespaces.
    cp_text, rt_text, ad_text, uh_text = await asyncio.gather(
        asyncio.to_thread(_check_context_pressure_sync, session_id, transcript_path),
        asyncio.to_thread(_check_runtime_tripwire_sync, session_id, agent_id),
        asyncio.to_thread(_check_first_agent_dispatch_sync, session_id, tool_name),
        uh_coro,
    )

    texts = [text for text in (cp_text, rt_text, ad_text, uh_text) if text]
    if texts:
        return post_advisory("\n\n".join(texts))
    return no_advisory()

"""
coordinator_core.session.guard_unlock_sentinel — single-source resolver for
the in-session hard-deny guard unlock sentinel, keyed by BOTH session id and
guard module name.

WHY THIS EXISTS

Every hard-deny guard override today is a `COORDINATOR_OVERRIDE_*`
environment variable that must be set before session launch. The moment that
teaches an operator the override exists — the deny message itself — is
inside a session that can no longer set it: a Bash export does not reach the
harness's own PreToolUse hook evaluation, and there is no way to relaunch
mid-task without losing the reason the override is needed. This module is
the reachable remedy: an operator, from a terminal outside the denied
session, drops a one-shot sentinel file naming the exact `(session_id,
guard_name)` pair to clear, and the engine seam that produced the deny
consumes it on the next attempt.

This is deliberately narrower than `dispatch_nudge_sentinel` and
`autonomous_sentinel`, its two siblings in this package: those are keyed on
session id alone because they gate one binary behavior per session. A guard
unlock must never generalize into "all guards off for this session" — see
the plan's Anti-scope — so the filename is keyed on the (session_id,
guard_name) pair, not the session id alone, and a single grant clears
exactly one guard once.

Resolution follows the same convention as both siblings: the platform temp
directory via `tempfile.gettempdir()` (honours TMPDIR/TEMP/TMP per-platform),
never a hardcoded POSIX `/tmp`. Windows is first-class here — DR-record
`docs/decisions/DR-222-health-sentinel-durability-parity-settings-home-dual-read.md`
is the closest prior ruling on sentinel-home conventions and does not apply
here: this sentinel has exactly one writer (the operator, by hand) and
exactly one home, so a dual-home read would be unjustified complexity, not
parity.

Spec backlink: docs/plans/2026-08-03-in-session-operator-unlock-for-the-hard-.md § C1.

Negative-spec:
    - Do NOT hardcode ``/tmp`` or reach for ``tempfile.gettempdir()``
      directly at a new call site for this sentinel — import and call
      ``sentinel_path()`` so there is exactly one place this convention can
      drift again.
    - Do NOT add a second candidate location or an OR-list of candidate
      homes for this sentinel — it has exactly one home.
    - Do NOT let ``consume()`` raise. A crash here would fail a guard OPEN
      — the one direction a hard-deny guard must never fail in — so every
      failure mode (vanished file, permissions error, unresolvable temp
      dir) is caught and normalized to ``False``.
    - Do NOT key the sentinel on session id alone. One unlock must clear
      exactly one guard in exactly one session (AC2) — never a blanket
      "all guards off" switch (see the plan's Anti-scope).
    - Do NOT join the sanitized ``(session_id, guard_name)`` components on
      ``_`` or any run of it (``__`` included). A literal ``_`` survives
      ``_sanitize_component`` unmodified, so an underscore-based separator
      is not a true delimiter and two distinct pairs can render to the same
      filename. ``sentinel_path`` joins on ``.`` instead, a character
      ``_sanitize_component`` always strips from both raw components, which
      makes the join collision-free by construction. Do not use a hash
      either — this path is typed by hand by a human operator, and
      readability is load-bearing for that.
    - Do NOT write this sentinel from agent-reachable code. It exists to be
      typed by a human operator; this module only resolves and consumes the
      path, it never creates one.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any, Dict

_SENTINEL_PREFIX = "coordinator-guard-unlock-"

_UNSAFE_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize_component(value: str) -> str:
    """Slugify a path-filename component; never lets a separator through.

    Guard module names and session ids are ``[a-z0-9_]`` by construction,
    but this does not assume it: anything outside the safe set (path
    separators, ``..``, whitespace, null bytes, and ``.``) is replaced with
    ``_`` rather than passed through. Note this means a literal ``_`` DOES
    survive sanitization unmodified — so ``_`` (and any run of it, e.g.
    ``__``) is never collision-free as a join separator between two
    sanitized components; see ``sentinel_path``, which joins on ``.``
    instead, a character this function always strips.
    """
    cleaned = _UNSAFE_CHARS.sub("_", value)
    return cleaned or "_"


def sentinel_path(session_id: str, guard_name: str) -> Path:
    """Return the guard-unlock sentinel path for ``(session_id, guard_name)``.

    Resolves the platform temp directory via ``tempfile.gettempdir()``
    rather than a hardcoded POSIX ``/tmp`` — the single point of truth for
    both the operator who writes this sentinel by hand and the engine seam
    that consumes it. Keyed on both components so one unlock clears exactly
    one guard in exactly one session (AC2).

    Joined on ``.`` rather than ``_``/``__``: ``_sanitize_component``'s
    ``[^a-zA-Z0-9_-]`` class maps every ``.`` inside either raw component to
    ``_``, so a literal ``.`` can only ever appear in the string at the one
    position this function itself inserts it. That makes the two-way split
    between session and guard unambiguous by construction, regardless of
    what characters either raw component contains — unlike the prior ``__``
    join, where a literal ``_`` surviving sanitization meant two distinct
    ``(session_id, guard_name)`` pairs could render to the identical
    filename (see the module's Negative-spec block).

    Deliberately not a hash of either component: an operator types this
    path by hand, off the deny message, from a terminal outside the denied
    session — a hashed component would destroy the one property (glanceable
    readability) that makes the whole mechanism usable. Do not "fix" this
    back to a hash.
    """
    session_part = _sanitize_component(session_id)
    guard_part = _sanitize_component(guard_name)
    return Path(tempfile.gettempdir()) / f"{_SENTINEL_PREFIX}{session_part}.{guard_part}"


def consume(session_id: str, guard_name: str) -> bool:
    """One-shot consume the unlock for ``(session_id, guard_name)``.

    Returns True exactly once per grant: if the sentinel exists, it is
    unlinked and True is returned; a second call for the same pair (no new
    sentinel written in between) returns False, so a denied write is
    re-denied on retry (AC3).

    Race-safe by construction: ``Path.unlink()`` is the atomic OS operation
    that decides ownership when two callers race on the same path — the
    caller whose unlink succeeds observes True, the other observes
    ``FileNotFoundError`` and is normalized to False below.

    Never raises. Every failure mode — the file already gone, a permissions
    error, an unresolvable temp directory — is caught here and returned as
    False, because a crash in this function would fail its caller's guard
    OPEN, which is the one direction a hard-deny guard must not fail in.
    """
    try:
        path = sentinel_path(session_id, guard_name)
    except Exception:
        return False

    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
    except Exception:
        return False


def annotate_deny(
    out: Dict[str, Any],
    session_id: str,
    guard_name: str,
    doc_display: str,
) -> Dict[str, Any]:
    """Prepend the in-session-unlock line to a firing hard-deny envelope.

    Lives here rather than in either engine because both engine seams
    (`write_guards.engine`, `bash_guards.dispatch`) emit the identical line
    and a second copy is exactly the drift this chunk's one-seam design
    exists to prevent — the same reasoning that put the intercept at the
    engines instead of in 47 guards applies to the text those engines print.

    Register: addressed AWAY from the agent, matching the established fleet
    shape (`bash_guards._helpers.operator_override_note`'s "Bypass options
    for a human operator, not this agent: ..."). That framing is what keeps
    an agent-visible message naming its own unlock honest — it reads as an
    affordance for the human watching, not as sanction for the agent that
    was just stopped. Shown FIRST, ahead of any pre-launch-key mention baked
    into the guard's own reason text, because it is the channel actually
    reachable from inside the denied session.

    `doc_display` is passed in rather than resolved here: both callers
    already import `bash_guards._helpers._resolve_override_keys_doc_display`,
    and resolving it here would add a `session -> bash_guards` import edge
    for one string.

    A missing/empty ``session_id`` skips the line rather than rendering a
    sentinel path keyed to an empty session — there would be nothing literal
    and actionable to print.

    Never raises. A malformed envelope is returned unchanged: this function
    only ever runs on a deny that has already been decided, and an
    augmentation that crashed would turn that settled deny into an engine
    crash.
    """
    if not session_id:
        return out
    try:
        hso = out.get("hookSpecificOutput")
        if not isinstance(hso, dict):
            return out
        reason = hso.get("permissionDecisionReason")
        if not isinstance(reason, str):
            return out
        line = (
            "Bypass options for a human operator, not this agent: create %s "
            "FIRST, as its own command -- chaining it onto the denied command "
            "re-denies, since the sentinel is checked before anything runs "
            "(in-session, one-shot, clears only this guard for this "
            "session) -- full list: %s"
            % (sentinel_path(session_id, guard_name), doc_display)
        )
        hso["permissionDecisionReason"] = "%s\n\n%s" % (line, reason)
    except Exception:
        return out
    return out

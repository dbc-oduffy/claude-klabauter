"""foreground_dispatch_strip -- shared library, NOT a registered hook.

Ported from DoE-claude `coordinator/hooks/scripts/_foreground_dispatch_strip.py`
per docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C4. THREE
ADAPTATIONS (the class-1 sites named in this package's own `__init__.py`
docstring), all direct-import or direct-primitive substitutions for a
sibling reached via `sys.path` insert on the doctrine plane:

  1. `resolve_wiki_citation` -- imported directly from this package's own
     already-landed `message_envelope.py` (W4-C3), no path manipulation.
  2. `resolve_git_common_dir` -- imported directly from this package's own
     `git_common_dir.py` (this chunk, W4-C4), no path manipulation.
  3. `ensure_session_dir` -- imported directly from this package's own
     already-landed `session_hub.py` (W4-C3), no path manipulation.

A FOURTH site, not named in the `__init__.py` docstring's two classes
because it substitutes a PRIMITIVE rather than crossing an import boundary:
the local `_git_root(start)` walk-up (a duplicate of a walk this engine
already implements) is replaced with `coordinator_core.git.repo_root.
show_toplevel(str(start))` -- the engine's own zero-spawn walk primitive,
same contract (root or `None`, given an explicit start directory, never
raises). This module's own walk never spawned a subprocess to begin with
(unlike `worktree_isolation_strip.py`'s `_git_root()`), so this is a
duplication removal, not a fallback drop.

Everything else (the three-state `run_in_background` discrimination, the
calibration marker, the escape hatch, the per-reroute notice, the
fail-open/fail-closed split) is unchanged from the source -- this module
remains a BYTE-FAITHFUL PORT of `coordinator_core.hooks.
nudge_foreground_agent_dispatch`'s decision logic, per the source's own
docstring.

Single-emitter fold-in (2026-07-31, re-land of the 2026-07-30-reverted
background-by-default reroute): `nudge-foreground-agent-dispatch.py` used to
be independently registered on the `Agent` PreToolUse matcher, relaying the
engine's `coordinator_core.hooks.nudge_foreground_agent_dispatch`
foreground-dispatch reroute op's REROUTE-gate result (a
`hookSpecificOutput.updatedInput` rewriting a foreground
`run_in_background: false` dispatch to `true`). That reroute was reverted
2026-07-30 on the conclusion that `updatedInput` does not bind
`run_in_background` for the `Agent` tool -- a fresh live probe (harness
2.1.220, single emitter, no competitor) disproved that: the EM regained
control 32.3s before the subagent finished, genuinely backgrounded. The real
root cause was that this shim's own `updatedInput` was racing
`enforce-agent-dispatch-mode.py`'s own `updatedInput` on the SAME `Agent`
matcher -- Claude Code runs same-event PreToolUse hooks in parallel with
undefined completion order, and `updatedInput` is last-writer-wins, so
whichever hook finished second silently clobbered the other's rewrite. This
is the identical clobber class `worktree_isolation_strip.py` (Concern E) and
`named_dispatch_strip.py` (Concern F) already closed for the worktree-strip
and named-dispatch-strip pairs; this module closes it for the foreground
reroute too, using those fixes as precedent shape.

Fix shape: the foreground-reroute DECISION (this module) is a pure,
side-effect-limited computation -- no stdout, no sys.exit -- that
`enforce-agent-dispatch-mode.py` folds into its own single merged
`updatedInput` (mode elevation + sidecar + contract-blocks + role-framing +
worktree-strip + named-dispatch-strip + foreground-reroute, all layered onto
ONE `merged` dict, ONE emission site, as "Concern G"). The engine's own
`coordinator_core.hooks.nudge_foreground_agent_dispatch` op stays the
reference implementation of this exact algorithm with its own dedicated
tests -- this module is a duplicate on this one seam deliberately, not an
oversight: calling into the engine in-process would drag the whole
`coordinator_core.hooks` package into a hot path this hook already pays a
role-framing pass on for EVERY Agent dispatch; a subprocess call would cost
a fresh process spawn on that same every-dispatch path. A pure-Python
computation costs neither, at the price of one duplicate algorithm between
this module and its reference op.

Three-state run_in_background logic (preserved exactly from the engine's
reference op):
    - present-and-true  -> nothing to do (already backgrounded -- correct
                           shape); the durable calibration marker is still
                           written, because presence of the param -- either
                           value -- is what proves the build exposes it.
    - present-and-false -> reroute (or deny, if no safe rewrite target).
    - absent            -> reroute only if calibrated (this session was
                           previously seen sending the param, proven by the
                           durable `.harness-bg-capable` marker); otherwise
                           nothing to do (brick-proof PASS -- a build with no
                           such param omits it on every dispatch, and acting
                           on that would gate every Agent call on the
                           machine).

Calibration marker: `<git-common-dir>/coordinator-sessions/<session_id>/
.harness-bg-capable`, written (touch, idempotent) whenever the key arrives
PRESENT with either value. Escape hatch: `<git-common-dir>/coordinator-
sessions/<session_id>/.foreground-ok` -- when present, an otherwise-
actionable dispatch passes through unrewritten for the rest of that session.
Both markers are resolved from `git_dir`, itself resolved from `cwd` WITHOUT
spawning a subprocess. Session bookkeeping lives under the COMMON dir
specifically because that is what every hook/session sharing this repo (main
clone or any linked worktree) resolves to the same path -- the private
per-worktree dir would silently fragment one session's markers across
worktrees.

NOTICE ON EVERY REROUTE -- load-bearing, not a style choice: the pre-revert
version of this logic (the engine's own commit history, 2026-07-29)
suppressed the reroute advisory after the first one per session (`.foreground-reroute-
noticed`, "bark-once"). That suppression is why the non-binding race went a
whole session undetected: the ONLY feedback channels a broken reroute has are
its own notice and the tool result, and a silenced notice took the one
channel that could have surfaced the mechanism silently failing. This module
carries NO notice-once state and never will -- `compute_foreground_reroute`
returns a fresh notice string on every single reroute it computes, full stop.

Fail-open on every detection-failure leg, mirroring `worktree_isolation_
strip.compute_strip`: an unresolvable `cwd`/git-dir, an unreadable marker, or
any other I/O failure degrades toward "nothing to do" (`None`) rather than
raising -- except the one case that is DELIBERATELY fail-closed: a
provably-foreground dispatch (present-and-false, or calibrated-absent) that
cannot be safely rewritten (`tool_input` absent, not a dict, or missing the
load-bearing `prompt` key the Agent tool schema requires) returns a "deny"
result rather than silently letting the foreground dispatch through. Never
raises -- this runs inside a PreToolUse gate and must not brick an Agent
dispatch.

Message-text divergence, deliberate: the "BYTE-FAITHFUL PORT" claim above
covers the three-state discrimination + calibration + escape-hatch DECISION
LOGIC only. `_REROUTE_NOTICE`/`_DENY_MESSAGE` are authored fresh for this
module's `additionalContext`/deny channels, not ported verbatim from the
reference op's own message constants -- wording drift here is intentional,
not an unflagged regression.

Input-coercion divergence, also deliberate: `compute_foreground_reroute`'s
`bg_true`/`has_bg` checks accept a raw Python `bool` or a mixed-case
`"true"`/`"false"` string, whereas the reference op only ever sees a
stringified `"true"`/`"false"` because its caller flattens every field
through its own payload accessor first. This module's caller hands it a raw
JSON-decoded dict instead, so it must coerce itself; the wider acceptance is
strictly more permissive in the safe (does-not-miss-a-foreground-dispatch)
direction. It is the *outcome* of the three-state logic, not the input
coercion feeding it, that is preserved exactly from the reference.

Spec backlink: coordinator_core/hooks/nudge_foreground_agent_dispatch.py
(the engine's reference implementation, the algorithm this module ports).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.hooks.support.git_common_dir import (
    resolve_git_common_dir as _resolve_git_common_dir,
)
from coordinator_core.hooks.support.message_envelope import resolve_wiki_citation
from coordinator_core.hooks.support.session_hub import ensure_session_dir

_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{4,}$")

_BG_CAPABLE_MARKER_NAME = ".harness-bg-capable"
_FOREGROUND_OK_MARKER_NAME = ".foreground-ok"

#: `message_envelope`-routed hooks use for their `_WIKI_ANCHOR` sites.
_UNLOCK_DOC_CITATION = "coordinator/docs/wiki/guard-unlock-channel.md"

_REROUTE_NOTICE = (
    "[foreground gate] this Agent dispatch was rewritten to run_in_background: true and "
    "is running now -- expect its result as a task notification, not inline; do not treat "
    "the next tool call as blocked on it. Coordinator default is backgrounded dispatch: a "
    "foreground subagent LOCKS THE EM IN PLACE, unable to issue the rest of its wave, "
    "reconcile plans, or answer the PM until it returns. This notice fires on EVERY "
    "reroute, not just the first, by design -- a silenced notice is how a broken reroute "
    "went undetected for a whole session before (2026-07-30). Rare legitimate foreground "
    "need: see {unlock_doc}."
).format(unlock_doc=resolve_wiki_citation(_UNLOCK_DOC_CITATION))

_DENY_MESSAGE = (
    "[foreground gate] denied: this Agent dispatch chose run_in_background: false (or "
    "omitted it on a session already proven to expose the param) but supplied no safely "
    "rewritable tool_input -- either tool_input was not forwarded, or it is missing the "
    "`prompt` key the Agent tool schema requires. Rewriting without `prompt` would dispatch "
    "a subagent with no instructions, silently -- worse than this deny. Reissue the call "
    "with a complete tool_input and run_in_background: true. Rare legitimate foreground "
    "need: see {unlock_doc}. Doctrine: "
    "coordinator/snippets/em-operating-doctrine.md § How to Dispatch."
).format(unlock_doc=resolve_wiki_citation(_UNLOCK_DOC_CITATION))


def _resolve_git_dir(cwd: Any) -> Optional[str]:
    if not isinstance(cwd, str) or not cwd:
        return None
    try:
        start = Path(cwd).resolve()
    except Exception:
        return None
    git_root = show_toplevel(str(start))
    if not git_root:
        return None
    common_dir = _resolve_git_common_dir(git_root)
    return common_dir or None


def _bg_capable_path(git_dir: str, session_id: str) -> Path:
    return Path(git_dir) / "coordinator-sessions" / session_id / _BG_CAPABLE_MARKER_NAME


def _foreground_ok_path(git_dir: str, session_id: str) -> Path:
    return Path(git_dir) / "coordinator-sessions" / session_id / _FOREGROUND_OK_MARKER_NAME


def _mark_bg_capable(git_dir: Optional[str], session_id: str) -> None:
    if not git_dir or not session_id:
        return
    marker = _bg_capable_path(git_dir, session_id)
    try:
        if marker.exists():
            return
        if not ensure_session_dir(marker.parent, session_id):
            return
        marker.touch()
    except Exception:
        pass


def _is_bg_capable(git_dir: Optional[str], session_id: str) -> bool:
    if not session_id or not git_dir:
        return False
    try:
        return _bg_capable_path(git_dir, session_id).exists()
    except Exception:
        return False


def compute_foreground_reroute(
    run_in_background: Any,
    session_id: Any,
    tool_input: dict,
    cwd: Any,
) -> Optional[tuple[str, Optional[bool], str]]:
    sid = session_id if isinstance(session_id, str) else ""
    if sid and not _SESSION_ID_RE.match(sid):
        sid = ""

    has_bg = run_in_background is not None and run_in_background != ""
    bg_true = run_in_background is True or (
        isinstance(run_in_background, str) and run_in_background.strip().lower() == "true"
    )

    git_dir = _resolve_git_dir(cwd)

    if has_bg and sid:
        _mark_bg_capable(git_dir, sid)

    if bg_true:
        return None

    if not has_bg:
        if not _is_bg_capable(git_dir, sid):
            return None

    if sid and git_dir:
        try:
            if _foreground_ok_path(git_dir, sid).exists():
                return None
        except Exception:
            pass

    if isinstance(tool_input, dict) and tool_input.get("prompt"):
        return (
            "reroute",
            True,
            _REROUTE_NOTICE,
        )

    return (
        "deny",
        None,
        _DENY_MESSAGE,
    )

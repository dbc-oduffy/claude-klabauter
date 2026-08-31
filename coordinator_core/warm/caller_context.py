"""coordinator_core.warm.caller_context -- one per-call caller-context accessor.

Spec backlink: state/dispatch-briefs/2026-08-28-the-four-folded-bash-guards-get-registered-not-folded/C0.md
Bug backlog: state/bug-backlog/2026-08-27-the-warm-engine-resolves-from-its-own-context-not-the-callers.yaml

THE FAMILY THIS CLOSES ONE MORE INSTANCE OF. A warm, long-lived server process has its own
ambient context -- process env, cwd, "current" session -- and every one of those is silently
the WRONG answer whenever the caller dialling this op is not the engine itself. Three
mechanisms of the same defect were named in one day (`memo_send`, `provision_report.
resolve_plugin_root`, `_resolve_sent_by`) and this module is the shared shape the bug-backlog
row's own "what would discharge it" asks for: one accessor, imported by both ends of the seam
(`warm/hook_http.py` on the write side, `bash_guards/dispatch.py` on the read side), that reads
plugin_root/cwd/session_id/agent_id FROM THE PER-CALL PAYLOAD first, with the ambient/
process-env read demoted to an explicitly-named fallback rung -- never rung 1.

PATTERN, mirrored from the two call sites that already got this right (both cited by name
because this module is a generalisation of them, not a new invention):

  - `bash_guards.dispatch_checks._override` (C14c, `32d5224ed`) -- prefers a per-call
    `payload["env"]` mapping over ambient `os.environ`, precisely so guard evaluation can move
    off a fresh child process without a per-session override boundary going silently dead.
  - `bash_guards.block_subagent_destructive_action` -- already reads `payload.get("session_id")`
    /`payload.get("cwd")`/`payload.get("agent_id")` directly, never falling back to any ambient
    notion of "the current session" for those three fields.

WHY plugin_root NEEDS A FALLBACK AND THE OTHER THREE DO NOT. `session_id`, `cwd`, and
`agent_id` are properties of ONE CALL -- there is no meaningful ambient answer for "which
session is this" once the caller's own payload is silent, so those three resolve to `None` on
a miss and callers already treat `None` as "unresolved" (see `block_subagent_destructive_
action`'s own no-agent_id-means-EM branch). `plugin_root` is different: it names a directory
that is the SAME across every call on a given machine (the coordinator-claude plugin install),
so an ambient fallback is a legitimate last resort there in a way it is not for a per-call
identity field -- provided it is a clearly-labelled LAST rung, never the first one a resolver
reaches for.

NEGATIVE SPEC.

- **No new resolution logic for plugin_root.** This module does not re-derive
  `resolve_plugin_root`'s three-rung ambient probe (`CLAUDE_PLUGIN_ROOT` env var ->
  `claude_config_dir()/plugins/coordinator-claude` -> `.doe-root` pointer) -- it reuses
  `subagent_sandbox.provision_report.resolve_plugin_root` verbatim as the fallback rung, so the
  two probes cannot drift apart. What this module adds is the payload-first rung IN FRONT of
  that whole chain, not a replacement for it.
- **No caller wiring yet.** C1/C6/C7 are this accessor's first consumers; this chunk is
  definition only, imported by nothing on the live per-call path. The DR-344 process-time/
  spawn-count measurement this plan's C0 body requires is therefore N/A at this chunk boundary
  -- there is no existing chain call site yet to baseline against, since nothing calls this
  module. Each of C1/C6/C7 owns measuring ITS OWN integration point before/after wiring this
  in, against ITS OWN chain baseline.
- **No re-derivation of `_resolve_subagent_identity`'s canonicalisation.** `agent_id` here is
  read off the payload VERBATIM (raw), exactly as `block_subagent_destructive_action.payload.
  get("agent_id")` already does -- canonicalising it is a guard-specific concern the guard
  itself already owns, not this accessor's job.
- **`hooks/cater_subagent_start.py` is NOT rewired here.** It hand-mirrors `resolve_plugin_
  root`'s rungs by explicit design decision (noted fifth site in the bug-backlog row); the
  chunk that defines this accessor does not touch a file this plan otherwise leaves alone.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from coordinator_core.subagent_sandbox.provision_report import (
    resolve_plugin_root as _resolve_plugin_root_ambient,
)

__all__ = ["CallerContext", "resolve_caller_context"]


@dataclass(frozen=True)
class CallerContext:
    """The five caller-owned facts a warm-engine call site needs, resolved per-call.

    Every field is `Optional[str]` -- a miss at every applicable rung is `None`, never an
    exception and never a silently-substituted ambient guess dressed up as a caller value.

    `pid` (added by docs/plans/2026-08-30-every-op-runs-in-the-callers-environment.md
    § C1b) is the fifth field: the CALLER process's own id, resolved payload-first with
    `os.getpid()` as its ambient fallback rung -- the same disposition `cwd` already has,
    for the same reason (see `resolve_caller_context`'s own docstring). It exists because
    `harness_registry.self_record()` keys off `CLAUDE_PID`, not off `session_id`, and the
    warm-identity cohort sweep (`state/audits/2026-08-30-warm-identity-cohort-sweep.md`)
    names three live defects that misattribute self-classification for exactly that
    reason -- a session-id-only fix leaves them standing.

    `settings_home` (added by docs/plans/2026-08-31-the-settings-home-crosses-the-warm-
    boundary.md § C2) is the sixth field: the caller's own `COORDINATOR_SETTINGS_HOME`
    claim, when it explicitly set one. Unlike the other five fields, it is never read off
    the `_caller` payload `resolve_caller_context` parses below -- it rides its own
    top-level `_settings_home` wire field (`warm/settings_home_claim.py`,
    `SETTINGS_HOME_FIELD`), because that field predates this dataclass and both wire
    producers (`warm.client`, `door.c`) already stamp it independently of `_caller`.
    `_serve_line` pops it via `settings_home_claim.request_claim` and joins it onto the
    already-resolved `CallerContext` with `dataclasses.replace` before handing the object
    to `dispatch` -- see that module's own body for why joining here, rather than a sixth
    parallel parameter threaded next to `caller`, is required rather than merely tidy. No
    ambient fallback rung: absence means "this caller has no opinion", never this
    process's own resolved home (see `warm/settings_home_claim.py`'s own "ABSENCE IS NOT A
    MISMATCH").
    """

    plugin_root: Optional[str]
    cwd: Optional[str]
    session_id: Optional[str]
    agent_id: Optional[str]
    pid: Optional[str]
    settings_home: Optional[str] = None


def resolve_caller_context(payload: Optional[Mapping[str, Any]] = None) -> CallerContext:
    """Resolve plugin_root/cwd/session_id/agent_id FROM THE PER-CALL PAYLOAD.

    `payload` is the same per-call mapping `bash_guards.dispatch_checks._override` and
    `block_subagent_destructive_action` already read (`session_id`, `cwd`, `agent_id` keys
    verbatim; `plugin_root` is the new key this accessor's first consumers populate). A
    `payload` that is `None`, not a `Mapping`, or missing a given key resolves that field via
    its own named fallback rung below -- never a bare, unlabelled `os.environ`/`os.getcwd()`
    read inline at the call site, which is the exact shape the bug-backlog row's three
    instances all took independently.

    Fallback rungs, one per field, DEMOTED (never rung 1):

      - `plugin_root` -> `subagent_sandbox.provision_report.resolve_plugin_root()`'s own
        ambient three-rung probe (env var -> plugin dir -> `.doe-root` pointer).
      - `cwd` -> `os.getcwd()`, this PROCESS's own working directory -- correct only when the
        caller and this process are the same, which a per-call payload miss cannot confirm; a
        genuinely fresh dispatch (payload absent entirely, e.g. a direct in-process call with
        no wire in between) is the only case this rung exists for.
      - `session_id`, `agent_id` -> no ambient fallback. Neither has a meaningful process-wide
        answer (module docstring, "WHY plugin_root NEEDS A FALLBACK AND THE OTHER THREE DO
        NOT"); a payload miss resolves straight to `None`.
      - `pid` -> `os.getpid()`, this PROCESS's own id -- the same disposition as `cwd`
        and for the same reason: correct only when the caller and this process are the
        same, which a per-call payload miss cannot confirm, so this rung exists for a
        genuinely fresh dispatch (payload absent entirely) rather than for a server
        reading an incomplete wire payload.
    """
    plugin_root: Optional[str] = None
    cwd: Optional[str] = None
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    pid: Optional[str] = None

    if isinstance(payload, Mapping):
        candidate = payload.get("plugin_root")
        if isinstance(candidate, str) and candidate:
            plugin_root = candidate

        candidate = payload.get("cwd")
        if isinstance(candidate, str) and candidate:
            cwd = candidate

        candidate = payload.get("session_id")
        if isinstance(candidate, str) and candidate:
            session_id = candidate

        candidate = payload.get("agent_id")
        if isinstance(candidate, str) and candidate:
            agent_id = candidate

        candidate = payload.get("pid")
        if isinstance(candidate, str) and candidate:
            pid = candidate

    if plugin_root is None:
        plugin_root = _resolve_plugin_root_ambient()

    if cwd is None:
        cwd = os.getcwd()

    if pid is None:
        pid = str(os.getpid())

    return CallerContext(
        plugin_root=plugin_root,
        cwd=cwd,
        session_id=session_id,
        agent_id=agent_id,
        pid=pid,
    )

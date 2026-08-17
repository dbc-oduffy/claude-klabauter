"""coordinator_core.write_guards.block_confined_agent_write — confine a
confined findings-agent's write-shaped tool calls to its own provisioned
sandbox directory, whatever the tool.

Purpose: docs/plans/2026-08-17-confined-findings-agents-cannot-write.md.
``coordinator/agents/code-reviewer.md`` § HARD RULE states that a confined
findings-agent's writes are confined to its own sandbox directory
(``state/subagent-share/<em_session_id>/``) — its remediation on any
provisioning failure is "fill the provisioned sidecar," never "write
somewhere else." That guarantee was expressed only as a tool omission
(``Write`` absent from the agent definition's ``tools:`` list), and
Example-cockpit-repo-em's clean probe (``cross-repo/inbox/2026-08-17-project-
Cockpit-em-agent-tools-declaration-not-enforced.md``) found the omission is
not in force: a plain ``Agent`` dispatch of ``coordinator:code-reviewer``
declared 5 tools and received 8, ``Write`` among the undeclared four, and it
was called, succeeded, and silently created its parent directory outside the
sandbox. This guard is the engine-side artifact that discharges the rule.

A first cut of this guard (commit ``7cefacf94``) matched ``Write`` only and
denied it unconditionally, on the theory that ``Edit`` was a "sanctioned
narrow path" because it cannot CREATE a file. That theory is now known
false: verified against a real dispatched ``coordinator:code-reviewer``
identity, an unconfined ``Edit`` could still MODIFY
``coordinator_core/write_guards/engine.py`` and
``coordinator_core/bash_guards/block_reviewer_bash_outside_allowlist.py`` —
the guard confining the same agent's Bash surface — and a Write to a path
outside the repo entirely (a ``scratchpad/`` sibling) was never contained by
a Write-only, deny-unconditionally shape in the first place. The correct
contract is containment, not tool-selective denial: a confined agent may
write-shaped-mutate ONLY inside its own sandbox, on every matcher.

Fires on any of ``MATCHERS`` when:
  (1) a raw ``agent_id`` is present (a top-level EM write carries none —
      see the module docstring precedent in every sibling guard in this
      package), AND
  (2) the resolved subagent identity's back-pointer ``subagent_type``
      satisfies ``is_confined_findings_agent`` (currently:
      ``coordinator:code-reviewer``), AND
  (3) the resolved target path is NOT contained under
      ``<git_root>/state/subagent-share/<em_session_id>/``, where
      ``<em_session_id>`` is the firing payload's own ``session_id`` (the
      EM session that dispatched this agent — verified this session against
      a real ``.git/coordinator-sessions/.agents/<agent_id>/em-session-
      id.txt`` back-pointer to equal the payload's ``session_id`` for a
      dispatched subagent's tool call).

Allow conditions (pass through):
  (1) The resolved target path IS contained under the agent's own sandbox
      root — the provisioned sidecar and anything else the agent creates
      inside its own directory.
  (2) No raw ``agent_id`` (EM main-loop write) — always allow.
  (3) The resolved identity's git root is unresolvable — allow. This guard
      is PER-KIND policy (only the confined-findings set is denied; every
      other resolved or unresolved kind is a legitimate write caller),
      matching ``block_subagent_plan_body_write``'s PM-directed 2026-06-09
      lookup-fail-is-allow posture, NOT the uniform-deny identity family
      (``block_subagent_commit``, ``block_subagent_destructive_action``).
      See the plan's Anti-scope #2 — do NOT fail closed here.
  (4) The back-pointer subagent_type lookup fails (missing/unreadable/
      malformed chain) — allow, same rationale as (3).
  (5) The resolved subagent_type is anything ``is_confined_findings_agent``
      does not recognize (including ``coordinator:executor``,
      ``coordinator:enricher``, ``coordinator:review-integrator``, and
      every other roster member) — allow.
  (6) Override env ``COORDINATOR_OVERRIDE_CONFINED_AGENT_WRITE=1`` — allow.

Negative-spec:
  - Does NOT match ``Write`` only — MATCHERS is the engine's full
    ``_VALID_MATCHERS`` set (``Write``, ``Edit``, ``MultiEdit``,
    ``NotebookEdit``). Matching ``Write`` alone was the defect this
    rewrite closes.
  - Does NOT copy ``_CONFINED_FINDINGS_AGENTS`` — imports
    ``is_confined_findings_agent`` from ``bash_guards._helpers`` so the
    membership set has exactly one definition. Verified at rewrite time
    (2026-08-17): that set is ``frozenset({"coordinator:code-reviewer"})``
    — ``coordinator:executor`` is NOT a member, so this guard does not
    confine it.
  - Does NOT fail closed on an unresolvable identity or subagent_type — see
    allow-conditions (3)-(4) above and the plan's Anti-scope #3. This is
    per-kind policy, not the uniform-deny identity family.
  - Does NOT hand-roll path normalization — containment routes through
    ``coordinator_core.ops._path_guard.contained_path``, with BOTH the
    candidate and the allowed-root string pre-processed through
    ``coordinator_core.write_guards._case_fold_path.casefold_path`` before
    either is wrapped in ``Path(...)`` — a case-varied write on macOS APFS
    (case-insensitive-but-case-preserving) or a Windows extended-length-
    prefix desync must not slip the containment check either direction.
    (`tests/test_casefold_bypass_lint.py` lints for exactly this shape.)
  - Does NOT touch ``bash_guards/`` — the Bash confinement for this same
    agent set held under the source probe; there is nothing to fix there.
  - Does NOT claim to close the memo's wider finding (``Agent``,
    ``Artifact``, ``Skill`` also undeclared-and-present); out of scope per
    the plan.

Deny-message register: `docs/wiki/guard-messaging.md` § Register — one fact
(WHAT HAPPENED), one terse alternative (WHAT TO DO INSTEAD: fill the
provisioned sidecar), no override key named inline. The override pointer, if
any, is rendered by ``operator_override_note`` — which itself resolves to
the empty string for a dispatched-subagent audience (the only audience that
can ever trip THIS guard, since allow-condition (2) already excludes the EM
main loop) — so in practice no pointer renders here at all; the call is kept
for the same reason every sibling guard keeps it: a single call site, not a
per-guard judgment call about its own audience.

Spec backlink: docs/plans/2026-08-17-confined-findings-agents-cannot-write.md
Source memo: cross-repo/inbox/2026-08-17-example-cockpit-repo-em-agent-tools-declaration-not-enforced.md
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._helpers import (
    is_confined_findings_agent,
    operator_override_note,
)
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.write_guards._case_fold_path import casefold_path
from coordinator_core.write_guards._repo_root import resolve_repo_root
from coordinator_core.write_guards._subagent_identity import (
    _read_backpointer_subagent_type,
    _resolve_subagent_identity,
)

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
#: hard-deny band; next free slot after block_subagent_guard_grant_write (47),
#: before block_consumed_handoff_edit (50) -- no other guard claims 48 or 49.
PRIORITY = 48

#: Generator-provenance declaration (coordinator_core/ops/generator_provenance.py).
#: This module performs no filesystem writes of its own — no state-file
#: writes, no log appends. It only reads a back-pointer chain under
#: <git_root>/.git/coordinator-sessions/.
GENERATES = []

#: Escape-hatch env var named (indirectly, via operator_override_note) as
#: this guard's override key.
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_CONFINED_AGENT_WRITE"

#: The sandbox root, relative to git_root, a confined agent's writes are
#: contained to. The EM session id segment is appended per-payload.
_SANDBOX_PARENT = ("state", "subagent-share")

#: tool_input keys that can carry the target path, in probe order.
#: NotebookEdit uses notebook_path; the rest use file_path. Mirrors
#: block_home_dir_memo_delivery.py's own ``_PATH_KEYS``.
_PATH_KEYS = ("file_path", "notebook_path")

#: Control-whitespace/C0-control sanitization before interpolating an
#: attacker-influenced file_path into a deny reason. Same pattern as every
#: sibling write guard in this package.
_CONTROL_WHITESPACE_RE = re.compile(r"[\t\r\n\f\v]")
_C0_CONTROL_RE = re.compile(r"[\x00-\x1f]")


def _sanitize_file_path_for_reason(file_path: str) -> str:
    """Port of FILE_PATH_SAFE — same shape as every sibling guard."""
    safe = _CONTROL_WHITESPACE_RE.sub(" ", file_path)
    return _C0_CONTROL_RE.sub("", safe)


def _extract_file_path(payload: Dict[str, Any]) -> str:
    """Probe ``tool_input`` for the target path across every MATCHERS shape
    (``file_path`` for Write/Edit/MultiEdit, ``notebook_path`` for
    NotebookEdit)."""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    for key in _PATH_KEYS:
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _deny_reason(file_path: str, payload: Optional[Dict[str, Any]] = None) -> str:
    """One fact, one alternative — `docs/wiki/guard-messaging.md` § Register.
    Names no override key; `operator_override_note` renders nothing for a
    dispatched-subagent audience, which is the only audience that reaches
    this function (see module docstring).
    """
    file_path_safe = _sanitize_file_path_for_reason(file_path)
    _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload)
    return (
        f"BLOCKED {file_path_safe}: this agent's writes are confined to its "
        "own sandbox.\n"
        "Use instead: fill your provisioned sidecar."
        + ("\n\n" + _note if _note else "")
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the confined-agent write guard against a PreToolUse payload.

    Returns ``None`` (allow) or the nested hard-deny envelope. Fails open on
    any missing/unresolvable identity leg (see module docstring allow-
    conditions (3)-(4) and the plan's Anti-scope #3) — this is per-kind
    policy, not the uniform-deny identity family.

    Order of checks, cheapest first (plan chunk C1 body, and the plan's own
    Anti-scope note that this package sits on the PreToolUse write hot path
    with 50-70 concurrent LLMs on this box): override env var; tool_name;
    empty raw agent_id; resolve identity; resolve subagent type;
    membership; containment.
    """
    # Honor escape hatch first.
    if os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
        return None

    # Tool-name guard — defense-in-depth (MATCHERS already filters this at
    # the engine level, but every sibling guard re-checks defensively).
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None

    # Empty raw agent_id -> no subagent at all (EM main-loop write) -> allow.
    raw_agent_id = payload.get("agent_id") or ""
    if not raw_agent_id:
        return None

    session_id = payload.get("session_id") or ""
    agent_id = _resolve_subagent_identity(raw_agent_id, session_id)
    if not agent_id:
        return None

    git_root = resolve_repo_root(payload.get("cwd"))
    if not git_root:
        return None

    subagent_type = _read_backpointer_subagent_type(
        git_root, agent_id, expected_em_session_id=session_id
    )
    if not is_confined_findings_agent(subagent_type):
        return None

    file_path = _extract_file_path(payload)
    if not file_path:
        return None

    sandbox_root = Path(
        casefold_path(str(Path(git_root, *_SANDBOX_PARENT, session_id)))
    )
    # A tool-supplied file_path is contractually absolute (every MATCHERS
    # tool requires it), but a relative string is joined against git_root
    # rather than left to resolve() against this PROCESS's cwd (which need
    # not be the payload's cwd at all) — fail-open on a stray relative path
    # would be the wrong direction for a containment check.
    candidate_raw = file_path if Path(file_path).is_absolute() else str(Path(git_root, file_path))
    candidate = Path(casefold_path(candidate_raw))
    if contained_path(candidate, [sandbox_root]) is not None:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _deny_reason(file_path, payload),
        }
    }

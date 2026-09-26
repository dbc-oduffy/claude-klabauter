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

Re-scoped 2026-09-26 (docs/plans/2026-09-26-retire-review-integrator.md,
row M1) for the reviewer-applies-own-findings contract: a confined agent may
ALSO write to a path in the invoking EM session's own registered review-target
set, so a reviewer can apply its findings in place in the reviewed artifact.
The set is read from ``<git_root>/.git/coordinator-sessions/<session_id>/
review-targets.txt`` — one repo-relative forward-slash path per non-blank
line, written by ``review-findings-ledger targets --add`` (claude-klabauter
``coordinator_core/ops/review_findings_ledger.py``). A candidate write is
admitted when it equals a listed path exactly (after resolving both against
``git_root`` and casefolding), never by directory containment — a target is
one file, not a tree. A missing, unreadable, or empty targets file admits
nothing extra: the guard then behaves exactly as it did before this
re-scope, sandbox-only. This is additive: the sandbox roots, the confined
set, and every fail-open identity leg below are unchanged.

Fires on any of ``MATCHERS`` when:
  (1) a raw ``agent_id`` is present (a top-level EM write carries none —
      see the module docstring precedent in every sibling guard in this
      package), AND
  (2) the resolved subagent identity's back-pointer ``subagent_type``
      satisfies ``is_confined_findings_agent`` (currently:
      ``coordinator:code-reviewer``), AND
  (3) the resolved target path is NOT contained under EITHER of the
      agent's sandbox roots — ``machinery_paths.share_dir(git_root,
      <em_session_id>)`` or the legacy
      ``<git_root>/state/subagent-share/<em_session_id>/`` — where
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
  (7) The resolved target path equals a registered review target for the
      firing payload's own ``session_id`` — see the module docstring's
      2026-09-26 re-scope note above.

Negative-spec:
  - Does NOT admit a review target by containment — the equality check in
    ``_is_registered_review_target`` matches one file, never a directory a
    target happens to sit under.
  - Does NOT widen the registered-target set itself from inside this guard
    — it only reads ``review-targets.txt``; only ``review-findings-ledger
    targets --add`` (EM-only, per the M3 bash guard) writes it.
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
from coordinator_core.session.machinery_paths import legacy_share_dir, share_dir
from coordinator_core.write_guards._case_fold_path import casefold_path
from coordinator_core.write_guards._repo_root import resolve_repo_root
from coordinator_core.write_guards._subagent_identity import (
    _read_backpointer_subagent_type,
    _resolve_subagent_identity,
)

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 48

GENERATES = []

_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_CONFINED_AGENT_WRITE"

#: block_home_dir_memo_delivery.py's own ``_PATH_KEYS``.
_PATH_KEYS = ("file_path", "notebook_path")

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
    file_path_safe = _sanitize_file_path_for_reason(file_path)
    _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload)
    return (
        f"BLOCKED {file_path_safe}: writes confined to own sandbox.\n"
        "Use instead: fill your provisioned sidecar."
        + ("\n\n" + _note if _note else "")
    )


def _review_targets_file(git_root: str, session_id: str) -> Path:
    return Path(git_root) / ".git" / "coordinator-sessions" / session_id / "review-targets.txt"


def _is_registered_review_target(git_root: str, session_id: str, candidate: Path) -> bool:
    """A candidate write is admitted when it equals a listed path exactly
    (after resolving both against ``git_root`` and casefolding) — a target
    is one file, not a tree, so this is equality, not containment. A
    missing, unreadable, or empty targets file admits nothing extra."""
    targets_path = _review_targets_file(git_root, session_id)
    try:
        raw = targets_path.read_text(encoding="utf-8")
    except OSError:
        return False

    for line in raw.splitlines():
        rel = line.strip()
        if not rel:
            continue
        target_raw = str(Path(git_root, rel))
        target = Path(casefold_path(target_raw))
        if candidate == target:
            return True
    return False


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
        return None

    # Tool-name guard — defense-in-depth (MATCHERS already filters this at
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None

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

    sandbox_roots = [
        Path(casefold_path(share_dir(git_root, session_id))),
        Path(casefold_path(legacy_share_dir(git_root, session_id))),
    ]
    # A tool-supplied file_path is contractually absolute (every MATCHERS
    candidate_raw = file_path if Path(file_path).is_absolute() else str(Path(git_root, file_path))
    candidate = Path(casefold_path(candidate_raw))
    if contained_path(candidate, sandbox_roots) is not None:
        return None

    if _is_registered_review_target(git_root, session_id, candidate):
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _deny_reason(file_path, payload),
        }
    }

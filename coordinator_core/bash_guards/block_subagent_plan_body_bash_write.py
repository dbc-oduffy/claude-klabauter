"""
coordinator_core.bash_guards.block_subagent_plan_body_bash_write -- Python
engine-ification of DoE's retired
``coordinator/hooks/scripts/block-subagent-plan-body-bash-write.sh``
PreToolUse(Bash) hook (deleted 2026-07-16, DoE ``2f8b8450``).

Purpose: closes the Bash escape from the Write/Edit/MultiEdit/NotebookEdit-only
deny in the sibling hook ``block-subagent-plan-body-write.sh`` (ported at
``coordinator_core.write_guards.block_subagent_plan_body_write``). A
``coordinator:executor`` can still mutate a plan body via Bash -- `sed -i`,
`tee ... >>`, `> docs/plans/x.md`, `cp`/`mv`/`dd`. This guard denies the
enumerated COMMON write idioms for that command shape; it does not
categorically close the Bash escape (an unlisted idiom still passes) -- the
reference hook is explicit about this being a cost-raise, not a closure.

This is a faithful engine-ification, not a redesign: it ports the reference
hook's identity resolver, back-pointer subagent_type lookup, the AMBIGUOUS
collision-sentinel unconditional-deny branch, the 4-idiom target-detection
regex set (the reference hook 5th "heredoc" idiom is dead code per an
already-landed code-reviewer finding -- idiom 1 plain-redirect match always
fires first for any command shape that would satisfy idiom 5; it is NOT
ported, per the W3a recipe explicit instruction not to port dead code
forward), the per-session deny log side effect, and both deny-reason strings
verbatim.

TWO INDEPENDENT AXES -- preserved from the reference hook, not conflated:
  (a) IDENTITY axis -- mirrors ``block-subagent-plan-body-write.sh`` EXACTLY,
      including AMBIGUOUS-canonical-id -> fail-CLOSED (deny) semantics, no
      carve-out.
  (b) TARGET-DETECTION axis -- once identity resolves to
      ``coordinator:executor``, scan the Bash command for an UNAMBIGUOUS write
      to ``docs/plans/*.md``. Any doubt -> fail OPEN (allow); a false-negative
      Bash escape is the pre-existing status quo (asymmetric risk vs. a
      false-positive that breaks a legitimate executor command). Reads
      (grep/cat without redirect) ALLOW.

Identity-resolver provenance note (deliberate divergence from the W3a recipe
section (a) "reuse subagent_sandbox.engine's ``_canonical_agent_id``" default):
the reference hook for THIS guard calls the SAME bash
``resolve_subagent_identity`` / ``cs_build_canonical_agent_id`` pair that
``block-subagent-plan-body-write.sh`` calls (both source
``lib/coordinator-session.sh`` and invoke the identical function) -- NOT the
simplified resolver ``subagent_sandbox.engine._canonical_agent_id`` built for
a different reference hook (``block-reviewer-write-outside-sidecar.sh``),
which -- per that write-guard port own already-landed "Negative-spec" note --
returns a named-teammate id RAW/unchanged rather than the canonical
``<name>@session-<short-session-id>`` form ``resolve_subagent_identity``
produces. Using the simplified resolver here would look up the wrong
back-pointer directory for named-teammate dispatches and silently
under-block. This module therefore imports the ALREADY-VERIFIED-CORRECT
``_resolve_subagent_identity`` straight from the write-sibling port (reuse,
not a second re-implementation) rather than the engine.py re-export the
recipe names as the default -- see "Open risk" in the W3b return for the
recipe-vs-disk discrepancy this reveals. The back-pointer
``subagent_type`` lookup and ``git_root`` resolution ARE identical between
``subagent_sandbox.engine`` and the write-sibling local copies (verified by
direct comparison), so those two legs ARE reused from
``coordinator_core.bash_guards._helpers`` per the recipe instruction.

ADVISORY_REWRITE note (2026-08-06): `check()` no longer returns a deny
envelope. C13 moved this guard's `dispatch.py` registration to
`GuardBand.ADVISORY_REWRITE` with `fail_closed=False`; this module (C14c)
follows on the return-vocabulary side -- an unambiguous-write match now
returns `permissionDecision: "allow"` with the same reason text surfaced via
`additionalContext` instead of `permissionDecisionReason`. `CLASS =
"hard-deny"` above is dead metadata on the bash-guard side (nothing reads
it) and is left as historical record, not the load-bearing signal.

Spec backlink: DoE-claude:pln-dispatch-sidecar-contract-exec-5e045c
  section D-BASH, AC4, chunk C-BASH.
Spec backlink (ADVISORY_REWRITE conversion):
  docs/plans/2026-08-06-apply-guard-class-census.md, chunk C14.
Ported from the retired DoE bash guard ``block-subagent-plan-body-bash-write.sh``
  (deleted 2026-07-16, DoE ``2f8b8450``).
Recipe: scratch/subagent-sandbox/bash-to-python-migration/W3a-preuse-bash-recipe.md
  section (b) fold-candidate 1.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._dialect import Dialect, dialect_from_tool_name
from coordinator_core.bash_guards._helpers import (
    resolve_git_root,
    _read_backpointer_subagent_type,
    emit_kind_resolution_failure_signal,
    operator_override_note,
)
from coordinator_core.bash_guards._verdict import record_silent
from coordinator_core.write_guards.block_subagent_plan_body_write import (
    _resolve_subagent_identity,
)
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.bash_guards._override_log_path import (
    session_audit_log_dir,
)

# DEFERRED, NOT a module-level import (2026-08-13 hot-path import-budget fix,
_UNRESOLVED = object()
resolve_roster = _UNRESOLVED  # type: ignore[assignment]


def _resolve_roster_accessor():
    global resolve_roster
    if resolve_roster is _UNRESOLVED:
        from coordinator_core.hooks.block_unenumerated_agent_type import (
            resolve_roster as _imported_resolve_roster,
        )

        resolve_roster = _imported_resolve_roster
    return resolve_roster


CLASS = "hard-deny"
# Widened 2026-08-19 (subagent-boundary MATCHERS parity, see
# on `Dialect.POWERSHELL` internally (`dialect_from_tool_name`) -- this
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 40

GENERATES = []

#: line 65), so it also bypasses the AMBIGUOUS unconditional-deny branch.
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_SUBAGENT_PLAN_BODY"

_EXECUTOR_TYPE = "coordinator:executor"

_AMBIGUOUS_SENTINEL = "AMBIGUOUS"

_PLAN_PATH_FRAGMENT = r"([A-Za-z0-9_./-]*/)?docs/plans/[A-Za-z0-9_.-]+\.md"

_NO_SEP_NO_QUOTE = r"[^;&|\n\"']*"

_CMD_POS_ANCHOR = r"(^|[\s]*[;&|][\s]*)"

_REDIRECT_RE = re.compile(r">>?\s*" + _PLAN_PATH_FRAGMENT)

_SED_VERB_RE = (
    r"sed[\s]+((-[A-Za-z]*|--[A-Za-z-]+(=[^\s]*)?)[\s]+)*"
    r"(-i|--in-place(=[^\s]*)?)"
)
_SED_RE = re.compile(
    _CMD_POS_ANCHOR + _SED_VERB_RE + r"[\s=]" + _NO_SEP_NO_QUOTE + _PLAN_PATH_FRAGMENT
)

_TEE_RE = re.compile(_CMD_POS_ANCHOR + r"tee[\s]" + _NO_SEP_NO_QUOTE + _PLAN_PATH_FRAGMENT)

_CP_MV_DD_RE = re.compile(
    _CMD_POS_ANCHOR + r"(cp|mv|dd)[\s]" + _NO_SEP_NO_QUOTE + _PLAN_PATH_FRAGMENT
)


_CMD_WHITESPACE_CTRL_RE = re.compile(r"[\t\r\n\f\v]")
_CMD_C0_CTRL_RE = re.compile(r"[\x00-\x1f]")


def _sanitize_cmd_for_reason(cmd: str) -> str:
    """Port of CMD_SAFE (reference hook 266-267)."""
    safe = _CMD_WHITESPACE_CTRL_RE.sub(" ", cmd)
    return _CMD_C0_CTRL_RE.sub("", safe)


def _write_block_log(git_root: Optional[str], session_id: str, agent_id: str) -> None:
    if not session_id or not git_root:
        return
    try:
        resolved = session_audit_log_dir(git_root, session_id)
        if resolved is None:
            return
        log_dir = Path(resolved)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(log_dir / "plan-body-bash-write-block.log", "a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{ts} | DENY | agent_id={agent_id} | bash-command-plan-write\n")
    except OSError as exc:
        print(
            f"block-subagent-plan-body-bash-write: failed to write deny audit "
            f"log under {git_root}: {exc}",
            file=sys.stderr,
        )


def _deny_reason_ambiguous(
    agent_id: str,
    cmd_safe: str,
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> str:
    """Compressed port of the AMBIGUOUS-branch REASON (reference hook)."""
    _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload, git_root=git_root)
    return (
        "BLOCKED: ambiguous agent identity — canonical-id collision, failing closed.\n\n"
        "Two dispatches shared one id with different subagent_types. Ask the EM to\n"
        "re-dispatch cleanly."
        + ("\n\n" + _note if _note else "")
    )


def _deny_reason_executor(
    agent_id: str,
    cmd_safe: str,
    subagent_type: str = _EXECUTOR_TYPE,
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> str:
    _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload, git_root=git_root)
    if subagent_type != _EXECUTOR_TYPE:
        return (
            f"BLOCKED: subagent_type {subagent_type!r} is not on coordinator's\n"
            "enumerated agent roster, so it can't write docs/plans/*.md via Bash.\n\n"
            "Status stamps use instead:\n"
            "  .coordinator-local/subagent-share/<path>.md (report_sidecar)\n\n"
            "Type is legitimate? It belongs on the roster. Body edit was your\n"
            "deliverable? Ask the EM to dispatch an enumerated kind."
            + ("\n\n" + _note if _note else "")
        )
    return (
        "BLOCKED: coordinator:executor can't write docs/plans/*.md via Bash.\n\n"
        "Status stamps use instead:\n"
        "  .coordinator-local/subagent-share/<path>.md (report_sidecar)\n\n"
        "Body edit was your deliverable? Wrong agent — ask the EM to route to\n"
        "enricher/review-integrator."
        + ("\n\n" + _note if _note else "")
    )


def _has_unambiguous_write(cmd_norm: str) -> bool:
    """Port of the TARGET-DETECTION axis idioms 1-4 (reference hook 177-247).

    Any doubt -> False (allow). Only an idiom match returns True.
    """
    if _REDIRECT_RE.search(cmd_norm):
        return True
    if _SED_RE.search(cmd_norm):
        return True
    if _TEE_RE.search(cmd_norm):
        return True
    if _CP_MV_DD_RE.search(cmd_norm):
        return True
    return False


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the plan-body-bash-write guard against a PreToolUse payload.

    Returns ``None`` (allow) or the nested hard-deny envelope. Fails open
    (returns ``None``) on any missing/unparseable field or lookup-miss,
    matching the reference hook's ``-e``-omitted, fail-open-on-error
    contract for the target-detection axis, and the identity axis's own
    fail-CLOSED-on-AMBIGUOUS/coordinator:executor-only semantics.
    """
    # below, including the AMBIGUOUS unconditional-deny branch.
    if os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
        return None

    # via PowerShell -- see the TARGET-DETECTION axis below for what that
    tool_name = payload.get("tool_name") or ""
    dialect = dialect_from_tool_name(tool_name)
    if dialect not in (Dialect.BASH, Dialect.POWERSHELL):
        return None

    # IDENTITY AXIS -- verbatim mirror of block-subagent-plan-body-write.sh
    cwd = payload.get("cwd")

    raw_agent_id = payload.get("agent_id") or ""

    if not raw_agent_id:
        return None

    session_id = payload.get("session_id") or ""
    agent_id = _resolve_subagent_identity(raw_agent_id, session_id)

    tool_input = payload.get("tool_input") or {}
    cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
    cmd = cmd or ""
    if not cmd:
        return None

    git_root = resolve_git_root(cwd)

    subagent_type = ""
    if agent_id and git_root:
        subagent_type = _read_backpointer_subagent_type(git_root, agent_id)

    is_ambiguous = subagent_type == _AMBIGUOUS_SENTINEL

    kind_unresolved = not is_ambiguous and not subagent_type

    # Block when SUBAGENT_TYPE is coordinator:executor OR AMBIGUOUS
    if not is_ambiguous and subagent_type != _EXECUTOR_TYPE:
        if kind_unresolved:
            emit_kind_resolution_failure_signal(
                "block_subagent_plan_body_bash_write", agent_id, git_root, None
            )
            return None
        roster, _roster_error = _resolve_roster_accessor()()
        if roster is None or subagent_type in roster:
            return None

    # TARGET-DETECTION AXIS -- independent of identity. AMBIGUOUS denies
    if is_ambiguous:
        unambiguous_write = True
    else:
        cmd_norm = cmd.replace("\r", "")
        if dialect is Dialect.POWERSHELL:
            # `_REDIRECT_RE` needs no PowerShell-specific edit and keeps
            unambiguous_write = bool(_REDIRECT_RE.search(cmd_norm))
            if not unambiguous_write:
                record_silent(
                    "block_subagent_plan_body_bash_write",
                    "PowerShell command matched no dialect-neutral idiom; "
                    "sed/tee/cp/mv/dd idioms are POSIX-only and cannot rule "
                    "out an equivalent cmdlet write (New-Item/Set-Content/"
                    "Add-Content/Copy-Item/Move-Item)",
                )
        else:
            unambiguous_write = _has_unambiguous_write(cmd_norm)

    if not unambiguous_write:
        return None

    # not a deny log (ADVISORY_REWRITE band, C13 registration); the on-disk
    _write_block_log(git_root, session_id, agent_id or raw_agent_id)

    cmd_safe = _sanitize_cmd_for_reason(cmd)

    if is_ambiguous:
        reason = _deny_reason_ambiguous(agent_id or raw_agent_id, cmd_safe, payload=payload, git_root=git_root)
    else:
        reason = _deny_reason_executor(
            agent_id or raw_agent_id, cmd_safe, subagent_type, payload=payload, git_root=git_root
        )

    # ADVISORY_REWRITE (C14c) -- allow the command through and surface the
    # AMBIGUOUS branch, reports collision state the agent could not already
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": reason,
        }
    }

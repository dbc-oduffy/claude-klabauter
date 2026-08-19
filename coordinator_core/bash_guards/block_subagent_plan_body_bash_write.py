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

# DEFERRED, NOT a module-level import (2026-08-13 hot-path import-budget fix,
# latent-infra-blocker sibling of coordinator_core/bash_guards/_helpers.py's
# `_resolve_roster_accessor`): a module-level `from coordinator_core.hooks.
# block_unenumerated_agent_type import resolve_roster` drags in the
# `coordinator_core.hooks` package `__init__`'s full eager registration into
# every `coordinator_core.bash_guards.dispatch` import, against
# `coordinator_core/benchmarks/import-budget-manifest.json`'s hot-path budget.
# Cached on this module's OWN attribute (mirrors `_helpers._resolve_roster_
# accessor` and `coordinator_core.session.core._psutil()`) so the existing
# `monkeypatch.setattr(guard, "resolve_roster", ...)` surface (see this
# package's `tests/test_block_subagent_plan_body_bash_write.py`) keeps
# working unmodified. DO NOT re-flatten to a module-level import.
_UNRESOLVED = object()
resolve_roster = _UNRESOLVED  # type: ignore[assignment]


def _resolve_roster_accessor():
    """Lazily import and cache ``resolve_roster`` on this module's own
    attribute (see the negative-spec comment above this cache's
    declaration). Returns the callable; never calls it.
    """
    global resolve_roster
    if resolve_roster is _UNRESOLVED:
        from coordinator_core.hooks.block_unenumerated_agent_type import (
            resolve_roster as _imported_resolve_roster,
        )

        resolve_roster = _imported_resolve_roster
    return resolve_roster


CLASS = "hard-deny"
# Widened 2026-08-19 (subagent-boundary MATCHERS parity, see
# docs/reference/guard-tool-name-membership.md): `check()` already branches
# on `Dialect.POWERSHELL` internally (`dialect_from_tool_name`) -- this
# guard was pre-built dialect-aware, just never reachable under the
# PowerShell tool until now.
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 40

# Generator-provenance declaration (generator_provenance.py). _write_block_log
# appends to <git_root>/.git/coordinator-sessions/<session_id>/plan-body-
# bash-write-block.log -- inside .git, an untracked per-session audit log,
# never a tracked repo artifact.
GENERATES = []

#: Escape-hatch env var -- checked BEFORE identity resolution (reference hook
#: line 65), so it also bypasses the AMBIGUOUS unconditional-deny branch.
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_SUBAGENT_PLAN_BODY"

#: subagent_type value that gates target-detection (reference hook 165).
_EXECUTOR_TYPE = "coordinator:executor"

#: AC14 collision sentinel -- unconditional deny, no carve-out (reference hook 163-167).
_AMBIGUOUS_SENTINEL = "AMBIGUOUS"

#: docs/plans/*.md path-shape fragment, tolerant of a leading path prefix
#: (reference hook).
_PLAN_PATH_FRAGMENT = r"([A-Za-z0-9_./-]*/)?docs/plans/[A-Za-z0-9_.-]+\.md"

#: Quote-free, separator-free gap between a write-verb and its own argument
#: (reference hook).
_NO_SEP_NO_QUOTE = r"[^;&|\n\"']*"

#: Command-position anchor: start of string, or after a ;/&/| separator
#: (reference hook).
_CMD_POS_ANCHOR = r"(^|[\s]*[;&|][\s]*)"

#: (1) Plain redirect: `> docs/plans/x.md` or `>> docs/plans/x.md` -- no verb
#: anchor, matches anywhere in the string (reference hook).
_REDIRECT_RE = re.compile(r">>?\s*" + _PLAN_PATH_FRAGMENT)

#: (2) sed -i / --in-place, verb command-position-anchored (reference hook).
_SED_VERB_RE = (
    r"sed[\s]+((-[A-Za-z]*|--[A-Za-z-]+(=[^\s]*)?)[\s]+)*"
    r"(-i|--in-place(=[^\s]*)?)"
)
_SED_RE = re.compile(
    _CMD_POS_ANCHOR + _SED_VERB_RE + r"[\s=]" + _NO_SEP_NO_QUOTE + _PLAN_PATH_FRAGMENT
)

#: (3) tee ... docs/plans/x.md (reference hook).
_TEE_RE = re.compile(_CMD_POS_ANCHOR + r"tee[\s]" + _NO_SEP_NO_QUOTE + _PLAN_PATH_FRAGMENT)

#: (4) cp / mv / dd targeting docs/plans/*.md (reference hook).
_CP_MV_DD_RE = re.compile(
    _CMD_POS_ANCHOR + r"(cp|mv|dd)[\s]" + _NO_SEP_NO_QUOTE + _PLAN_PATH_FRAGMENT
)

#: (5) heredoc idiom is DEAD CODE and is deliberately NOT ported (reference
#: hook / W3a recipe "do not port idiom 5 as a separate check").

#: Control-whitespace/C0-control sanitization before interpolating an
#: attacker-influenced command string into a deny reason (reference hook).
_CMD_WHITESPACE_CTRL_RE = re.compile(r"[\t\r\n\f\v]")
_CMD_C0_CTRL_RE = re.compile(r"[\x00-\x1f]")


def _sanitize_cmd_for_reason(cmd: str) -> str:
    """Port of CMD_SAFE (reference hook 266-267)."""
    safe = _CMD_WHITESPACE_CTRL_RE.sub(" ", cmd)
    return _CMD_C0_CTRL_RE.sub("", safe)


def _write_block_log(git_root: Optional[str], session_id: str, agent_id: str) -> None:
    """Best-effort per-session deny log (reference hook 255-262).

    Wrapped so any failure can NEVER flip the ALLOW/DENY decision -- mirrors
    bash's trailing ``2>/dev/null || true``.
    """
    if not session_id or not git_root:
        return
    try:
        log_dir = Path(git_root) / ".git" / "coordinator-sessions" / session_id
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(log_dir / "plan-body-bash-write-block.log", "a", encoding="utf-8") as fh:
            fh.write(f"{ts} | DENY | agent_id={agent_id} | bash-command-plan-write\n")
    except OSError as exc:
        # Best-effort audit log only -- never flips the ALLOW/DENY decision
        # (the deny itself already happened by the time this runs). Surfaced
        # because a silently-lost DENY audit entry is worth knowing about.
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
    """Compressed port of the coordinator:executor-branch REASON (reference hook).

    ``subagent_type`` names the kind that actually reached this leg; the
    ``coordinator:executor`` default preserves the ported REASON for the
    original caller. Twin of ``block_subagent_plan_body_write``'s function of
    the same name — C3 (docs/plans/2026-08-10-deny-unenumerated-agent-types-
    at-dispatch.md) routes cleanly-resolved unenumerated kinds here too, and
    "wrong agent, ask the EM to re-route" is wrong advice for those: the fix
    for an unenumerated kind is the roster, not a re-route.
    """
    _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload, git_root=git_root)
    if subagent_type != _EXECUTOR_TYPE:
        return (
            f"BLOCKED: subagent_type {subagent_type!r} is not on coordinator's\n"
            "enumerated agent roster, so it can't write docs/plans/*.md via Bash.\n\n"
            "Status stamps use instead:\n"
            "  state/subagent-share/<path>.md (report_sidecar)\n\n"
            "Type is legitimate? It belongs on the roster. Body edit was your\n"
            "deliverable? Ask the EM to dispatch an enumerated kind."
            + ("\n\n" + _note if _note else "")
        )
    return (
        "BLOCKED: coordinator:executor can't write docs/plans/*.md via Bash.\n\n"
        "Status stamps use instead:\n"
        "  state/subagent-share/<path>.md (report_sidecar)\n\n"
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
    # Honor escape hatch first (reference hook 64-67) -- bypasses everything
    # below, including the AMBIGUOUS unconditional-deny branch.
    if os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
        return None

    # Tool-name guard -- this hook fires on Bash (reference hook 85-94) and,
    # per C5's PowerShell conversion, on the dialect-neutral residue reached
    # via PowerShell -- see the TARGET-DETECTION axis below for what that
    # residue is and where it records SILENT instead of a guess. Any other
    # tool_name (including unrecognised) is out of this guard's remit
    # entirely, same as before this conversion.
    tool_name = payload.get("tool_name") or ""
    dialect = dialect_from_tool_name(tool_name)
    if dialect not in (Dialect.BASH, Dialect.POWERSHELL):
        return None

    # ------------------------------------------------------------------
    # IDENTITY AXIS -- verbatim mirror of block-subagent-plan-body-write.sh
    # (reference hook 96-167).
    # ------------------------------------------------------------------
    cwd = payload.get("cwd")
    git_root = resolve_git_root(cwd)

    raw_agent_id = payload.get("agent_id") or ""

    # Empty RAW agent_id -> no subagent at all (EM main-loop) -> allow. This
    # is the EM/subagent discriminator this guard gates on: whether the
    # harness supplied an agent_id at all, not whether it canonicalizes.
    if not raw_agent_id:
        return None

    session_id = payload.get("session_id") or ""
    agent_id = _resolve_subagent_identity(raw_agent_id, session_id)

    # Extract the Bash command string (reference hook 132-140).
    tool_input = payload.get("tool_input") or {}
    cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
    cmd = cmd or ""
    if not cmd:
        return None

    # Subagent-type lookup via back-pointer chain (reference hook 142-158).
    subagent_type = ""
    if agent_id and git_root:
        subagent_type = _read_backpointer_subagent_type(git_root, agent_id)

    is_ambiguous = subagent_type == _AMBIGUOUS_SENTINEL

    # Lookup-fail semantics: allow -- reinstated 2026-07-30 to match the
    # sibling write_guards.block_subagent_plan_body_write, which the same
    # PM ruling (2026-06-09, "don't punish legitimate integrator/enricher
    # work on infra noise") governs. THIS guard and its write-side sibling
    # encode ONE rule across two surfaces (Bash vs. Write/Edit) -- the
    # 2026-06-09 ruling is about who may edit a plan body, not a property
    # of which tool they used to do it. A brief that confined only ONE of
    # the two surfaces on an unresolvable kind would manufacture a split
    # that never existed (an enricher blocked via Bash but not via Write,
    # or vice versa), which is worse than either consistent answer.
    # Reverting the VERDICT only; the measurement-only instrumentation
    # signal below is kept so a future, properly-scoped PM conversation
    # about the 2026-06-09 ruling has real frequency data from both
    # surfaces. Emitted at the actual exit point with the actual verdict
    # (``None``, since this branch is the ONLY reachable exit for an
    # unresolved, non-ambiguous kind) rather than a hand-passed claim.
    kind_unresolved = not is_ambiguous and not subagent_type

    # Block when SUBAGENT_TYPE is coordinator:executor OR AMBIGUOUS
    # (fail-closed, no carve-out -- reference hook 160-167). All other
    # types -- including empty/lookup-failure -- allow. AC6/C3 (mirrors the
    # write-guard sibling above, deliberate twins): "all other types" is
    # narrowed to enumerated-roster types only -- a lookup FAILURE
    # (kind_unresolved) still allows through this same exit, unchanged from
    # the 2026-06-09/2026-07-30 ruling above, but a type that resolves
    # CLEANLY to something absent from the roster (C1's own union-of-three
    # roster, coordinator_core.hooks.block_unenumerated_agent_type.
    # resolve_roster) falls through to the SAME target-detection axis below
    # rather than exiting here -- an invented type gets no more trust than
    # coordinator:executor for this guard's purposes. A roster-load error is
    # a peer-repo hiccup, not this guard's problem to newly deny on: C1's
    # PreToolUse(Agent) deny is the primary fix, so this stays defence in
    # depth and falls back to today's allow rather than denying on an
    # unresolvable roster.
    if not is_ambiguous and subagent_type != _EXECUTOR_TYPE:
        if kind_unresolved:
            emit_kind_resolution_failure_signal(
                "block_subagent_plan_body_bash_write", agent_id, git_root, None
            )
            return None
        roster, _roster_error = _resolve_roster_accessor()()
        if roster is None or subagent_type in roster:
            return None

    # ------------------------------------------------------------------
    # TARGET-DETECTION AXIS -- independent of identity. AMBIGUOUS denies
    # unconditionally regardless of target (reference hook 169-247).
    # ------------------------------------------------------------------
    if is_ambiguous:
        unambiguous_write = True
    else:
        # Normalize CRLF -> LF (reference hook 180-182).
        cmd_norm = cmd.replace("\r", "")
        if dialect is Dialect.POWERSHELL:
            # Only idiom (1) is dialect-neutral: `>`/`>>` are the SAME
            # operator characters in PowerShell as in POSIX, so
            # `_REDIRECT_RE` needs no PowerShell-specific edit and keeps
            # ruling correctly. Idioms (2)-(4) (`sed -i`, `tee`, `cp`/`mv`/
            # `dd`) are POSIX verbs with no recognized PowerShell cmdlet
            # equivalent here (`New-Item`, `Set-Content`, `Add-Content`,
            # `Copy-Item`, `Move-Item`) -- a PowerShell command that misses
            # idiom (1) is NOT a confirmed clean verdict, it is unparsed
            # residue, so it is recorded SILENT (per AC1/AC3) rather than
            # silently returned as a clean "no write detected" the way the
            # bash leg's own doubt->allow default would otherwise read.
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

    # Any doubt -> fail OPEN (allow). Only an unambiguous match denies
    # (reference hook 249-250).
    if not unambiguous_write:
        return None

    # Match confirmed. Per-session log (best-effort) (reference hook 252-262).
    # Retained under its historical name -- this is now an advisory-fire log,
    # not a deny log (ADVISORY_REWRITE band, C13 registration); the on-disk
    # audit trail semantics (best-effort, never flips the verdict) are
    # unchanged.
    _write_block_log(git_root, session_id, agent_id or raw_agent_id)

    cmd_safe = _sanitize_cmd_for_reason(cmd)

    if is_ambiguous:
        reason = _deny_reason_ambiguous(agent_id or raw_agent_id, cmd_safe, payload=payload, git_root=git_root)
    else:
        reason = _deny_reason_executor(
            agent_id or raw_agent_id, cmd_safe, subagent_type, payload=payload, git_root=git_root
        )

    # ADVISORY_REWRITE (C14c) -- allow the command through and surface the
    # advisory in `additionalContext` rather than denying via
    # `permissionDecisionReason`. The message text itself is unchanged: it
    # already names a concrete, applicable alternative (the
    # `state/subagent-share/<path>.md` sidecar surface) or, for the
    # AMBIGUOUS branch, reports collision state the agent could not already
    # know -- both satisfy the Axis-A firing-shape gate as advisory prose,
    # not just as a deny reason.
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": reason,
        }
    }

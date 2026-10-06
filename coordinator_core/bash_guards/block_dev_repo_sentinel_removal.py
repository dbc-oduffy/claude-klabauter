"""coordinator_core.bash_guards.block_dev_repo_sentinel_removal --
PreToolUse (Bash) guard protecting `.coordinator-dev-repo` (the coordinator-content-repo
repo-root dev-vs-OSS discriminant) from Bash-level removal or relocation.

WHY THIS EXISTS. `.coordinator-dev-repo`'s mere PRESENCE at the repo root is
consumed by `coordinator_core.claude_md_budget` (`DEV_REPO_SENTINEL`) and
`coordinator_core.resolve_coordinator_clone` to tell the dev doctrine repo
apart from an OSS install, and is written at install time by
`coordinator_core.install.maximalist`. Deleting or relocating it away from
the repo root destroys that discriminant fleet-wide -- with NO error at the
moment of the move; the breakage surfaces later, silently, in an unrelated
session that resolves the wrong dev/OSS branch of logic. This is the
opposite failure shape from the existing sentinel-creation guards in this
package (`block_approval_sentinel_creation.py`,
`block_worktree_sentinel_creation.py`,
`block_disarm_marker_sentinel_creation.py`), each of which protects a
sentinel whose ABSENCE gates a capability, so THEY block creation and
explicitly leave removal out of scope. This guard is that family's mirror
image, built on the mirror-image detector, `_sentinel_removal_guard.
SentinelRemovalDetector` (see that module's own docstring for the full rule
set and, importantly, its POSTURE section).

LEGS. `check` (CONFINEMENT_DENY, `fail_closed=True`) denies on the detector's
`VERDICT_DENY`, a direct match; the policy point downgrades that deny to an
advisory on a consumer box. `check_advisory` (ADVISORY_REWRITE) advises on
`VERDICT_ADVISORY`: unexaminable indirection, or an unparseable command that
only textually mentions the sentinel.

OVERRIDE. `COORDINATOR_OVERRIDE_DEV_REPO_SENTINEL=1` allows unconditionally
(both `check` and `check_advisory`) -- advertised in the advisory text
itself.

NOT IDENTITY-GATED -- fires for every caller, EM included, same posture as
the sibling sentinel guards in this package: the anti-pattern (an agent
quietly destroying the dev/OSS discriminant) is wrong regardless of who
types it.

REGISTRATION ORDERING. Both legs sit ahead of `offer-git-c`, same
reasoning as every sibling sentinel guard: that check rewrites `cd <dir> && git <sub>` into
`git -C <dir> <sub>` and returns allow+updatedInput, which SHORT-CIRCUITS
every later guard in the chain, so an entry surfacing `cd <dir> && rm
.coordinator-dev-repo` (or `cd <dir> && git rm .coordinator-dev-repo`)
must sit ahead of it too.

Spec: `.coordinator-dev-repo` removal guard (coordinator-content-repo dispatch, 2026-07-31).
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from coordinator_core._hook_envelope import deny
from coordinator_core.bash_guards._dialect import Dialect, dialect_from_tool_name
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards._sentinel_creation_guard import indirection_deny_reason
from coordinator_core.bash_guards._sentinel_removal_guard import (
    REASON_INDIRECTION,
    VERDICT_ADVISORY,
    VERDICT_DENY,
    SentinelRemovalDetector,
)
from coordinator_core.bash_guards._verdict import record_silent
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "hard-deny"
#: Widened 2026-08-07 (C4f, `docs/plans/2026-08-07-guards-reach-a-verdict-
#: on-powershell-or-stay-silent.md`) -- this guard's own dialect-carry
#: (`dialect_from_tool_name(payload["tool_name"])` in `check`/
#: `check_advisory` below) now handles a PowerShell command correctly for
#: its converted legs and declines to rule (records SILENT) rather than
#: guessing where it cannot -- see `_sentinel_removal_guard.evaluate`'s own
#: docstring. Same precedent as `block_reviewer_bash_outside_allowlist.py`'s
#: own MATCHERS widening (C6). A direct reference to the shared universe
#: (C2 declaration-form conversion) -- never a copy or re-wrap.
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 42

_TARGET_BASENAME = ".coordinator-dev-repo"

_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_DEV_REPO_SENTINEL"

_detector = SentinelRemovalDetector(_TARGET_BASENAME)

#: Guard identity threaded into `_verdict.record_silent` for the
#: absent/unrecognized-dialect leg below -- matches this guard's own
#: registered name (`check_advisory`'s dispatch entry) and
#: `_sentinel_removal_guard._GUARD_NAME`, so a SILENT declaration recorded
#: from either this module or the shared engine reads as the same guard to
#: a caller collecting declarations (`_verdict.collecting`).
_GUARD_NAME = "block-dev-repo-sentinel-removal-advisory"


def _evaluate(cmd: str, dialect: Optional[Dialect]):
    return _detector.evaluate(cmd, dialect)


def _deny_reason(
    reason_kind: str,
    reason_class: str,
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> str:
    _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload, git_root=git_root)
    if reason_class == REASON_INDIRECTION:
        safe_shape = reason_kind.replace(_TARGET_BASENAME, "<the sentinel>")
        return indirection_deny_reason("dev-repo guard", safe_shape) + (
            "\n\n%s" % _note if _note else ""
        )
    return (
        "[dev-repo guard] BLOCKED: instead, ask the EM/PM to run this if "
        "intended -- removing or moving the dev-vs-OSS discriminant file "
        "silently breaks tooling fleet-wide."
    ) + ("\n\n%s" % _note if _note else "")


def _advisory_reason(
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> str:
    _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload, git_root=git_root)
    return (
        "[dev-repo guard] ADVISORY: not blocked. This command may remove "
        "or relocate the dev/OSS discriminant sentinel; recoverable by "
        "hand -- if unintended, restore or recreate it."
    ) + ("\n\n%s" % _note if _note else "")


def _cmd_from_payload(payload: Dict[str, Any]) -> str:
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return ""
    return cmd.replace("\r", "")


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Deny-or-None leg. Never identity-gated -- fires for every caller
    including the main-loop EM.
    """
    cmd = _cmd_from_payload(payload)
    if not cmd:
        return None

    if os.environ.get(_OVERRIDE_ENV_VAR) == "1":
        return None

    dialect = dialect_from_tool_name(payload.get("tool_name"))
    if dialect is None:
        record_silent(
            _GUARD_NAME,
            "no recognized dialect (tool_name=%r) -- declined to rule"
            % (payload.get("tool_name"),),
        )
        return None

    verdict, reason_kind, reason_class = _evaluate(cmd, dialect)
    if verdict != VERDICT_DENY:
        return None

    return deny("PreToolUse", _deny_reason(reason_kind, reason_class, payload=payload))


def check_advisory(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Advisory leg: advisory on `VERDICT_ADVISORY`, `None` otherwise (the
    deny leg, `check`, owns `VERDICT_DENY`). Never identity-gated.
    """
    cmd = _cmd_from_payload(payload)
    if not cmd:
        return None

    if os.environ.get(_OVERRIDE_ENV_VAR) == "1":
        return None

    dialect = dialect_from_tool_name(payload.get("tool_name"))
    if dialect is None:
        record_silent(
            _GUARD_NAME,
            "no recognized dialect (tool_name=%r) -- declined to rule"
            % (payload.get("tool_name"),),
        )
        return None

    verdict, _reason_kind, _reason_class = _evaluate(cmd, dialect)
    if verdict != VERDICT_ADVISORY:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": _advisory_reason(payload=payload),
        }
    }

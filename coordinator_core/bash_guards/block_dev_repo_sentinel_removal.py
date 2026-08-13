"""coordinator_core.bash_guards.block_dev_repo_sentinel_removal --
PreToolUse (Bash) guard protecting `.coordinator-dev-repo` (the coordinator-claude
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

CLASS-CENSUS CONVERSION (2026-08-06, `docs/plans/2026-08-06-apply-guard-
class-census.md`, chunks C13/C14e) -- SUPERSEDES the former TWO-LEG SPLIT
(2026-08-05, mirrors `check_destructive_git_revert` /
`check_destructive_git_revert_advisory` in `dispatch_checks.py`, commit
`e63c42e39`). `check()` below is STILL the same pure deny-or-None function,
still directly callable and unit-tested, but C13 retired its `dispatch.py`
CONFINEMENT_DENY registration: it is no longer reachable through the
registered chain. `check_advisory()` is now the guard's SOLE registered
leg (ADVISORY_REWRITE band, ahead of `offer-git-c`'s rewrite), widened to
render on BOTH the detector's `VERDICT_ADVISORY` and its former-deny
`VERDICT_DENY` outcome -- so every shape that used to hard-deny now
produces an advisory instead of a silent, contentless allow. `_detector.
evaluate(cmd)` is pure string analysis (no subprocess oracle), so `check`
and `check_advisory` simply call it independently.

POSTURE -- ADVISORY, NOT DENY (the guard's ENTIRE posture as of the
conversion above, not merely its ambiguous cases). This guard defends
against ordinary eagerness in a busy shared repo: `rm`, `mv`, and `git mv`
are extremely common commands, this sentinel is one specific dotfile among
thousands of ordinary targets, and a removal that slips through remains
recoverable by hand -- the rationale the class census flipped this guard
on. A DIRECT match against the sentinel's own basename (as a plain
argument to `rm`/`unlink`/`mv` (source)/`git rm`/`git mv` (source)/`find
-delete`/`find -exec rm`, or a `python -c` payload that both mentions the
basename and calls a removal/move verb) and genuinely unexaminable
indirection (`xargs`, a bare-interpreter-invoked script file, a
stdin-piped interpreter, the indirection depth cap, or an unparseable
command that only TEXTUALLY mentions the sentinel) both now surface the
SAME advisory -- it never blocks, it surfaces the concern and names how to
recover.

OVERRIDE. `COORDINATOR_OVERRIDE_DEV_REPO_SENTINEL=1` allows unconditionally
(both `check` and `check_advisory`) -- advertised in the advisory text
itself.

NOT IDENTITY-GATED -- fires for every caller, EM included, same posture as
the sibling sentinel guards in this package: the anti-pattern (an agent
quietly destroying the dev/OSS discriminant) is wrong regardless of who
types it.

REGISTRATION ORDERING. `check_advisory` is registered in `dispatch.py`'s
ADVISORY_REWRITE band ahead of `offer-git-c`, same reasoning as every
sibling sentinel guard: that check rewrites `cd <dir> && git <sub>` into
`git -C <dir> <sub>` and returns allow+updatedInput, which SHORT-CIRCUITS
every later guard in the chain, so an entry surfacing `cd <dir> && rm
.coordinator-dev-repo` (or `cd <dir> && git rm .coordinator-dev-repo`)
must sit ahead of it too.

Spec: `.coordinator-dev-repo` removal guard (coordinator-claude dispatch,
2026-07-31; deny-to-advisory conversion 2026-08-06).
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._dialect import Dialect, dialect_from_tool_name
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards._sentinel_removal_guard import (
    REASON_INDIRECTION,
    VERDICT_ADVISORY,
    VERDICT_ALLOW,
    VERDICT_DENY,
    SentinelRemovalDetector,
)
from coordinator_core.bash_guards._verdict import record_silent
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

# `_evaluate()` never returns VERDICT_ALLOW alongside content, so anything
# other than VERDICT_ALLOW is advisory-worthy on the now-single (advisory)
# leg -- see this module's own updated "TWO-LEG SPLIT" docstring section.
_ADVISORY_VERDICTS = (VERDICT_ADVISORY, VERDICT_DENY)

CLASS = "advisory"
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

#: The exact basename this guard protects. Never relaxed to a substring/
#: prefix match.
_TARGET_BASENAME = ".coordinator-dev-repo"

#: Escape hatch, advertised in the deny/advisory text itself (offer-shaped
#: -- names what to do instead, not a bare block).
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_DEV_REPO_SENTINEL"

#: Shared detection engine -- see `_sentinel_removal_guard.py` module
#: docstring.
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
    # Deliberately does NOT echo `cmd` and does NOT name the target
    # basename -- same message-safety discipline as the sibling sentinel
    # guards (an eager agent reading its own bypass in a deny message
    # treats it as sanctioned).
    _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload, git_root=git_root)
    if reason_class == REASON_INDIRECTION:
        safe_shape = reason_kind.replace(_TARGET_BASENAME, "<the sentinel>")
        return (
            "[dev-repo guard] BLOCKED: this command was denied because its "
            "payload is delivered through an interpreter, stdin, or "
            "command-assembly indirection this guard cannot examine.\n\n"
            "Instead: run its underlying steps directly (not through an "
            "interpreter/stdin/xargs wrapper) so this guard can see them, "
            "or, if this genuinely does not touch the dev-repo discriminant "
            "sentinel, ask the EM/PM to run it.\n\n"
            "Detected shape: %s"
            % (safe_shape,)
        ) + ("\n\n%s" % _note if _note else "")
    return (
        "[dev-repo guard] BLOCKED: instead, confirm this removal/relocation "
        "is intentional and ask the EM/PM to run it -- this command would "
        "remove or relocate a file whose mere presence is the dev-vs-OSS "
        "discriminant this repo's tooling relies on; removing or moving it "
        "away from the repo root breaks that discriminant fleet-wide with "
        "no error at the moment of the move, only later, in an unrelated "
        "session."
    ) + ("\n\n%s" % _note if _note else "")


def _advisory_reason(
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> str:
    # Deliberately short (Axis-A/prose-cap discipline) and never names the
    # target basename (message-safety discipline, same as `_deny_reason`)
    # -- covers both a direct-match input (formerly this guard's deny leg,
    # widened here into the sole advisory leg) and genuine indirection.
    _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload, git_root=git_root)
    return (
        "[dev-repo guard] ADVISORY: not blocked. This command may remove "
        "or relocate the dev/OSS discriminant sentinel; recoverable by "
        "hand -- if unintended, restore or recreate it."
    ) + ("\n\n%s" % _note if _note else "")


def _cmd_from_payload(payload: Dict[str, Any]) -> str:
    """Extract the raw command text, no longer gated on `tool_name ==
    "Bash"` (AC2/C4): dialect resolution now happens separately, in
    `check`/`check_advisory` below, via `_dialect.dialect_from_tool_name` --
    an unrecognized `tool_name` still ends up as an allow, just via the
    SILENT/declined-to-rule path rather than a bare empty-command short
    circuit here. A payload with no `tool_input.command` at all (any
    `tool_name`, e.g. a non-Bash/non-PowerShell tool like `Edit`) still
    returns `""` and neither `check` nor `check_advisory` reaches the
    dialect check for it."""
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return ""
    return cmd.replace("\r", "")


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pure deny-or-None function, UNCHANGED, but no longer registered in
    `dispatch.py` as of C13 (see this module's own "CLASS-CENSUS
    CONVERSION" docstring section) -- not reachable through the live
    dispatch chain, only directly callable (unit-tested). `check_advisory`
    is the guard's sole registered leg.

    Returns `None` (allow) or the nested hard-deny envelope. Never
    identity-gated -- fires for every caller including the main-loop EM.
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

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _deny_reason(reason_kind, reason_class, payload=payload),
        }
    }


def check_advisory(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The guard's sole registered leg (ADVISORY_REWRITE band, ahead of
    `offer-git-c`'s rewrite) since C13 (`docs/plans/2026-08-06-apply-guard-
    class-census.md`) retired `check`'s CONFINEMENT_DENY registration --
    see that removal's comment at its old `dispatch.py` call site, and this
    module's own "TWO-LEG SPLIT" docstring section (now a MISNOMER kept for
    history; there is one leg).

    Renders on BOTH `VERDICT_ADVISORY` and `VERDICT_DENY` -- widened here so
    every shape the deleted deny leg used to cover still produces an
    advisory instead of a silent, contentless allow. `check` above is
    unchanged and still callable directly (exercised by unit tests), but is
    no longer reachable through the registered dispatch chain.

    Returns `None` (allow, no comment) or the nested allow+additionalContext
    advisory envelope. Never identity-gated, same posture as `check`.
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
    if verdict not in _ADVISORY_VERDICTS:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": _advisory_reason(payload=payload),
        }
    }

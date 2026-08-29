"""coordinator_core.write_guards.block_fleet_delegation_write — hard-deny
guard: the fleet-delegation grant file is not hand-authorable through any
file-write tool, for any caller class.

Purpose: docs/plans/2026-08-28-the-ask-the-pm-step-gets-an-artifact-to-
check.md, chunk C3. The ask-the-PM routing decision (C1/C2) reads a live,
unexpired grant from ``<settings_home()>/fleet-delegation.json`` to decide
whether an "ask the PM" step routes to a designated peer session or to the
human. That resolver's own call signature admits no input by which a peer
could assert the relay (the plan's Exit criterion) — but that guarantee
only holds if the grant file itself cannot be hand-authored through a
write tool. Nothing upstream of this module gated a plain ``Write``/
``Edit``/``MultiEdit``/``NotebookEdit`` against that path; this module
closes that surface.

Deliberately UNCONDITIONAL — no ``agent_id`` gate, no ``subagent_type``
narrowing, no escape-hatch env var. The EM is not exempted: unlike
``block_subagent_grant_record_write`` (whose allow leg (1) is "no
``agent_id`` -> EM-inline write -> allow", because the EM IS the
legitimate acquirer of that grant), the fleet-delegation grant here has no
legitimate write-tool acquirer at all — it is produced by a human-run CLI,
never by an agent process of any kind. Modeled instead on
``block_worktree_sentinel_write.py``'s shape: one fixed target, no
identity gate, deny is unconditional for every caller class, exactly as
that sibling and ``block_approval_sentinel_creation``-class guards
withhold one, and for the same reason — an override a subagent (or the EM
itself) could set on its own defeats the whole point of gating the
decision on independent, human-issued state.

PATH-RESOLUTION aspect modelled on
``block_subagent_grant_record_write.py``'s shape (per this chunk's own
brief): the candidate ``file_path``/``notebook_path`` is normalized
(backslash -> slash), resolved to an absolute form against ``cwd`` when
relative, lexically collapsed via ``posixpath.normpath`` (no filesystem
access, no symlink following, safe on the PreToolUse hot path), then
case-folded via ``_case_fold_path.casefold_path`` — same target the fixed
``<settings_home()>/fleet-delegation.json`` path is independently
resolved, normalized, and case-folded through. Containment/equality is
checked on the two RESOLVED, CASE-FOLDED strings — never a lexical/regex
match on the raw ``file_path``. Both sides go through the identical
normalize -> collapse -> casefold pipeline so a differently-cased or
``..``-bearing candidate that resolves onto the real grant file still
denies, and one that resolves anywhere else correctly allows.

``settings_home()`` is read from ``coordinator_core._settings_home`` — the
bootstrap-safe, zero-external-call resolver (pure env/home precedence, no
subprocess) — not ``coordinator/lib/settings_home.py``'s CLI-adjacent
sibling, per this repo's contract that ``coordinator_core`` owns guard
logic and never shells out on the PreToolUse hot path.

Message register per docs/wiki/guard-messaging.md § Register: one fact,
stated once, plus the terse sanctioned alternative
(``coordinator-delegation grant ...``, run by the human) — no
self-legitimacy (B1), no repeated claim (B2), no reassurance wrapper (B3),
no apology (B4), and no bypass key (B6) — there is no override for this
guard to name. Also no sentence asserting the file "cannot be forged":
that claim describes what OTHER layers do or don't verify, not what this
guard denies, and is out of scope for a WHAT-HAPPENED/WHAT-TO-DO-INSTEAD
message.

Negative-spec:
  - Does NOT gate on ``agent_id`` or ``subagent_type`` — the deny fires
    identically whether the caller is the EM's own top-level session or a
    dispatched subagent of any type. See "Deliberately UNCONDITIONAL"
    above.
  - Does NOT read any escape-hatch environment variable — no
    ``COORDINATOR_OVERRIDE_*`` leg exists for this guard, unlike several
    write_guards siblings that carry one (see
    ``docs/reference/guard-override-keys.md``); adding one would reopen
    exactly the self-grant hole this module exists to close.
  - Does NOT block removal (``rm``) or reads of the grant file — no
    deletion-shaped tool is in ``MATCHERS``; only
    Write/Edit/MultiEdit/NotebookEdit are guarded.
  - Does NOT match on a bare filename/basename check — the full resolved,
    case-folded path must equal the resolved, case-folded
    ``<settings_home()>/fleet-delegation.json``, so an unrelated
    ``fleet-delegation.json`` living elsewhere on disk is NOT caught (no
    over-broad basename-only match).
  - Does NOT name any override incantation, CLI env var, or claim of
    unforgeability in its deny text (see "Message register" above).

Spec backlink:
  docs/plans/2026-08-28-the-ask-the-pm-step-gets-an-artifact-to-check.md
  § chunk C3.
Precedent (unconditional, no-identity-gate shape):
  coordinator_core/write_guards/block_worktree_sentinel_write.py
Precedent (path-resolution shape, per this chunk's brief):
  coordinator_core/write_guards/block_subagent_grant_record_write.py
"""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.write_guards._case_fold_path import casefold_path

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]

#: PRIORITY 49 -- unique within the HARD-DENY phase (checked against the
#: full set of hard-deny PRIORITY values at HEAD this session: 5, 10, 20,
#: 30, 40, 45, 46, 47, 48, 50, 56, 65, 76, 129, 130, 131, 132, 135, 136,
#: 137 taken -- 47 and 48 already claimed by block_subagent_guard_grant_
#: write.py and block_confined_agent_write.py respectively). Slotted
#: immediately after those two nearest siblings in the same grant-adjacent
#: artifact family, ahead of block_consumed_handoff_edit (50). The phase
#: runs first-non-None-wins, so relative order among non-overlapping-path
#: guards has no behavioral effect here -- this is a readability/grouping
#: choice only.
PRIORITY = 49

_INTERCEPTED_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

#: The fixed target filename this guard protects, under settings_home().
_GRANT_FILENAME = "fleet-delegation.json"

_DENY_REASON = (
    "BLOCKED: the fleet-delegation grant file is not hand-authorable.\n"
    "Alternative: run `coordinator-delegation grant ...` (human-run) to "
    "issue a grant instead."
)


def _extract_file_path(payload: Dict[str, Any]) -> str:
    """``file_path``, falling back to ``notebook_path`` for NotebookEdit."""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    return tool_input.get("file_path") or tool_input.get("notebook_path") or ""


def _normalize_slashes(file_path: str) -> str:
    """Backslash -> forward-slash, no other transform -- mirrors the first
    step of ``block_subagent_grant_record_write._normalize_path`` (this
    module has no UNC-preservation need: neither ``settings_home()`` nor
    any realistic ``cwd`` for this guard's callers is a UNC share, and
    ``casefold_path`` downstream already strips a Windows extended-length
    prefix on both sides symmetrically).
    """
    return file_path.replace("\\", "/")


def _is_absolute(normalized: str) -> bool:
    return normalized.startswith("/") or (
        len(normalized) >= 2 and normalized[1] == ":"
    )


def _resolve_candidate(file_path: str, cwd: Optional[str]) -> str:
    """Resolve ``file_path`` to an absolute, forward-slash form, joining
    against ``cwd`` when relative, then lexically collapse ``.``/``..``
    segments via ``posixpath.normpath`` (pure string manipulation, no
    filesystem access, no spawn -- safe on the PreToolUse hot path).
    """
    normalized = _normalize_slashes(file_path)
    if not _is_absolute(normalized):
        base = cwd or "."
        normalized = _normalize_slashes(str(Path(base) / normalized))
    return posixpath.normpath(normalized)


def _target_path() -> str:
    """Resolve ``<settings_home()>/fleet-delegation.json`` through the same
    normalize -> collapse pipeline the candidate goes through, so both
    sides are comparable strings.
    """
    joined = _normalize_slashes(str(settings_home() / _GRANT_FILENAME))
    return posixpath.normpath(joined)


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the fleet-delegation grant-file write guard against a
    PreToolUse payload.

    Returns ``None`` (allow) or the nested hard-deny envelope. No
    ``agent_id``/``subagent_type`` gate -- see module docstring
    "Deliberately UNCONDITIONAL".
    """
    tool_name = payload.get("tool_name") or ""
    if tool_name not in _INTERCEPTED_TOOLS:
        return None

    file_path = _extract_file_path(payload)
    if not file_path:
        return None

    cwd = payload.get("cwd")
    candidate = casefold_path(_resolve_candidate(file_path, cwd))
    target = casefold_path(_target_path())

    if candidate != target:
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _DENY_REASON,
        }
    }

"""coordinator_core.write_guards.nudge_shell_shaped_spawn — advisory guard.

Write-time ergonomic surface for the shell-spawn regrowth gate
(`docs/plans/2026-08-06-shell-spawn-regrowth-gate.md`, chunk C5). The test
gate (`coordinator_core/tests/test_no_unsanctioned_shell_spawn.py`, C3) is
the teeth of this workstream; this guard is the offer that makes the
naked-Python path cheaper than a shell-shaped spawn BEFORE the write even
lands, so an author never has to round-trip through a red test to learn a
carve-out exists (or doesn't).

CLASS is "advisory" — per this repo's north star (project CLAUDE.md
"ergonomics over enforcement"), the standing carve-out for a hard block is
irreversible harm, and an unsanctioned shell-shaped spawn is not that: C3's
test gate is the actual enforcement, this module simply makes the correct
path visible earlier. A guard that asks an eager model to refuse work it
wants to do is advisory in practice anyway (project CLAUDE.md § North
star) — so this module is honest about that and leans on being USEFUL
instead of on the word "deny".

DETECTION reuses `coordinator_core.spawn_policy.sites_in_source` (C1,
`tasks/shell-spawn-regrowth-gate/PINNED-API.md`) — the single source of
truth for "is this call site a shell-shaped spawn." `sites_in_source` is
TEXT-based (source string in, sites out, no disk access), which is exactly
why this module reconstructs the POST-EDIT whole-file buffer before calling
it rather than scanning a Write/Edit fragment: a fragment cannot see the
full enclosing scope `sites_in_source` needs to compute `enclosing`/
`ordinal`, and mid-file `Edit`s would otherwise be invisible to the AST
walk.

WHOLE-FILE RECONSTRUCTION is REUSED, not re-derived, from the live sibling
`nudge_windows_subprocess_popup` (DR-077 part 1): `_read_file_safely`,
`_apply_one_edit`, and the `Write`/`Edit`/`MultiEdit` old_string -> new_string
replay are imported directly from that module rather than copied, since this
guard's fidelity requirement ("did the edit really produce this text") is
identical to DR-077's and importing keeps the two in lockstep by
construction rather than by hand-sync discipline. The two guards' CLASSIFIER
purposes stay separate (popup-suppression vs. shell-spawn shape) — only the
reconstruction plumbing is shared, per this workstream's C5 brief.

MESSAGE ORDER (per the C5 brief, "lead with the alternative"):
  1. the naked-Python equivalent for the SPECIFIC kind detected,
  2. the carve-out path (`docs/reference/shell-out-carve-outs.md`, PM-ruling
     only) for a site that is genuinely sanctioned,
  3. only then, what was found (file/site count) — never a "You cannot..."
     opening line.

BOUNDED AND FAIL-OPEN, mirroring DR-077: single-file parse, byte-capped at
`_MAX_WHOLE_FILE_BYTES` (mirrors the popup guard's cap, falling back to no
advice — never to a fragment scan, since `sites_in_source` needs real
Python syntax and a truncated fragment would parse-error), and any
`SpawnParseError` or unexpected exception returns None (ALLOW/no-op) rather
than raising or denying. A write-time guard that raises or hangs is worse
than no guard.

Negative-spec:
  - Does NOT deny/block anything — CLASS is "advisory"; the envelope carries
    only `additionalContext`, never `permissionDecision`.
  - Does NOT itself consult the allowlist (`spawn_policy.allowlist`) to
    decide whether to fire — a site that IS sanctioned still gets the
    offer, because the offer's second line already tells the author where
    the carve-out register lives; re-deriving sanctioned/unsanctioned here
    would duplicate C3's enforcement logic in an advisory surface that must
    stay cheap and simple.
  - Does NOT fire on non-`.py` files — `sites_in_source` is an AST walk;
    there is nothing to parse in a `.sh`/`.ps1` file (that shape is
    `nudge_windows_subprocess_popup`'s and `nudge_new_sh_file_naked_python`'s
    territory, not this module's).
  - Does NOT merge with, narrow, or delete `nudge_windows_subprocess_popup`
    — only its reconstruction helper is imported; the classifier stays this
    module's own.
  - Never raises: any unexpected input shape, oversized file, or parse
    failure returns None (ALLOW/no-op).

Spec backlink: docs/plans/2026-08-06-shell-spawn-regrowth-gate.md § C5
Grep anchors: SHELL-SPAWN-REGROWTH-GATE
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from coordinator_core.spawn_policy import SpawnKind, SpawnParseError, sites_in_source
from coordinator_core.write_guards.nudge_windows_subprocess_popup import (
    _MAX_WHOLE_FILE_BYTES,
    _extract_content,
    _extract_file_path,
)

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 190  # advisory/deny-offer band; next slot after 172 (see block_dev_side_mirror_wiki.py)
# -- 180 is free (bump_out_of_repo_tool_write.py moved to the hard-deny
# band's PRIORITY 135 when its CLASS flipped; see that module's own
# PRIORITY comment).

_CARVE_OUT_DOC = "docs/reference/shell-out-carve-outs.md"

#: Naked-Python equivalent offered per SpawnKind — see module docstring
#: "MESSAGE ORDER" item 1.
_ALTERNATIVE_BY_KIND: Dict[SpawnKind, str] = {
    SpawnKind.SHELL_BINARY: (
        "instead of a bash/sh/powershell/cmd argv[0], call the target "
        "program directly as argv[0] (e.g. subprocess.run([prog, *args]))"
    ),
    SpawnKind.SHELL_TRUE: (
        "instead of shell=True/os.system, use shlex.split(cmd) and pass "
        "the resulting list as argv to subprocess.run/Popen"
    ),
    SpawnKind.PLAIN_SPAWN: (
        "prefer the stdlib equivalent for this call over a subprocess hop "
        "(e.g. shutil/os/platform/psutil) where one exists"
    ),
}


def _reason_for(kind: SpawnKind, file_path: str, site_count: int) -> str:
    alternative = _ALTERNATIVE_BY_KIND[kind]
    plural = "site" if site_count == 1 else "sites"
    return (
        f"OFFER: {alternative}.\n"
        f"Genuinely sanctioned? Extend {_CARVE_OUT_DOC} (PM ruling only) instead.\n"
        f"Found {site_count} shell-shaped spawn {plural} in {file_path}."
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit"):
            return None

        raw_file_path = _extract_file_path(payload)
        if not raw_file_path:
            return None

        file_path = raw_file_path.replace("\\", "/")
        if not file_path.endswith(".py"):
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        content = _extract_content(tool_name, tool_input, file_path)
        if not content:
            return None

        if len(content.encode("utf-8", errors="replace")) > _MAX_WHOLE_FILE_BYTES:
            return None

        try:
            sites = sites_in_source(content, file_path)
        except SpawnParseError:
            return None

        # This guard is scoped to SHELL-SHAPED spawns only (module name and
        # docstring) — a PLAIN_SPAWN site (any other subprocess/exec call)
        # is not the shape this offer addresses; C3's test gate covers the
        # full site inventory, this advisory targets the shell-shaped subset.
        shell_shaped = [
            s for s in sites if s.kind in (SpawnKind.SHELL_BINARY, SpawnKind.SHELL_TRUE)
        ]
        if not shell_shaped:
            return None

        # Lead with the alternative for the FIRST (lowest-ordinal) site's
        # kind — one advisory envelope per PreToolUse response (INTERFACE.md).
        primary_kind = shell_shaped[0].kind
        reason = _reason_for(primary_kind, file_path, len(shell_shaped))

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }
    except Exception:
        # Fail-OPEN on any unexpected error — this guard offers only on a
        # positive shell-shaped-spawn match, never on an error.
        return None

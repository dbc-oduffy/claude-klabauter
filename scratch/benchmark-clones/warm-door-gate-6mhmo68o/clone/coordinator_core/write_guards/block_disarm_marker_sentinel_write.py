"""coordinator_core.write_guards.block_disarm_marker_sentinel_write --
hard-deny guard closing the file-write leg of the blanket-disarm marker's
un-creatable-by-agent boundary.

WHY THIS EXISTS. `coordinator_core.bash_guards.block_disarm_marker_sentinel_
creation` closes the Bash leg for `_blanket_disarm.py`'s marker (every shell
shape tried -- `touch`, `>`, `>>`, `tee`, `cp`, `mv`, `dd of=`, `sed -i`,
printf-redirect, heredoc `cat >`, `bash -c`, `xargs`, `cd && touch` -- is
denied there). But that guard's own docstring ("Write-surface note") names
the gap directly: it closes ONLY the Bash leg, and the `Write`/`Edit`/
`MultiEdit`/`NotebookEdit` leg is "a SEPARATE PreToolUse hook family this
repo does not own." Confirmed empirically (by the agent that built the Bash
guard): a plain `Write` of the marker file succeeded with no denial. This is
the identical discovery shape as `block_worktree_sentinel_write.py`'s own
gap (that guard's own docstring): a sentinel whose mere ABSENCE gates a
capability needs its CREATION blocked on every write-shaped tool surface, or
the boundary is theatre on the surfaces left uncovered. This module is that
follow-up for the disarm marker specifically -- the third sentinel this
package protects, after the worktree-override sentinel
(`block_worktree_sentinel_write.py`) and the doctrine-edit-approval sentinel
(`guard_doctrine_surface_edits.py`).

Mechanism: delegates entirely to the shared `_sentinel_write_guard` helper
(`extract_target_path`, `sentinel_write_denial`) already vendored into this
package for `block_worktree_sentinel_write.py`. This guard has no
approval-lookup concern to order against -- it exists solely to protect one
sentinel path, identical in that respect to `block_worktree_sentinel_write.py`.

Basename sourced from `_blanket_disarm.MARKER_BASENAME`, not hand-copied --
the same discipline `block_disarm_marker_sentinel_creation.py` (the Bash
sibling) already applies, so the write leg and the Bash leg and
`_blanket_disarm.py` itself can never independently drift on what string
means "the disarm marker." (A DoE-side hook could not do this: it cannot
import a claude-klabauter module, so if this guard had to live in DoE it would
need a hand-copied second string. It does not, because the write-guard fold
already relocated this whole family into this repo -- see
`docs/plans/2026-07-29-hook-fan-in-write-path.md` (DoE-claude repo) and
`coordinator/hooks/scripts/preuse-write-dispatch.py` (DoE-claude repo),
which fronts this package's guards in-process rather than registering a
separate hook script per guard.)

Deny message discipline -- the marker's basename is NEVER printed, same
reasoning as `block_disarm_marker_sentinel_creation.py`'s own deny message
and `block_worktree_sentinel_write.py`'s: a deny message that prints its own
bypass reads to an eager agent as a sanctioned next step, not a boundary.
Names the two legitimate paths instead: the operator creating the marker
themselves via the `!`-prefixed prompt (bypasses every PreToolUse hook,
this one included), or escalating to the EM/PM.

Removal stays allowed -- this guard only matches
Write/Edit/MultiEdit/NotebookEdit -- there is no file-deletion tool in that
set, so `rm`-based removal of an existing marker (already permitted by the
Bash-leg sibling) is untouched here too, and remains the sanctioned way to
re-arm the guard suite.

Symlink and case-folding are handled by the shared helper (see
`_sentinel_write_guard.is_sentinel_write` docstring): the target path is
resolved (`os.path.realpath(os.path.abspath(...))`) and the basename
comparison is case-folded before matching against the marker's basename.
Matches on BASENAME only, same as the Bash-leg sibling's `SentinelCreation
Detector` -- the marker is settings-home-scoped, not repo-root-scoped, so
its absolute path varies by machine (`COORDINATOR_SETTINGS_HOME` override,
else `CLAUDE_HOME`, else the platform home + `.coordinator-claude-
settings`); basename matching denies every path shape without this module
needing to resolve `settings_home()` itself.

Fail-CLOSED on its own resolution failures: an unexpected exception inside
this module's own `check()` propagates rather than being swallowed into an
allow, matching `block_worktree_sentinel_write.py`'s fail-closed posture and
the fidelity rule against wrapping a ported hard-deny `check()` in a blanket
`try/except: return None` (`write_guards/INTERFACE.md`).

Negative-spec:
  - Does NOT block reads, or removal (`rm`) of the marker -- no
    deletion-shaped tool is in `MATCHERS`.
  - Does NOT name the marker's basename or any override incantation in its
    deny reason, by design (see "Deny message discipline" above).
  - Does NOT gate any other approval/allow decision -- like
    `block_worktree_sentinel_write.py`, this module has no ordering hazard
    against an approval-state lookup because it has no such lookup.
  - Does NOT touch the Bash leg -- that is
    `block_disarm_marker_sentinel_creation.py`'s job, in the `bash_guards`
    package, evaluated by a different dispatcher.

Spec: blanket-disarm-marker sentinel un-creatable-by-agent guard, Write-tool
leg (dispatch M18-leg2 follow-up, 2026-07-30) -- closes the gap
`block_disarm_marker_sentinel_creation.py`'s own "Write-surface note" names.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from coordinator_core.write_guards._sentinel_write_guard import (
    extract_target_path,
    sentinel_write_denial,
)
from coordinator_core.bash_guards._blanket_disarm import MARKER_BASENAME

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 130

#: The exact basename this guard protects -- imported from `_blanket_
#: disarm.py`, the module that defines what "the disarm marker" means, so
#: this module and the Bash-leg sibling and `_blanket_disarm.py` itself can
#: never independently drift on the string.
_SENTINEL_NAME = MARKER_BASENAME

#: Deny reason -- deliberately withholds the sentinel basename and any
#: override incantation (see module docstring "Deny message discipline").
#:
#: 2026-08-13 (docs/plans/2026-08-13-guard-messages-stop-handing-agents-
#: the-keys.md, C4c): the prior text named the sanctioned creation route as
#: "via !-prefixed prompt" -- a CLI-invocation-shaped clause naming a
#: concrete unlock mechanism. Removed unconditionally, for every audience:
#: AC-1 bans a CLI invocation / any statement that an unlock exists from a
#: subagent-audience render, and AC-2 caps an EM-audience render at "a key,
#: path, or command" only via a wiki pointer -- this guard has no doc
#: pointer to substitute (no override env var backs this sentinel), so the
#: clause has no audience-safe form and is dropped rather than gated.
_DENY_REASON = (
    "[disarm-marker guard] BLOCKED: this file disarms the guard suite; no "
    "self-grant.\n"
    "Only the operator can create it. If warranted, say so, let EM/PM "
    "decide.\n"
    "Read/remove stays allowed -- removal re-arms."
)


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the disarm-marker-write-ban gate against a PreToolUse
    payload.

    Returns `None` (allow) or the nested hard-deny envelope.
    """
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None

    target = extract_target_path(tool_input)
    if not target:
        return None

    return sentinel_write_denial(target, _SENTINEL_NAME, _DENY_REASON, payload=payload)

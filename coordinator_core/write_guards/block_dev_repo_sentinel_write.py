"""coordinator_core.write_guards.block_dev_repo_sentinel_write -- advisory
guard closing the file-write leg of the `.coordinator-dev-repo` removal
guard.

WHY THIS EXISTS. `coordinator_core.bash_guards.block_dev_repo_sentinel_
removal` closes the Bash leg for `.coordinator-dev-repo` (the example-doctrine-repo
repo-root dev-vs-OSS discriminant consumed by `claude_md_budget.
DEV_REPO_SENTINEL` and `resolve_coordinator_clone.py`, written at install
by `coordinator_core.install.maximalist`). This module is the companion
Write/Edit/MultiEdit/NotebookEdit leg -- mechanism and template lifted
directly from `block_worktree_sentinel_write.py`, the closest sibling
(a single-sentinel guard with no approval-lookup ordering concern).

LOWER VALUE THAN THE BASH LEG, DELIBERATELY NAMED. Truncating or editing
the sentinel's CONTENT (what this guard actually blocks) does not remove
the file -- `claude_md_budget`/`resolve_coordinator_clone` gate on the
sentinel's mere PRESENCE (`os.path.isfile`/`Path.exists`), not its
contents, so an Edit/MultiEdit/NotebookEdit against it is far less
dangerous than the Bash leg's `rm`/`mv`/`git rm`/`git mv` shapes -- those
remove the file the discriminant depends on; this leg only ever touches
what is already inside it. `Write`, which replaces file content wholesale
(and could, in principle, truncate to empty), is the one real overlap with
the Bash leg's concern, but even an empty file still satisfies `isfile()`/
`exists()`, so the discriminant survives even a maximally destructive Write
here. The message below is scaled to that lower stakes -- it does not
repeat the Bash leg's "breaks the discriminant fleet-wide" framing.

Mechanism: delegates entirely to the shared `_sentinel_write_guard` helper
(`extract_target_path`, `sentinel_write_advisory`), same as every sibling
sentinel-write guard in this package. No approval-lookup ordering concern
-- this guard exists solely to protect one path.

CLASS = "advisory" (2026-08-06 write-guard classification pass,
docs/plans/2026-08-06-apply-guard-class-census.md C12): reclassified from
hard-deny per DR-277. The sentinel's mere PRESENCE is the discriminant
(see above), not its content, so even a maximally destructive `Write`
here cannot remove the discriminant -- the file still satisfies
`isfile()`/`exists()`. This guard now calls `sentinel_write_advisory()`,
the additive advisory-shaped sibling of `sentinel_write_denial()` added
to `_sentinel_write_guard.py` for this flip -- `sentinel_write_denial()`
itself is untouched, so the OTHER hard-deny callers of that helper
(`block_disarm_marker_sentinel_write.py`, `block_worktree_sentinel_write.py`,
`guard_doctrine_surface_edits.py`, `guard_settings_json_write.py`) keep
their deny envelope unchanged.

Advisory message discipline -- the sentinel's basename is still never
printed in this guard's own reason text (a separate concern from the
bypass question below: naming `.coordinator-dev-repo` here would tell an
agent exactly what this guard protects, for no reader benefit). The
channel-withholding half of that discipline -- never naming the override at
all -- is now the OUTLIER, not the rule: the reason text below names the
pre-launch env var via `operator_override_note`, same as every other
sentinel-write guard in this package that has one. (The in-session-unlock
auto-append at the `write_guards.engine` seam,
docs/plans/2026-08-03-in-session-operator-unlock-for-the-hard-.md § C4,
applies only to the hard-deny phase -- this guard's advisory no longer
reaches it, and its reason text does not depend on that append.) The
pre-launch env-var line names a key settable only in the environment before
session launch, not an assignment an agent could paste and run (see
`operator_override_note`'s own 2026-08-11 NEGATIVE SPEC 4) -- so naming it
is not the sanctioned-bypass signal the basename-withholding note above
still guards against; that framing applied to the target path, never to
the operator's own remediation channel.

Override: `COORDINATOR_OVERRIDE_DEV_REPO_SENTINEL=1` (pre-launch) -- same
env var as the Bash-leg sibling, so a single override covers both legs of
one operator intent rather than requiring two different variables for one
decision. The in-session unlock (per-`(session_id, guard_name)`, one-shot,
`coordinator_core.session.guard_unlock_sentinel`) is additive, reachable
from inside the very session that hit this deny, and is never the sole
channel named here -- the pre-launch key stays supported unchanged.

Removal stays allowed on this leg by construction -- there is no
file-deletion tool in `MATCHERS`; the Bash-leg sibling is the one that
actually gates `rm`/`mv`/`git rm`/`git mv`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _sentinel_write_guard import extract_target_path, sentinel_write_advisory  # noqa: E402

from coordinator_core.bash_guards._helpers import operator_override_note

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 122  # advisory band slot (docs/wiki/write-guard-priority-bands.md)

#: The exact basename this guard protects.
_SENTINEL_NAME = ".coordinator-dev-repo"

#: Same override env var as the Bash-leg sibling
#: (`block_dev_repo_sentinel_removal.py`) -- one operator decision, one
#: variable, covering both legs.
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_DEV_REPO_SENTINEL"

def _advisory_reason(payload: Optional[Dict[str, Any]]) -> str:
    """Per-call reason builder (not a module-level constant): the trailing
    ``operator_override_note`` call needs this request's ``payload`` to
    resolve audience, which an import-time constant cannot carry."""
    _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload)
    base = (
        "[dev-repo guard] This file's mere presence is the dev-vs-OSS "
        "discriminant; content edits aren't expected -- if you need to change "
        "it, delete and recreate it instead of editing in place (deletion "
        "isn't gated on this leg)."
    )
    return base + (" " + _note if _note else "")


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the dev-repo-sentinel-write guard against a PreToolUse
    payload.

    Returns `None` (allow) or the nested advisory envelope -- the write is
    never blocked; `additionalContext` only surfaces the recreate-instead-
    of-edit alternative.
    """
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None

    if os.environ.get(_OVERRIDE_ENV_VAR) == "1":
        return None

    target = extract_target_path(tool_input)
    if not target:
        return None

    return sentinel_write_advisory(
        target, _SENTINEL_NAME, _advisory_reason(payload), payload=payload
    )

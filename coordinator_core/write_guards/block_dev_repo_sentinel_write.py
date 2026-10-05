"""coordinator_core.write_guards.block_dev_repo_sentinel_write -- hard-deny
guard closing the file-write leg of the `.coordinator-dev-repo` removal
guard.

WHY THIS EXISTS. `coordinator_core.bash_guards.block_dev_repo_sentinel_
removal` closes the Bash leg for `.coordinator-dev-repo` (the coordinator-content-repo
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
(`extract_target_path`, `sentinel_write_denial`), same as every sibling
sentinel-write guard in this package. No approval-lookup ordering concern
-- this guard exists solely to protect one path.

CLASS = "hard-deny"; the policy point (``machine_profile.apply_guard_level``)
leaves the deny on an author box and downgrades it to a warning on a consumer
box. The reason text never names the sentinel's basename or the override
mechanism (``operator_override_note`` gates that by audience; see
docs/wiki/guard-messaging.md § Register).

Override: `COORDINATOR_OVERRIDE_DEV_REPO_SENTINEL` (pre-launch, same env var
as the Bash-leg sibling, so one override covers both legs) -- named here
for the reader of the code, never in the rendered message.

Removal stays allowed on this leg by construction -- there is no
file-deletion tool in `MATCHERS`; the Bash-leg sibling is the one that
actually gates `rm`/`mv`/`git rm`/`git mv`.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from coordinator_core.write_guards._sentinel_write_guard import (
    extract_target_path,
    sentinel_write_denial,
)

from coordinator_core.bash_guards._helpers import operator_override_note

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 122

_SENTINEL_NAME = ".coordinator-dev-repo"

_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_DEV_REPO_SENTINEL"

def _deny_reason(payload: Optional[Dict[str, Any]]) -> str:
    _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload)
    base = (
        "[dev-repo guard] This file's mere presence is the dev-vs-OSS "
        "discriminant; content edits aren't expected -- if you need to change "
        "it, delete and recreate it instead of editing in place (deletion "
        "isn't gated on this leg)."
    )
    return base + (" " + _note if _note else "")


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
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

    return sentinel_write_denial(
        target, _SENTINEL_NAME, _deny_reason(payload), payload=payload
    )

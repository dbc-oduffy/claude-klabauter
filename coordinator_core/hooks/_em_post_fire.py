"""Shared post-fire predicate: once an EM main-thread session has launched a Workflow run, its
product writes belong to the run. The Edit/Write hook and the Bash write guard decide through
this one module; `GUARD_NAME` is a floor guard, so no guard level turns the deny into an advisory."""

from __future__ import annotations

import os

from coordinator_core._hook_envelope import deny
from coordinator_core.machine_profile import apply_guard_level

GUARD_NAME = "block-em-post-fire-product-write"

_POST_FIRE_DENY = (
    "This session already fired engine-ask; its product edits belong to the run. "
    "Send the gap through workflow.revise, or fire a follow-up engine-ask whose em_context "
    "quotes the receipt's gap line."
)

_NON_PRODUCT_TOP_DIRS = ("state", ".coordinator-local")


def fired(session_id: str) -> bool:
    """True when this session launched a Workflow run, live or landed. False on an empty id or any
    read failure: the deny fails open."""
    if not session_id:
        return False
    try:
        from coordinator_core.hooks import flag_em_poll_in_flight

        return bool(flag_em_poll_in_flight.session_workflow_run_ids(session_id))
    except Exception:
        return False


def is_product_path(abs_path: str, root: str, session_id: str) -> bool:
    """True for a path inside `root` that is not scratch/tmp, under `state/` or `.coordinator-local/`,
    or a bootstrap/out-of-repo dir. The bare repo root counts as product."""
    from coordinator_core.hooks._em_write_budget import (
        _is_bootstrap_or_out_of_repo,
        _is_scratch_or_tmp,
    )

    if not abs_path or not root:
        return False
    path = os.path.normcase(os.path.abspath(abs_path))
    base = os.path.normcase(os.path.abspath(root))
    if path != base and not path.startswith(base + os.sep):
        return False
    probe = abs_path
    if path != base:
        top = path[len(base) + 1 :].split(os.sep, 1)[0]
        if top in _NON_PRODUCT_TOP_DIRS:
            return False
    else:
        # the work-tree probe looks at the parent dir; give it the root's own
        probe = os.path.join(abs_path, ".")
    if _is_scratch_or_tmp(abs_path):
        return False
    return not _is_bootstrap_or_out_of_repo(probe)


def deny_envelope() -> dict | None:
    """The PreToolUse deny, passed through `apply_guard_level` (floor: denies at every level)."""
    return apply_guard_level(GUARD_NAME, deny("PreToolUse", _POST_FIRE_DENY))

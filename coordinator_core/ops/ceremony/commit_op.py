"""
coordinator_core.ops.ceremony.commit_op — JSON-RPC "ceremony.commit" op.

Purpose: register a reachable, correctly-classified commit op so the warm
engine has a method to dispatch a commit to at all. C1 of
docs/plans/2026-08-26-the-commit-becomes-a-warm-served-op.md — the thinnest
useful version: this handler delegates to `run_commit_pipeline` UNCHANGED, no
git-mechanism work here. Later chunks (C3 onward) replace the pipeline's own
two-git-invocation agree branch with an in-process object write; this chunk's
only job is to make the seam REACHABLE (AC1) and correctly CLASSIFIED (AC2)
so every later chunk is measured through the real warm transport instead of
being exercised by calling `run_commit_pipeline` directly in a test.

Op name: `ceremony.commit` (plan § "The op's name chooses its budget") — a
FRESH decision, not a resurrection of the killed `ceremony.scoped_git_commit`
(DR-344: rebuild-from-scratch is the norm, never the old implementation made
faster). The `ceremony.` prefix is deliberate: it is both a
`_CEREMONY_PACKAGE_ALIASES` name match AND an `_owning_module_is_ceremony`
module-path match (this module lives under `ops/ceremony/`), so
`ipc.py :: _timeout_for` clamps it to `CEREMONY_BUDGET_SECS = 2.0` by
construction — the plan records and accepts that ceiling rather than adding
an override row (none exists for this op, and DR-348's ratchet forbids
raising the ceiling regardless).

Classification: MUTATING, asserted explicitly in `authz/classification.py`
rather than left to `_op_may_mutate`'s fail-closed default (AC2) — the
default is a safety net, not a declaration, and this op unambiguously writes
git objects/refs.

Keying scope: common_dir — same precedent as `commit.exec_bit_change`
(`ops/ceremony/commit_exec_bit.py`): the handler receives `repo_root` as the
git common dir and derives the caller's worktree via
`main_worktree_root(repo_root)`. `params.repo_root` is the optional D3
consistency assertion only (`check_repo_root`), never the worktree-resolution
source.

Spec backlinks:
    docs/plans/2026-08-26-the-commit-becomes-a-warm-served-op.md § C1, AC1, AC2

Negative-spec (hard-won, this chunk):
  - Does NOT reimplement or alter any git mechanism — `run_commit_pipeline`
    is called unchanged; C3 is the chunk that replaces its git-invocation
    shape with the in-process object write.
  - Does NOT resurrect the killed `ceremony.scoped_git_commit` op name or any
    string-keyed reference to it — this op has its own name, checked against
    the guard suites that caught a killed op's name living on before.
  - Does NOT support `stage_patch` (Out of scope: "stage_patch's private-index
    branch") or `on_committed` (no wire representation for a callback) — both
    are left at `run_commit_pipeline`'s own defaults.
  - Does NOT push — `push_mode` is forwarded only when a caller explicitly
    supplies it; this op does no push-mode policy of its own beyond passing
    the param through. An omitting caller therefore inherits
    `run_commit_pipeline`'s own default, which is `PUSH_MODE_NONE` as of
    2026-08-26 (it was `PUSH_MODE_SYNC`, which silently put a synchronous
    push inside this op's 2.0s ceremony clamp -- see that constant's block).
    The commit still publishes, via the post-commit hook's detached push.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import List, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.commit_pipeline import run_commit_pipeline
from coordinator_core.ops.fleet._common import check_repo_root, main_worktree_root


def _error(message: str, **extra: object) -> dict:
    """Build the structured-error result envelope for this op.

    Purpose: uniform fail-loud shape, mirroring `commit_exec_bit.py :: _error`
    — mutation flags false, "error" naming what went wrong, plus any extra
    naming payload.
    """
    result: dict = {
        "committed": False,
        "sha": None,
        "error": message,
    }
    result.update(extra)
    return result


def _string_list(params: dict, key: str) -> Optional[List[str]]:
    """Validate an optional params field as a list of non-empty strings.

    Returns the coerced list on success, `None` when the field is absent,
    or raises `ValueError` naming the field on a malformed value — the
    handler turns that into a structured `_error(...)` result rather than
    letting a TypeError escape to the caller.
    """
    raw = params.get(key)
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        raise ValueError(f"params.{key} must be a list of strings")
    return raw



"""
coordinator_core.ops.workday_drain_pending_push — JSON-RPC
"workday.drain_pending_push" operation.

Purpose: AC14's third independent drain point for the durable pending-push
record (`coordinator_core.hooks.auto_push`'s
`coordinator-auto-push-pending.json`) — the workday-start push-health seam.
Spec backlink: pln-check-5-foreign-staged-owner-a-415188
§ AC14/AC14a, § Delivery and residuals ("AC14 (partial) — drain redundancy
is 1-of-3").

Deliberately a SEPARATE op from `workday.surface_auto_push_failure_stats`,
never folded into it: that op's own module docstring ratifies it as a pure
read — "Idempotency (AC7, DEC-7 note): INHERENT — pure read, zero writes,
zero subprocess spawns" — and its double-invocation test pins exactly that
contract. A drain pushes and mutates (removes the pending record on a
successful push); adding that here would silently break a ratified
contract and its test. `/workday-start` is expected to invoke BOTH ops at
its push-health step: the read for the failure-count surface, this op for
the drain — see that module's own docstring for the read half.

`drain_pending_push` itself is idempotent and best-effort by its own
documented contract (its entire body is wrapped in a bare try/except and it
never raises) — this handler adds no additional guarding beyond the
required-param check below, mirroring `workday.
surface_auto_push_failure_stats`'s own param-authoritative handler shape.

Registration: importing this module fires
`@register_op("workday.drain_pending_push", _handler)` as a side effect. A
serial tail pass wires this module into
`coordinator_core/ops/__init__.py::_EAGER_OP_MODULES`,
`coordinator_core/op_scopes.py::_OP_KEY_SCOPE` (intended scope: `show_top`,
matching `workday.surface_auto_push_failure_stats`'s own entry and
rationale — the handler ignores the ipc-injected `repo_root` kwarg entirely
and uses only the explicit `repo_root` param), and `_registry_map.py` —
this module does not touch those shared surfaces itself (CC-3 convention;
see `workday_stitch_sidecar_summary.py`'s own registration note and
`workday_surface_auto_push_failure_stats.py`'s own scope-table comment for
precedent).

Contract: params {repo_root: str} -> {"drained": true}
    The return value is observational only ("the call ran"), never "a
    record existed and was pushed" — `drain_pending_push` swallows every
    outcome (no record, in-window record left alone, successful push,
    failed push) identically and returns None. A caller wanting to know
    whether a push actually happened should consult `.git/push-failures.log`
    (via `workday.surface_auto_push_failure_stats`) or the pending record
    file itself, not this op's return value.

Negative-spec:
    - Does NOT read or aggregate `.git/push-failures.log` — that remains
      `workday.surface_auto_push_failure_stats`'s exclusive job.
    - Does NOT decide whether a push is due, nor retry beyond what
      `run_push_with_retry(repo_root, branch)` already performs internally
      — delegates entirely, unchanged, to
      `coordinator_core.hooks.auto_push.drain_pending_push`. (Review:
      coordinator:code-reviewer, P2, 2026-08-30 -- `_skip_hold` was deleted
      from `run_push_with_retry`'s signature by this wave's Finding 6; this
      docstring described a call shape that no longer exists.)
    - Does NOT accept or require a git COMMON dir — `repo_root` is the
      caller's working-tree root, exactly like `auto_push.py`'s own
      `repo_root` parameter (it resolves the common dir internally via
      `resolve_git_common_dir`).
"""

from __future__ import annotations

from coordinator_core.hooks.auto_push import drain_pending_push
from coordinator_core.ipc import register_op


class DrainPendingPushError(RuntimeError):
    """Structured failure for workday.drain_pending_push — raised only on a
    caller premise failure (missing/empty `repo_root`), never on drain
    content — `drain_pending_push` itself never raises."""


@register_op("workday.drain_pending_push")
def _handler(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'workday.drain_pending_push' handler — sync.

    Params: repo_root (str, required) — the caller's repo working-tree
    root, mirroring `workday.surface_auto_push_failure_stats`'s own
    param-authoritative convention (the explicit param is authoritative;
    the ipc-injected `repo_root` kwarg is accepted but unused) so both
    workday-start push-health seams share one calling contract.
    """
    param_root = params.get("repo_root") or ""
    if not param_root:
        raise DrainPendingPushError(
            "workday.drain_pending_push: required param 'repo_root' is "
            "missing or empty"
        )
    drain_pending_push(param_root)
    return {"drained": True}

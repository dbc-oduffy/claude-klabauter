"""
coordinator_core.ops.workflow_fire.op — JSON-RPC "workflow.fire" and
"workflow.fire_status" operations.

Purpose: thin RPC wrapper registering the two-op surface this package
exists for -- ``workflow.fire`` spawns exactly one detached ``claude -p``
child per emitted workflow script and returns the fire registry record as
the run handle; ``workflow.fire_status`` re-reads and refreshes that record
by ``fire_id``. All spawning, plugin-dir resolution, and registry I/O lives
in ``fire.py`` -- this module's only original code is param validation and
mapping library exceptions to the JSON-RPC error contract other ops in
this family use (``ValueError`` for a missing/invalid param, matching
``dispatch_emit.op``'s convention).

Spec backlink: docs/plans/2026-08-18-claude-klabauter-fires-the-workflows-it-emits.md
§ C4

Wire params -- workflow.fire:
    script_path (str, required)   -- path to the emitted workflow ``.mjs``
                                      script to fire. Must exist on disk.
    model (str, optional)         -- driver model; defaults to
                                      ``fire.DEFAULT_MODEL`` (cheap tier --
                                      the driver calls one tool once).
    max_turns (int, optional)     -- child turn cap; defaults to
                                      ``fire.DEFAULT_MAX_TURNS``.
    claude_bin (str, optional)    -- the ``claude`` binary to invoke;
                                      defaults to ``"claude"`` (resolved via
                                      PATH, same as the interactive shim).
    concurrency_cap (int, optional) -- live-fire refusal threshold;
                                      defaults to
                                      ``fire.DEFAULT_CONCURRENCY_CAP``.

Reply fields -- workflow.fire:
    the fire registry record (see ``fire.fire_workflow`` docstring): {
      "fire_id", "pid", "script_path", "plugin_dir", "claude_bin", "model",
      "max_turns", "command", "started_at", "log_path", "state",
      "exit_code", "status_checked_at", "log_size_bytes" }
      ``log_size_bytes`` is a cheap, non-authoritative signal only -- see
      ``fire.fire_status`` docstring: a ``0`` flags "nothing captured yet",
      a non-zero value is NOT proof any phase succeeded.

Wire params -- workflow.fire_status:
    fire_id (str, required)       -- the ``fire_id`` from a prior
                                      ``workflow.fire`` reply.

Reply fields -- workflow.fire_status:
    the refreshed registry record, or ``{"fire_id": ..., "found": False}``
    if no record exists for ``fire_id``.

Negative-spec:
  - Does NOT spawn a child per phase -- one child per fired workflow (see
    ``fire.py`` module docstring). Not this module's own decision to
    re-litigate; it only forwards the caller's one ``script_path``.
  - Does NOT wait on the fired child or poll it to completion -- returns
    as soon as ``fire.fire_workflow`` confirms liveness (bounded window,
    see that function's docstring).
  - Does NOT add any repo_root-resolution logic of its own -- this module
    only forwards the envelope's ``repo_root`` straight through as
    ``fire.fire_workflow(..., cwd=str(repo_root) if repo_root is not None
    else None)``. Registry placement DOES therefore depend on the
    envelope's ``repo_root`` whenever a caller supplies one: it becomes the
    ``cwd`` that ``fire.py``'s ``_registry_dir`` -> ``sessions_dir`` walks
    from. Only when ``repo_root`` is absent does placement fall back to
    the process's own cwd / git-common-dir.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.workflow_fire import fire


@register_op("workflow.fire")
def _workflow_fire(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "workflow.fire" handler -- see module docstring for the
    wire contract.

    Raises:
        ValueError -- if ``script_path`` is missing.
        fire.ScriptNotFoundError -- if ``script_path`` does not exist.
        fire.PluginDirResolutionError -- if ``--print-plugin-dir`` cannot
            be resolved (fail-loud; never silently omits the flag).
        fire.ConcurrencyCapExceededError -- if the live-fire count is at
            or above the cap.
        fire.ChildSpawnFailedError -- if the child cannot be confirmed
            live (immediate non-zero exit, spawn OSError).
    """
    script_path = params.get("script_path")
    if not script_path:
        raise ValueError("workflow.fire requires param: script_path")

    return fire.fire_workflow(
        script_path,
        cwd=str(repo_root) if repo_root is not None else None,
        claude_bin=params.get("claude_bin", "claude"),
        model=params.get("model", fire.DEFAULT_MODEL),
        max_turns=params.get("max_turns", fire.DEFAULT_MAX_TURNS),
        concurrency_cap=params.get("concurrency_cap", fire.DEFAULT_CONCURRENCY_CAP),
    )


@register_op("workflow.fire_status")
def _workflow_fire_status(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "workflow.fire_status" handler -- see module docstring for
    the wire contract.

    Raises:
        ValueError -- if ``fire_id`` is missing.
    """
    fire_id = params.get("fire_id")
    if not fire_id:
        raise ValueError("workflow.fire_status requires param: fire_id")

    record = fire.fire_status(fire_id, cwd=str(repo_root) if repo_root is not None else None)
    if record is None:
        return {"fire_id": fire_id, "found": False}
    return record

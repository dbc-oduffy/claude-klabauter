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
    max_turns (int, optional)     -- child turn cap; when omitted, scales
                                      with the emitted script's own declared
                                      phase count (``fire._scaled_max_turns``,
                                      floor ``fire.DEFAULT_MAX_TURNS``) rather
                                      than a bare constant that never grew
                                      with the workflow it bounds
                                      (klabauter#41).
    claude_bin (str, optional)    -- the ``claude`` binary to invoke;
                                      defaults to ``"claude"`` (resolved via
                                      PATH, same as the interactive shim).
    concurrency_cap (int, optional) -- live-fire refusal threshold;
                                      defaults to
                                      ``fire.DEFAULT_CONCURRENCY_CAP``.

Reply fields -- workflow.fire:
    the fire registry record (see ``fire.fire_workflow`` docstring): {
      "fire_id", "pid", "script_path", "repo_root", "plugin_dir",
      "claude_bin", "model", "max_turns", "command", "started_at",
      "log_path", "state", "exit_code", "exit_code_note", "outcome",
      "outcome_basis", "failure_subtype", "driver_session_id",
      "status_checked_at", "status_checked_at_iso", "log_size_bytes",
      "publish_lag_message", "_readme" }
      ``repo_root`` (claude-klabauter#41) is the resolved worktree ``cwd``
      this fire's ``script_path`` was checked against (``None`` when no
      ``repo_root`` was resolvable for this call) -- see fire-time
      refusal, below.
      ``failure_subtype`` (claude-klabauter#37) is the driver's own
      terminal ``subtype`` (e.g. ``"error_max_turns"``) whenever
      ``outcome`` is ``"failed"``; absent otherwise.
      ``publish_lag_message`` (DR-335) is non-``None`` only when the
      published engine mirror is more than 30 minutes AND more than zero
      engine-touching commits behind this fire's source tree -- see
      ``coordinator_core.warm.skew.publish_lag``/``publish_lag_message``.
      ``None`` covers both "current" and "cannot tell"; this run always
      executes the published mirror regardless of this field's value.
      ``state``, ``exit_code`` and ``outcome`` are valid only as of
      ``status_checked_at`` -- the record is NOT written when the child
      exits, so a raw file read can report a dead run as running.
      ``_readme`` carries that fact for a reader who has only the file;
      ``workflow.fire_status`` is the refreshing read.
      ``exit_code`` is permanently ``None`` for a reaped child (detached
      spawn, no surviving parent -- ``exit_code_note`` says so in-band);
      ``outcome`` (``clean`` / ``truncated`` / ``unknown``, with
      ``outcome_basis`` naming the evidence) is what separates a clean
      finish from a truncation, classified from the child's own log.
      ``outcome: "clean"`` means the DRIVER finished, NOT that any phase
      did work -- for that, open the harness's workflow journal under
      ``driver_session_id`` (absent when no result envelope was recovered).
      See ``fire.fire_status``'s docstring for the reader move.
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
    from. When ``repo_root`` is absent, ``fire.fire_workflow`` no longer
    falls back to the firing process's own ambient cwd -- it resolves the
    target tree from ``script_path`` itself instead, or refuses via
    ``fire.RepoRootUnresolvableError`` when even that is not a git tree
    (klabauter#37; see ``fire._resolve_target_repo``).
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
        fire.RepoRootUnresolvableError -- if no ``repo_root`` was resolved
            AND ``script_path`` does not itself resolve to a git tree
            (fail-loud; never silently proceeds against the firing
            process's own ambient cwd, see klabauter#37).
        fire.ScriptOutsideRepoRootError -- if ``script_path`` does not live
            under the resolved repo root (klabauter#41 -- a fire aimed at
            the wrong tree used to spawn a child that could not read its
            own script and still reported success).
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
        max_turns=params.get("max_turns"),
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

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
    backend (str, optional)       -- ``codex`` selects the native Workflow
                                      executor and Codex workers; ``claude``
                                      keeps the existing Claude driver.
                                      Omitted preserves the legacy Claude
                                      default. ``engine_ask.submit`` selects
                                      Codex by default when firing.
    expected (str, optional)      -- the hex sha256 digest of the script the
                                      caller itself emitted -- ``dispatch.
                                      emit``'s own reply ``"sha256"`` key,
                                      threaded straight through, NEVER the
                                      script text. When supplied, checked
                                      against the on-disk bytes' digest via
                                      ``dispatch_emit.op.
                                      guard_against_fired_drift`` BEFORE the
                                      spawn -- refuses
                                      (``dispatch_emit.op.FiredDriftError``)
                                      a fire whose script changed on disk
                                      between emit and this call (a peer
                                      overwrote the deterministic emission
                                      path). Omitted, the fire is unverified
                                      -- the same behavior as before this
                                      param existed.
    model (str, optional)         -- Claude driver model; defaults to
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
    run_id (str, optional)        -- the run this fire drives; stored on the
                                      record so ``workflow.fire_status``
                                      reports ``outcome: not_landed`` for a
                                      clean exit whose run-stage is not
                                      ``landed``.
    notify_to (str, optional)     -- peer name notified by the warm engine
                                      when it observes driver exit.
                                      Native runs checkpoint and queue their
                                      notice on completion without polling.
    timeout_s (int, optional)     -- total native execution budget, including
                                      admission, agents and terminal action;
                                      defaults to 7200, bounded to 1..86400.

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
from coordinator_core.ops.workflow_fire.backends import check as check_backend


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
        fire.ChildSpawnFailedError -- if no child could be spawned (OSError).
            Fast child exits return their terminal handle, never invite a duplicate fire.
        dispatch_emit.op.FiredDriftError -- if ``expected`` is supplied and
            differs from the on-disk bytes at ``script_path``.
    """
    notify_to = params.get("notify_to")
    if notify_to is not None and (not isinstance(notify_to, str) or not notify_to.strip()):
        raise ValueError("notify_to must be a non-empty peer name")
    script_path = params.get("script_path")
    if not script_path:
        raise ValueError("workflow.fire requires param: script_path")

    expected = params.get("expected")
    if expected is not None:
        # Imported here, not at module top -- avoids widening this
        # package's own import surface for every caller that never passes
        # ``expected`` (the ordinary case today).
        from coordinator_core.ops.dispatch_emit.op import guard_against_fired_drift

        guard_against_fired_drift(Path(script_path), expected)

    backend = params.get("backend")
    if backend is not None:
        check_backend(backend)
    executor = params.get("executor", "headless" if backend not in (None, "claude") else "claude")
    if executor == "claude" and backend not in (None, "claude"):
        raise ValueError(f"a {backend} backend cannot use the Claude executor")
    if params.get("private_engine") is not None and (backend != "codex" or executor != "headless"):
        raise ValueError("a private native engine requires an explicit Codex headless executor")
    from coordinator_core.ops.workflow_fire.authority import fire_record_caller_session_id, require_fire_authority

    principal = require_fire_authority(
        script_path, expected, cwd=str(repo_root) if repo_root is not None else None,
        native=executor == "headless", backend=backend or ("codex" if executor == "headless" else "claude"),
        submitted_session_id=params.get("caller_session_id"),
    )
    if executor == "headless":
        from coordinator_core.ops.workflow_fire.headless import start, validate_private_engine_config

        private_spec = validate_private_engine_config(params.get("private_engine"), backend=backend or "codex")
        if private_spec is not None and principal.role != "em":
            raise PermissionError("only the EM may select a private native engine build")
        private_args = {'private_engine': private_spec} if private_spec is not None else {}

        record = start(
            script_path, cwd=str(repo_root) if repo_root is not None else None,
            notify_to=notify_to, backend=params.get("backend", "codex"),
            concurrency_cap=params.get("concurrency_cap", fire.DEFAULT_CONCURRENCY_CAP),
            timeout_s=params.get("timeout_s", 7200),
            args=params.get("args"), run_id=params.get("run_id"),
            caller_session_id=fire_record_caller_session_id(principal),
            expected=expected,
            **private_args,
        )
    elif executor == "claude":
        record = fire.fire_workflow(
            script_path,
            cwd=str(repo_root) if repo_root is not None else None,
            claude_bin=params.get("claude_bin", "claude"),
            model=params.get("model", fire.DEFAULT_MODEL),
            max_turns=params.get("max_turns"),
            concurrency_cap=params.get("concurrency_cap", fire.DEFAULT_CONCURRENCY_CAP),
            expected=expected,
            notify_to=notify_to.strip() if notify_to else None,
            run_id=params.get("run_id"),
        )
    else:
        raise ValueError("executor must be headless or claude")
    if notify_to is not None and not record.get("notify_to"):
        already_fired = record.get("already_fired")
        record = {**record, "notify_to": notify_to.strip()}
        try:
            record = _store_notification_record(record, repo_root)
        except OSError as exc:
            # The child exists: preserve its handle rather than inviting a second fire.
            record["notify_error"] = f"Notification target not persisted: {exc}"
        if already_fired:
            record = {**record, "already_fired": True}
    return record


def _store_notification_record(record: dict, repo_root: Optional[Path]) -> dict:
    """Persist notification metadata beside the fire record in its selected repo."""
    cwd = record.get("repo_root") or (str(repo_root) if repo_root is not None else None)
    path = fire._record_path(fire._registry_dir(cwd), record["fire_id"])
    from coordinator_core.locked_write import LockTimeout, held_lock

    try:
        with held_lock(path.resolve(), timeout=0, holder_label="workflow.fire notification target"):
            current = fire._read_record(path) or record
            return fire._write_record(path, {**current, "notify_to": current.get("notify_to") or record["notify_to"]})
    except LockTimeout:
        return {**record, "notify_error": "Notification target not persisted: record busy; repeat the same expected-digest fire to register target"}


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

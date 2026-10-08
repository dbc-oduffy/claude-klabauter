"""plan_chain driver: the five-stage main loop from an accepted sizing to ``dispatch.terminal_commit``.

Cold process, never a registered op. Each Workflow runs in its own pre-named headless child;
the one commit is issued from this frame. Every exit, normal or halt, writes the final digest
exactly once.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from coordinator_core.ops.plan_chain import digest as digest_mod
from coordinator_core.ops.plan_chain import emit_leg, execute_result, phase1_checks, phase1_gates, plan_stage
from coordinator_core.ops.plan_chain.contract import (
    ChainManifest,
    ChainState,
    Halt,
    WorkflowResult,
    WorkflowRunner,
    halt,
)

TerminalCommit = Callable[[dict], Mapping[str, Any]]

_WORKFLOW_ONLY_PROMPT = (
    "Call the Workflow tool exactly once with scriptPath {path}. "
    "Your final answer is its return value as compact JSON, verbatim. Call no other tool."
)
_USAGE_LIMIT = re.compile(r"usage[ _]limit", re.IGNORECASE)


def _workflow_only_prompt(script_path: Path) -> str:
    return _WORKFLOW_ONLY_PROMPT.format(path=script_path)


def _parse_json_object(text: str) -> dict[str, Any] | None:
    """The JSON object in ``text``: whole, or the outermost braces when the model wrapped it."""
    for candidate in (text.strip(), text[text.find("{") : text.rfind("}") + 1] if "{" in text else ""):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _workflow_run_result(session_id: str, script_path: Path) -> dict[str, Any] | None:
    """The Workflow's own return, from the child session's run record.

    Trap: the model's final answer is a retyped copy of this value and corrupts a large one,
    so the run record is read first and the answer text is only the fallback.
    """
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    projects = (Path(config) if config else Path.home() / ".claude") / "projects"
    want = os.path.normcase(os.path.abspath(script_path))
    best: tuple[str, dict[str, Any]] | None = None
    for run_file in projects.glob(f"*/{session_id}/workflows/*.json"):
        try:
            run = json.loads(run_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        result = run.get("result") if isinstance(run, dict) else None
        if not isinstance(result, dict) or run.get("status") != "completed":
            continue
        if os.path.normcase(os.path.abspath(str(run.get("scriptPath") or ""))) != want:
            continue
        stamp = str(run.get("timestamp") or "")
        if best is None or stamp > best[0]:
            best = (stamp, result)
    return best[1] if best else None


def _result_from_record(record: Mapping[str, Any], session_id: str, script_path: Path) -> WorkflowResult:
    from coordinator_core.ops.workflow_fire import fire

    log_path = record.get("log_path")
    text = ""
    if log_path:
        try:
            text = Path(log_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    envelope = fire._terminal_envelope(text) or {}
    raw = envelope.get("result")
    raw = raw if isinstance(raw, str) else ""
    if not raw and text:
        raw = text[-2000:]
    digest = _workflow_run_result(session_id, script_path)
    if digest is None and raw and not envelope.get("is_error"):
        digest = _parse_json_object(raw)
    return WorkflowResult(
        digest=digest, raw_result=raw, child_session_id=session_id, task_output_path=str(log_path or "")
    )


def _default_runner(repo_root: str) -> WorkflowRunner:
    def runner(script_path: Path, *, session_id: str) -> WorkflowResult:
        from coordinator_core.ops.workflow_fire import fire

        record = fire.fire_workflow(
            str(script_path),
            cwd=repo_root,
            prompt=_workflow_only_prompt(script_path),
            session_id=session_id,
            wait=True,
        )
        return _result_from_record(record, session_id, script_path)

    return runner


def _engine_terminal_commit(repo_root: str) -> TerminalCommit:
    def invoke(params: dict) -> Mapping[str, Any]:
        from coordinator_core.ipc import dispatch_message

        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "dispatch.terminal_commit",
            "params": params,
            "_origin_worktree": repo_root,
        }
        return asyncio.run(dispatch_message(msg, caller="plan-chain-run"))

    return invoke


def _delete_receipt(script: Path) -> None:
    from coordinator_core.ops.dispatch_emit.emission_receipt import emission_receipt_path

    emission_receipt_path(script).unlink(missing_ok=True)


def _run_child(runner: WorkflowRunner, script: Path, session_id: str, stage: str) -> WorkflowResult | Halt:
    """Run one child. A child that never ran loses its receipt; a usage-limited one halts."""
    from coordinator_core.ops.workflow_fire import fire

    try:
        result = runner(script, session_id=session_id)
    except fire.ConcurrencyCapExceededError as exc:
        _delete_receipt(script)
        return halt("fire-cap-reached", str(exc), running_stage=stage)
    except Exception as exc:  # noqa: BLE001 -- a child that did not run is a halt, not a crash
        _delete_receipt(script)
        return halt("child-no-digest", f"{type(exc).__name__}: {exc}", running_stage=stage)
    if result.digest is None and _USAGE_LIMIT.search(result.raw_result or ""):
        return halt("usage-limit", result.raw_result, running_stage=stage)
    return result


def _enter(state: ChainState, stage: str) -> None:
    if stage not in state.stages_run:
        state.stages_run.append(stage)


def _drive(
    manifest: ChainManifest,
    state: ChainState,
    chain_id: str,
    runner: WorkflowRunner,
    invoke_terminal_commit: TerminalCommit,
    seen: dict[str, Any],
) -> Halt | None:
    root = manifest.repo_root

    _enter(state, "plan")
    plan_sid = str(uuid.uuid4())
    plan_script = plan_stage.bind_plan_script(manifest, child_session_id=plan_sid, chain_id=chain_id)
    ran = _run_child(runner, plan_script, plan_sid, "plan")
    if isinstance(ran, Halt):
        return ran
    seen["plan"] = plan_stage.plan_digest(ran) or ran.digest
    plan_path = plan_stage.read_plan_result(ran, repo_root=root)
    if isinstance(plan_path, Halt):
        return plan_path
    state.plan_path = plan_path

    _enter(state, "ready-gate")

    _enter(state, "execute")
    blocked = phase1_gates.run(manifest, plan_path, repo_root=root) or phase1_checks.run(
        manifest, plan_path, repo_root=root
    )
    if blocked is not None:
        return blocked
    exec_sid = str(uuid.uuid4())
    emitted = emit_leg.run(
        plan_path, repo_root=root, child_session_id=exec_sid, trail_dir=manifest.trail_dir, chain_id=chain_id
    )
    if isinstance(emitted, Halt):
        return emitted
    exec_script, _sha = emitted
    ran = _run_child(runner, exec_script, exec_sid, "execute")
    if isinstance(ran, Halt):
        return ran
    seen["execute"] = ran.digest
    for stage in execute_result.stages_seen(ran.digest):
        _enter(state, stage)
    params = execute_result.read_execute_result(ran)
    if isinstance(params, Halt):
        state.resume = execute_result.resume_params(ran, exec_script)
        return params

    _enter(state, "terminal-commit")
    reply = invoke_terminal_commit({**params, "script_path": str(exec_script)})
    commit = execute_result.read_commit_reply(reply)
    if isinstance(commit, Halt):
        return commit
    state.commit = commit
    return None


def run(
    manifest: ChainManifest,
    *,
    runner: WorkflowRunner | None = None,
    invoke_terminal_commit: TerminalCommit | None = None,
) -> dict[str, Any]:
    """Drive the chain and return the final digest, already written to the trail directory."""
    state = ChainState()
    chain_id = uuid.uuid4().hex[:12]
    seen: dict[str, Any] = {}
    runner = runner or _default_runner(manifest.repo_root)
    invoke = invoke_terminal_commit or _engine_terminal_commit(manifest.repo_root)

    try:
        state.halt = _drive(manifest, state, chain_id, runner, invoke, seen)
    except Exception as exc:  # noqa: BLE001 -- surfaced as a halt at the running stage
        running = state.stages_run[-1] if state.stages_run else "plan"
        state.halt = Halt(running, f"{type(exc).__name__}: {exc}")
    if state.halt is not None and state.halt.halted_at not in state.stages_run:
        state.stages_run.append(state.halt.halted_at)

    trail = Path(manifest.trail_dir)
    if not trail.is_absolute():
        trail = Path(manifest.repo_root) / trail
    digest = digest_mod.assemble_final_digest(
        state, manifest, plan_digest=seen.get("plan"), execute_digest=seen.get("execute")
    )
    digest_mod.write_final_digest(digest, trail, chain_id)
    return digest

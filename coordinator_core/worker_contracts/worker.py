"""Explicit coordinator worker entry; policy is scoped to routine review/assent."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import uuid

from coordinator_core.codex_runner import roster
from coordinator_core.codex_runner.resolve import resolve_codex, preflight_auth
from coordinator_core.codex_runner.execute import _child_env
from coordinator_core.codex_runner.codex_spawn_env import shape_codex_child
from coordinator_core.codex_runner.sandbox_preflight import SandboxSetupRefused
from coordinator_core.bash_guards import _heavy_command_class as capacity
from coordinator_core.group_em import admission_post, slot_grant
from coordinator_core.session import peer_roster
from coordinator_core._settings_home import settings_home_child_env
from coordinator_core.worker_contracts.assignment import validate_assignment

POLICY = """You are a Codex worker assigned by the PM. Execute the assigned work.
Routine in-branch named review and assent gates are delegated to your judgment.
Keep scoped file commits, the worktree ban, box command capacity admission,
the assigned Group EM slot, and the ban on pushing to main. This delegation
does not authorize publishing, destructive actions, or changing these safeguards.
Do not infer permission for unrelated work. Attribute Codex work as Codex.
"""


def branch(repo: Path) -> str:
    """Workers use ordinary independent checkouts, never linked worktrees."""
    git = repo / ".git"
    if not git.is_dir():
        raise ValueError("worker requires an independent git checkout")
    head = (git / "HEAD").read_text(encoding="utf-8").strip()
    prefix = "ref: refs/heads/"
    if not head.startswith(prefix):
        raise ValueError("worker requires an assigned branch")
    name = head[len(prefix):]
    if name in {"main", "master"}:
        raise ValueError("worker cannot run on main/master")
    return name


def launch(repo: Path, task: str, assignment: dict, *, popen=None, stdout=None, on_spawn=None) -> int:
    """Launch only after PM-assignment assertions; preserve normal sandbox controls.

    Quiet-box and G-EM-slot assertions must come from the dispatch owner; this
    function does not issue a slot or relax command-class capacity admission.
    """
    validate_assignment(assignment, worker=True)
    if assignment.get("source") != "pm" or assignment.get("branch") != branch(repo):
        raise ValueError("PM assignment must name this branch")
    if not task.strip():
        raise ValueError("task is required")
    limit = assignment.get("quiet_max_sessions")
    peers = peer_roster.build_roster(str(repo), box_wide=True, raise_on_failure=True,
                                    raise_on_empty_snapshot=True)
    if len(peers) > limit:
        raise ValueError(f"box is not quiet: {len(peers)} live sessions exceeds assignment limit {limit}")
    preflight_auth(requested=False)
    argv = [*resolve_codex(), "--ask-for-approval", "on-request", "exec",
            "--sandbox", "workspace-write", "-"]
    return supervise(repo, POLICY + "\nAssigned task:\n" + task, assignment, argv,
                     provider="codex", mode="worker", popen=popen, stdout=stdout, on_spawn=on_spawn)


def _launch_identity_env(base: dict, provider: str, parent: str, argv: list[str]) -> dict:
    """A new native child owns no session until its actual SessionStart."""
    from coordinator_core.codex_runner.identity_env import independent_child_env
    env = independent_child_env(base, provider=provider, role="worker", parent=parent)
    for index, argument in enumerate(argv):
        if argument == "--":
            break
        if argument in ("--model", "-m") and index + 1 < len(argv):
            model = argv[index + 1]
            if model and not model.startswith("-"):
                env["COORDINATOR_AGENT_MODEL"] = model
        elif argument.startswith("--model=") and argument[8:]:
            env["COORDINATOR_AGENT_MODEL"] = argument[8:]
    return env


def supervise(repo: Path, prompt: str, assignment: dict, argv: list[str], *,
              provider: str, mode: str, popen=None, stdout=None, on_spawn=None) -> int:
    """Shared slot/memory/roster lifecycle; no worker policy is injected here."""
    validate_assignment(assignment)
    sid, run = assignment.get("session_id"), assignment.get("run")
    peak_mb = admission_post.valid_peak_mb(assignment.get("declared_peak_mb"))
    granted, _ = slot_grant.check_slot_grant(sid, run, peak_mb=peak_mb)
    if not granted:
        raise ValueError("worker requires a live Group EM slot grant covering declared_peak_mb")
    key = f"{provider}-{mode}"
    factory = subprocess.Popen if popen is None else popen
    parent_pid = os.getpid()
    admitted = capacity.admit_before_spawn(key, peak_mb / 1024,
                                           register_pid=parent_pid, session_id=sid)
    if not admitted.admitted:
        raise ValueError(admitted.reason)
    proc = None
    try:
        if not slot_grant.check_slot_grant(sid, run, peak_mb=admitted.peak_gib * 1024)[0]:
            raise ValueError("Group EM grant does not cover recorded worker peak")
        env = _launch_identity_env(settings_home_child_env(_child_env(False)), provider, sid, argv)
        if provider == "codex":
            try:
                env = shape_codex_child(env, writable_root=repo)
            except SandboxSetupRefused as exc:
                raise ValueError(str(exc)) from exc
        proc = factory(argv, cwd=repo, env=env, stdin=subprocess.PIPE, stdout=stdout,
                       text=True, encoding="utf-8", errors="replace", shell=False,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        # Keep the checked parent reservation until child completion. The legacy
        # register_spawn helper swallows write failures, so transferring to it
        # would introduce an unreserved-child gap. The supervisor stays alive
        # throughout communicate; Group EM START separately names the child PID.
        roster.write_record(proc.pid, f"{mode}-{uuid.uuid4().hex[:12]}", mode, str(repo),
                            sid, provider=provider)
        if on_spawn is not None:
            on_spawn(proc.pid)
        posted = admission_post.post("START", sid, pids=[proc.pid], run=run,
                                      declared_peak_mb=admitted.peak_gib * 1024)
        if not posted.get("delivered"):
            raise ValueError("worker START admission could not reach Group EM")
        proc.communicate(prompt)
        return proc.returncode
    except BaseException:
        if proc is not None:
            _cleanup(proc.kill)
            _cleanup(proc.communicate)
        raise
    finally:
        _cleanup(capacity.release_spawn, key, parent_pid)
        if proc is not None:
            _cleanup(roster.remove_record, proc.pid)
            _cleanup(admission_post.post, "END", sid, pids=[proc.pid], run=run)


def _cleanup(function, *args, **kwargs) -> None:
    """Run every cleanup leg without replacing the child result or primary error."""
    try:
        function(*args, **kwargs)
    except Exception as exc:
        print(f"worker cleanup: {function.__name__}: {type(exc).__name__}", file=sys.stderr)

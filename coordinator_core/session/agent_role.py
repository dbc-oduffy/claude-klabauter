"""Audience classification is separate from job mode and grants no engine authority."""
from __future__ import annotations

import uuid

ROLE_ENV = "COORDINATOR_AGENT_ROLE"
ROLES = frozenset({"em", "worker", "workflow-agent", "reviewer", "unknown"})
SOURCES = frozenset({"startup", "resume", "clear", "compact", "fork"})


def _engine_issued_contract(env):
    """True when env carries what `independent_child_env` stamps on an engine child."""
    return bool(env.get(ROLE_ENV) or env.get("COORDINATOR_PARENT_SESSION_ID"))


def classify(payload, env, *, native_main=False, exec_worker=False, native_role=None):
    if payload.get("hook_event_name") == "SubagentStart" or payload.get("agent_id"):
        return "worker"
    if str(env.get("COORDINATOR_AGENT_WORKER", "")).lower() in ("1", "true", "yes"):
        return "worker"
    explicit = env.get(ROLE_ENV)
    if explicit:
        if not isinstance(explicit, str) or explicit not in ROLES:
            return "unknown"
        if explicit != "em":
            return explicit
        if exec_worker:
            return "reviewer" if native_role == "reviewer" else "worker"
    if exec_worker:  # reached only without an explicit ROLE_ENV
        if native_role == "reviewer":
            return "reviewer"
        if _engine_issued_contract(env):
            return "worker"
        native_main = True  # root `codex exec`: no engine-issued contract, so not an engine child.
    if env.get("COORDINATOR_JOB_MODE") in ("blitz", "cron"):
        return "unknown"
    if not native_main or payload.get("hook_event_name") != "SessionStart" or payload.get("source") not in SOURCES:
        return "unknown"
    try:
        uuid.UUID(payload["session_id"])
    except (ValueError, TypeError, KeyError, AttributeError):
        return "unknown"
    return "em"


# Arity observed with local `codex --help` (2026-11-08). Unknown options fail
# unclassified instead of guessing whether a later `exec` token is a value.
_VALUE_OPTIONS = frozenset({"--config", "--enable", "--disable", "--remote",
    "--remote-auth-token-env", "--image", "--model", "--local-provider", "--profile",
    "--sandbox", "--cd", "--add-dir", "--ask-for-approval"})
_SHORT_VALUE = frozenset({"c", "i", "m", "p", "s", "C", "a"})
_SWITCH_OPTIONS = frozenset({"--strict-config", "--oss", "--approve-for-me",
    "--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust",
    "--worktree", "--search", "--no-alt-screen", "--no-daemon"})
_OTHER_COMMANDS = frozenset({"login", "logout", "mcp", "plugin", "remote-control", "app",
    "completion", "update", "doctor", "sandbox", "debug", "apply", "a", "queue", "archive",
    "delete", "migrate-rollouts", "migrate-nfs-db", "unarchive", "cloud", "exec-server", "features", "help"})


def observed_cli_audience(argv):
    """Classify observed native argv without mistaking option values for commands."""
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(arg, str) for arg in argv):
        return "unknown"
    index = 1
    image_seen = False
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            return "em" if len(argv) - index <= 2 else "unknown"
        key, equal, value = arg.partition("=")
        if key in _VALUE_OPTIONS:
            if equal:
                if not value:
                    return "unknown"
                index += 1
            else:
                if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
                    return "unknown"
                index += 2
            image_seen |= key == "--image"
            continue
        if len(arg) >= 2 and arg[0] == "-" and arg[1] in _SHORT_VALUE:
            if len(arg) == 2:
                if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
                    return "unknown"
                index += 2
            else:
                index += 1
            image_seen |= arg[1] == "i"
            continue
        if arg in _SWITCH_OPTIONS:
            index += 1
            continue
        if arg.startswith("-"):
            return "unknown"
        # --image accepts multiple files. A subsequent bare token may be a file
        # or a subcommand; refuse EM classification for that ambiguous shape.
        if image_seen:
            return "unknown"
        if arg in ("exec", "e"):
            return "worker"
        if arg in ("review", "r"):
            return "reviewer"
        if arg in ("app-server", "resume", "fork", "agents"):
            return "em"
        if arg in _OTHER_COMMANDS:
            return "unknown"
        return "em" if index == len(argv) - 1 else "unknown"  # One interactive prompt.
    return "em"


def observed_cli_launch(argv):
    role = observed_cli_audience(argv)
    return {"native_main":role == "em", "exec_worker":role in ("worker", "reviewer"),
            "native_role":role}


def native_launch():
    """Observed native ancestry only; never infer a role from a PID alone."""
    try:
        import os
        import psutil
        for process in psutil.Process(os.getpid()).parents():
            if process.name().lower().removesuffix(".exe") == "codex":
                argv = process.cmdline()
                return observed_cli_launch(argv)
    except Exception:
        pass
    return observed_cli_launch([])

def role_for_caller(caller_env, session_id):
    """Return caller authority from carried env plus its own validated host row.

    Explicit non-EM markers always win over any parent/daemon EM identity.
    `em` is returned only for the exact live registered caller whose row was
    classified as EM by its native SessionStart. Missing/invalid state is unknown.
    A workflow-agent still needs the engine's assigned-script delegation gate.
    """
    env = caller_env or {}
    if str(env.get("COORDINATOR_AGENT_WORKER", "")).lower() in ("1", "true", "yes"):
        return "worker"
    explicit = env.get(ROLE_ENV)
    if explicit and explicit != "em":
        return explicit if isinstance(explicit, str) and explicit in ROLES else "unknown"
    from coordinator_core.session import external_registry
    row = external_registry.lookup(session_id)
    if not isinstance(row, dict) or row.get("session_id") != session_id:
        return "unknown"
    role = row.get("role", "unknown")
    return role if isinstance(role, str) and role in ROLES else "unknown"


role_for_event = classify

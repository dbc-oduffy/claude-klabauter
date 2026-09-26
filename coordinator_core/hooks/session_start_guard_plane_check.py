
from __future__ import annotations

import json
import os

from coordinator_core.hooks._envelope import context_only, no_advisory
from coordinator_core.ipc import register_op

_COORDINATOR_MARKERS = ("coordinator", "guard-", "guard_")

_SELF_STEM = "session_start_guard_plane_check"

_GUARD_EVENTS = frozenset({"PreToolUse", "PostToolUse"})

_REMEDIATION = (
    "coordinator/templates/cloud-env/setup.sh, pasted into the environment's "
    "Setup script box at claude.ai/code -- an operator surface, not a session one"
)


def _settings_candidates() -> "list[str]":
    """Every settings file that could register a hook for this session, in
    resolution order. `CLAUDE_CONFIG_DIR` wins where it is set; otherwise the
    user's home carries the one that matters."""
    home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude"
    )
    project = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return [
        os.path.join(home, "settings.json"),
        os.path.join(project, ".claude", "settings.json"),
        os.path.join(project, ".claude", "settings.local.json"),
    ]


def count_coordinator_hooks(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0
    found = 0
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    for event, matchers in hooks.items():
        if event not in _GUARD_EVENTS:
            continue
        if not isinstance(matchers, list):
            continue
        for matcher in matchers:
            if not isinstance(matcher, dict):
                continue
            for hook in matcher.get("hooks") or []:
                if not isinstance(hook, dict):
                    continue
                command = hook.get("command")
                if not isinstance(command, str) or _SELF_STEM in command:
                    continue
                if any(marker in command for marker in _COORDINATOR_MARKERS):
                    found += 1
    return found


def build_report() -> "str | None":
    """The context block to emit, or `None` when this session is not one this
    check speaks to — silent outside a remote session (`CLAUDE_CODE_REMOTE`
    unset/not "true"), matching the source script's own silence rule (a
    `--plugin-dir` launch registers nothing on disk and would otherwise read
    as a false absence)."""
    if os.environ.get("CLAUDE_CODE_REMOTE") != "true":
        return None

    scanned = _settings_candidates()
    present = [path for path in scanned if os.path.isfile(path)]
    registered = sum(count_coordinator_hooks(path) for path in present)

    if registered:
        return (
            f"Coordinator guard plane: {registered} hook(s) registered for this "
            "remote session."
        )

    return (
        "Coordinator guard plane: no hook registered this remote session -- "
        "gated writes are ungated here, silently. Provision instead: "
        "`coordinator/templates/cloud-env/setup.sh` in claude.ai/code's "
        "Setup script box."
    )


@register_op("hooks.session_start_guard_plane_check")
def _handler(params: dict, repo_root=None) -> dict:
    try:
        report = build_report()
    except Exception:
        return no_advisory()
    if not report:
        return no_advisory()
    return context_only("SessionStart", report)

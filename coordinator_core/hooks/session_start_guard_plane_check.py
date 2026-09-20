"""coordinator_core.hooks.session_start_guard_plane_check — SessionStart(*) op:
reports whether a REMOTE session has any coordinator PreToolUse/PostToolUse
hook registered for it at all.

Arrival note (W4-C10, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `.claude/hooks/session_start_guard_plane_check.py` — DoE's own
repo-local hook (registered in DoE's own `.claude/settings.json`, not
`hooks.json`; per the row's own body, "it moves the same way and DoE rewires
its own settings"). The source script is stdlib-only, self-contained, and
disk-scan-based (settings.json files, never a live registry) — nothing about
its logic is DoE-plane-resident, so this is a near-verbatim port: the
`hookSpecificOutput` envelope construction is replaced with this package's own
`_envelope.context_only`, and `main()`'s bare-stdin-drain plus `print(...)`
becomes an async `@register_op` handler returning the envelope dict directly
instead of printing it.

Op contract: `params` is the flat SessionStart payload dict (`session_id`,
`source`, `cwd`, ...) — none of its fields are read; this op derives every
fact from `os.environ` and on-disk settings files, exactly as the source
script did from its own process environment. Returns one `hookSpecificOutput`
envelope (`context_only("SessionStart", ...)`) when the check has something to
say, `no_advisory()` otherwise (silent outside a remote session, or a healthy
remote session's report — see `build_report()`'s own docstring for why
silence is the correct behavior for a workstation launch).

Negative-spec:
    Does NOT read a live coordinator hook registry or invoke any guard body —
    it counts `command` entries naming a coordinator surface in on-disk
    settings.json files only, per the source script's own "WHAT IT DOES NOT
    DO" section.
    Does NOT spawn a subprocess — stdlib `json`/`os` only, matching the source
    script's "Stdlib only, no subprocess" contract.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C10
"""

from __future__ import annotations

import json
import os

from coordinator_core.hooks._envelope import context_only, no_advisory
from coordinator_core.ipc import register_op

_COORDINATOR_MARKERS = ("coordinator", "guard-", "guard_")

#: This module's own stem — excluded from its own count so a registration of
#: this very hook does not count itself as "a coordinator hook is present".
_SELF_STEM = "session_start_guard_plane_check"

#: The hook events that actually gate a tool call. A coordinator registration
#: on any other event says nothing about whether a write would be stopped.
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
    """Registered hook commands in `path` that name a coordinator surface.

    A file that is absent, unreadable, or not JSON contributes zero and is
    not an error — the absence is the finding this function reports, so
    raising on it would replace a fact with a stack trace."""
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
async def _handler(params: dict, repo_root=None) -> dict:
    """SessionStart(*) op — see module docstring for the report contract.
    Never raises: `build_report()`'s own internal reads are already
    exception-scoped per-file; a failure here degrades to `no_advisory()`."""
    try:
        report = build_report()
    except Exception:
        return no_advisory()
    if not report:
        return no_advisory()
    return context_only("SessionStart", report)

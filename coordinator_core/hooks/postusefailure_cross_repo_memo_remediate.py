"""coordinator_core.hooks.postusefailure_cross_repo_memo_remediate —
PostToolUseFailure warm-engine op.

Port of: coordinator-content-repo coordinator/hooks/scripts/postusefailure-cross-repo-memo-
remediate.py. Fires for exactly ONE bounded case: a `Bash` tool call running a
`cross-repo-memo` invocation that exits 127 ("command not found"), and offers
the resolved forwarder path. ALWAYS returns an advisory-only result — never a
block/deny — matching the DoE shim's unconditional exit 0.

No module-level mutable state; every call re-reads the settings-home forwarder
fresh (per DR-warm-hook-miss-policy / the adapter-shape spike: ported ops hold
no state across calls).
"""

from __future__ import annotations

import re

from coordinator_core._hook_envelope import no_advisory, payload_of
from coordinator_core._settings_home import settings_home
from coordinator_core.hooks._payload import field
from coordinator_core.hooks._envelope import context_only
from coordinator_core.hooks.support.forwarder_resolve import resolve_forwarder
from coordinator_core.ipc import register_op

_EXIT_127_RE = re.compile(r"(?m)^Exit code 127\b")
_MEMO_SUBSTRING = "cross-repo-memo"


def _compose_remediation() -> str:
    forwarder = None
    try:
        forwarder = resolve_forwarder(settings_home() / "bin", "cross-repo-memo")
    except Exception:
        forwarder = None

    if forwarder:
        return (
            "[cross-repo-memo remediation] exit 127: not on PATH. Invoke the "
            f"resolved forwarder directly: `{forwarder}`."
        )
    return (
        "[cross-repo-memo remediation] exit 127: not on PATH, and no "
        "settings-home forwarder resolved -- confirm `bin/cross-repo-memo` "
        "is present under `<settings-home>/bin/`."
    )


@register_op("hooks.postusefailure_cross_repo_memo_remediate")
def _handler(params: dict, repo_root=None) -> dict:
    params = payload_of(params)
    try:
        if field(params, "tool_name") != "Bash":
            return no_advisory()

        error = field(params, "error")
        if not isinstance(error, str) or not error or not _EXIT_127_RE.search(error):
            return no_advisory()

        tool_input = params.get("tool_input")
        if not isinstance(tool_input, dict):
            return no_advisory()
        command = tool_input.get("command")
        if not isinstance(command, str) or _MEMO_SUBSTRING not in command:
            return no_advisory()

        message = _compose_remediation()
    except Exception:
        return no_advisory()

    return context_only("PostToolUseFailure", message)

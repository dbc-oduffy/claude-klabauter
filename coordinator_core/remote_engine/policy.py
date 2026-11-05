"""Which coordinator ops may be retried on a remote box, and the reply envelope.

Fail-closed: an op with no classification, or a local-only op, is never remote-safe.
"""

from __future__ import annotations

import re
from typing import Any

from coordinator_core.authz.classification import OpClass, classify

# Ops that must run on the caller's own box: the file-cache trim and anything reading
# caller-local state (the governor view). Matched by exact name or by prefix.
LOCAL_ONLY: frozenset[str] = frozenset(
    {
        "trim-file-cache",
        "trim_file_cache",
        "BoxCacheTrim",
    }
)
LOCAL_ONLY_PREFIXES: tuple[str, ...] = (
    "governor.",
    "governor_",
    "trim-file-cache",
    "trim_file_cache",
    "file_cache_trim",
)

# Phase 2 mutating ops cleared to run remotely: mint/land only. The far engine runs them in its own
# tree, locks and git, so the run lands on the SERVING box's daily branch (multibox Placement rule).
# Never add a caller-local-state op: LOCAL_ONLY wins over this list.
PHASE2_MUTATING_ALLOWLIST: frozenset[str] = frozenset(
    {
        "hooks.submit_engine_ask",
        "dispatch.emit",
    }
)

_PATH_KEYS: frozenset[str] = frozenset({"cwd", "repo_root", "files"})
_PATH_KEY_SUFFIXES: tuple[str, ...] = ("path", "paths", "_dir", "_root", "_file", "_files")
_LOCAL_PATH_RE = re.compile(r"^(/|~($|[\\/])|[A-Za-z]:($|[\\/])|\\\\)")


def _any_local_string(value: Any) -> bool:
    """True when `value`, or any string nested in a list/tuple/dict under it, names a local path."""
    if isinstance(value, str):
        return bool(_LOCAL_PATH_RE.match(value))
    if isinstance(value, (list, tuple)):
        return any(_any_local_string(v) for v in value)
    if isinstance(value, dict):
        return any(_any_local_string(v) for v in value.values())
    return False


def is_phase2_mutating(op: str) -> bool:
    """True when `op` is an allowlisted mutating op whose far run must never be repeated locally."""
    return op in PHASE2_MUTATING_ALLOWLIST


def far_result_indeterminate(op: str, res: Any) -> bool:
    """True when `op` is phase-2 mutating and `res` is not a clean far reply (ok with a dict reply).

    The far op may have run, so the caller must not re-run it locally.
    """
    if not is_phase2_mutating(op):
        return False
    clean = isinstance(res, dict) and bool(res.get("ok")) and isinstance(res.get("reply"), dict)
    return not clean


def carries_local_path(params: Any) -> bool:
    """True when a path-typed param value (recursively) names a caller-local filesystem path.

    Only keys `cwd`, `repo_root`, `files` and those ending `path`, `paths`, `_dir`, `_root`,
    `_file`, `_files` are tested, and every string under such a key (lists included) is checked
    for `/`, `~`, `X:` and UNC prefixes; free-text values (a prompt beginning with a slash
    command) are never inspected.
    """
    if isinstance(params, dict):
        for key, value in params.items():
            if isinstance(key, str) and (key in _PATH_KEYS or key.endswith(_PATH_KEY_SUFFIXES)):
                if _any_local_string(value):
                    return True
            if carries_local_path(value):
                return True
    elif isinstance(params, (list, tuple)):
        return any(carries_local_path(v) for v in params)
    return False


def is_local_only(op: str) -> bool:
    """True when `op` names a trim or caller-local-state op that never routes remotely."""
    return op in LOCAL_ONLY or op.startswith(LOCAL_ONLY_PREFIXES)


def remote_safe(op: str) -> bool:
    """True when `op` may be retried once on a remote box.

    Local-only ops are refused even if allowlisted; an unclassified op is refused.
    """
    if is_local_only(op):
        return False
    if op in PHASE2_MUTATING_ALLOWLIST:
        return True
    try:
        return classify(op) is OpClass.COMPUTE_ONLY
    except KeyError:
        return False


def placement(op: str, served_by: str, reply: Any) -> dict[str, Any]:
    """Placement labels for a Phase 2 reply: the serving box and the branch its engine landed on.

    Empty for a read-only op. The branch is read from the far reply, never inferred from the caller.
    """
    if op not in PHASE2_MUTATING_ALLOWLIST:
        return {}
    branch = reply.get("branch") if isinstance(reply, dict) else None
    return {"placement": "serving-box-daily-branch", "serving_box": served_by, "branch": branch}


def reply_envelope(served_by: str, latency_ms: float) -> dict[str, Any]:
    """Telemetry labels attached to a reply served by a remote box."""
    return {"served_by": served_by, "latency_ms": float(latency_ms), "remote": True}

"""The grind mode contract of ``dispatch.terminal_commit``: params key, row shape, row trailer.

Shared by the op that lands a grind's finished rows and by the emitter that composes the call, so it
imports nothing beyond the standard library.

Handler reply shape: ``{committed, sha, rows: [{row_id, outcome, sha}],
dropped: [{row_id, path, reason}], error, refused}``. A path the commit cannot land is dropped with
a ``DROP_*`` reason, never refused; only a malformed path refuses the call.
"""

from __future__ import annotations

from typing import Optional

GRIND_COMMIT_PARAM = "grind"
ROW_TRAILER_KEY = "Grind-Row"
LEDGER_ROOT = "state/queue-grind/"
DROP_LEDGER_ROOT = "ledger-root"
DROP_UNSEEN_DELETION = "deletion-git-cannot-see"


def partition_ledger_paths(paths: list[str]) -> tuple[list[str], list[str]]:
    """Split ``paths`` into ``(kept, dropped)``; dropped is every path under ``LEDGER_ROOT``. Order kept."""
    kept: list[str] = []
    dropped: list[str] = []
    for path in paths:
        (dropped if path.startswith(LEDGER_ROOT) else kept).append(path)
    return kept, dropped


def _squash(value: str) -> str:
    return " ".join(value.split())


def render_row_trailer(row_id: str, outcome: str) -> str:
    """One finished row's trailer line: ``Grind-Row: <row_id> <outcome>``, whitespace collapsed."""
    return f"{ROW_TRAILER_KEY}: {_squash(row_id)} {_squash(outcome)}"


def is_grind_call(params: object) -> bool:
    """True when ``params[GRIND_COMMIT_PARAM]`` is a dict whose ``rows`` is a list."""
    grind = params.get(GRIND_COMMIT_PARAM) if isinstance(params, dict) else None
    return isinstance(grind, dict) and isinstance(grind.get("rows"), list)


def _bad_path(path: object) -> Optional[str]:
    if not isinstance(path, str) or not path:
        return f"path {path!r} is not a non-empty str"
    if path.startswith("/") or "\\" in path or (len(path) > 1 and path[1] == ":"):
        return f"path {path!r} is not repo-relative POSIX"
    if ".." in path.split("/"):
        return f"path {path!r} contains '..'"
    return None


def _path_list(row_id: str, field: str, value: object) -> Optional[str]:
    if not isinstance(value, list):
        return f"row {row_id!r} {field} is not a list"
    for path in value:
        bad = _bad_path(path)
        if bad:
            return f"row {row_id!r} {field}: {bad}"
    return None


def validate_grind_params(value: object) -> Optional[str]:
    """A refusal string when the ``grind`` params value is malformed, else None."""
    if not isinstance(value, dict):
        return "params.grind is not an object"
    profile = value.get("profile")
    if not isinstance(profile, str) or not profile:
        return "params.grind.profile is not a non-empty str"
    rows = value.get("rows")
    if not isinstance(rows, list):
        return "params.grind.rows is not a list"
    seen: set = set()
    for row in rows:
        if not isinstance(row, dict):
            return f"params.grind row {row!r} is not an object"
        row_id = row.get("row_id")
        outcome = row.get("outcome")
        if not isinstance(row_id, str) or not row_id:
            return f"params.grind row_id {row_id!r} is not a non-empty str"
        if not isinstance(outcome, str) or not outcome:
            return f"row {row_id!r} outcome {outcome!r} is not a non-empty str"
        if row_id in seen:
            return f"row id {row_id!r} is duplicated"
        seen.add(row_id)
        for field in ("paths", "deleted_paths"):
            bad = _path_list(row_id, field, row.get(field))
            if bad:
                return bad
    halted = value.get("halted")
    if halted is not None and (not isinstance(halted, str) or not halted):
        return "params.grind.halted is not absent, null or a non-empty str"
    return None

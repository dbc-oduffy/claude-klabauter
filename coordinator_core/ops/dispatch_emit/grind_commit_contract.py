"""The grind mode contract of ``dispatch.terminal_commit``: params key, row shape, row trailer.

Shared by the op that lands a grind's finished rows and by the emitter that composes the call, so it
imports nothing beyond the standard library.

Handler reply shape: ``{committed, sha, rows: [{row_id, outcome, sha}],
dropped: [{row_id, path, reason}], error, refused, already_landed, unlanded}``. A path the commit
cannot land is dropped with a ``DROP_*`` reason, never refused; only a malformed path refuses the call.
``already_landed`` (``ALREADY_LANDED_KEY``) is true only on a repeat call answered from the run
token's spent receipt: ``sha`` and ``rows`` are the first landing's, nothing landed again.
``unlanded`` (``UNLANDED_KEY``) is ``[{row_id, reason}]`` for each requested row the landed commit
did not carry; the reason names the landed sha. Both keys are absent on a first landing.
"""

from __future__ import annotations

import json
from typing import Optional
from urllib.parse import quote, unquote

GRIND_COMMIT_PARAM = "grind"
ALREADY_LANDED_KEY = "already_landed"
UNLANDED_KEY = "unlanded"
ROW_TRAILER_KEY = "Grind-Row"
LEDGER_ROOT = "state/queue-grind/"
DROP_LEDGER_ROOT = "ledger-root"
DROP_UNSEEN_DELETION = "deletion-git-cannot-see"

# Run-close contract: the ``grind-row run-record`` flags, the decisions.json keys and the record shape.
DECLARED_FLAG = "declared-urlenc"
DECISIONS_FLAG = "decisions-file"
QUEUE_FLAG = "queue"
BASELINE_DIRTY_KEY = "baseline_dirty"
MINTED_KEY = "minted"
SWEEP_FLAG = "sweep"
# Twinned by grind_rows run-record --sweep (the record) and grind_compose's run close (the hand-back).
SWEEP_FAILED_HEAD = "drain sweep did not complete"
RUN_RECORD_REQUIRED = ("profile", "run_id", "counts", "spend", "handed_back", "halted")

# Records a fix stage must never write: history, not work product.
HISTORICAL_RECORD_PREFIXES = (
    "state/warp/",
    "docs/plans/",
    "state/handoffs/",
    "state/sizings/",
    "docs/decisions/",
)

_SHARED_WRITE_ROOTS = ("state/sizings/", "state/handoffs/", "state/apm-rulings/")


def _dir_root(path: str) -> str:
    return path if path.endswith("/") else path + "/"


def run_write_roots(profile: str, queue_dirs: list[str]) -> tuple[str, ...]:
    """Which directories may a grind run's close commit sweep: the queue dirs, the profile's
    ledger dir and the shared sizing, baton and ruling dirs, each ending in ``/``, deduplicated in order."""
    roots = [_dir_root(d) for d in queue_dirs]
    roots.append(f"{LEDGER_ROOT}{profile}/")
    roots.extend(_SHARED_WRITE_ROOTS)
    return tuple(dict.fromkeys(roots))


def under_roots(path: str, roots: tuple[str, ...] | list[str]) -> bool:
    """True when the repo-relative POSIX ``path`` lies under any of ``roots``."""
    return any(path.startswith(root) for root in roots)


def is_historical_record(path: str) -> bool:
    """May a fix stage write ``path``? False exactly when it lies under ``HISTORICAL_RECORD_PREFIXES``."""
    return path.startswith(HISTORICAL_RECORD_PREFIXES)


def encode_declared_paths(paths: list[str]) -> str:
    """Render ``paths`` as the ``--declared-urlenc`` token: percent-encoded JSON ``{"paths": [...]}``
    with ``'`` encoded too, so it is one shell-safe single-quoted token."""
    return quote(json.dumps({"paths": list(paths)}, sort_keys=True), safe="")


def decode_declared_paths(token: str) -> list[str]:
    """Inverse of ``encode_declared_paths``; returns ``sorted(set(paths))``, empty when none were
    declared. Raises ``ValueError`` on malformed JSON, a wrong key set or a non-string entry."""
    try:
        obj = json.loads(unquote(token))
    except json.JSONDecodeError as exc:
        raise ValueError(f"declared paths token is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict) or set(obj) != {"paths"}:
        raise ValueError("declared paths token must be an object with exactly the key 'paths'")
    paths = obj["paths"]
    if not isinstance(paths, list):
        raise ValueError("declared paths token 'paths' must be a list")
    for entry in paths:
        if not isinstance(entry, str) or not entry:
            raise ValueError("declared paths token entries must be non-empty strings")
    return sorted(set(paths))


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

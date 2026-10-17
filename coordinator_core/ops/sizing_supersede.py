"""coordinator_core.ops.sizing_supersede — JSON-RPC "sizing.supersede".

Moves a `routed` or `draft` sizing to `superseded` and records why in `superseded_note`:
the applier for a routed sizing whose run was refused or replaced by a later
sizing, where no decision record exists for `sizing.decline` to cite, and for an
intake-minted draft whose work landed elsewhere before routing — no other op can
close a draft.

Writes exactly two fields (`status`, `superseded_note`). Idempotent: an already
`superseded` sizing is left byte-identical and keeps its first note, including
when it has none (no backfill). Any status other than `routed`/`draft` is refused.

Negative spec: no successor parameter, no cascade, no archive, no commit, no
touch of `plan`, `baton`, `pm_resolution` or `declined_note`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from coordinator_core.frontmatter.primitives import (
    read_fm_field_unquoted,
    replace_fm_field,
    replace_fm_field_raw,
)
from coordinator_core.frontmatter.schema_validate import format_validation_errors
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.sizing_ship import _err, _validate_sizing_fm
from coordinator_core.session import record_homes


_SUPERSEDABLE_FROM = frozenset({"routed", "draft"})


@register_op("sizing.supersede")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Params: `sizing_path` (under state/sizings/), `note`. Both required.

    Plain `def`: every step blocks, so an `async def` would make the dispatch
    timeout unenforceable.

    exit_code 0 applied True  — routed/draft -> superseded, superseded_note written.
    exit_code 0 applied False — already superseded; file untouched.
    exit_code 1               — bad params, path outside state/sizings/, not a
                                file, status not routed/draft, schema refusal, lock
                                timeout.
    """
    sizing_path_raw = (params.get("sizing_path") or "").strip()
    note = (params.get("note") or "").strip()
    if not sizing_path_raw:
        return _err("missing required param: sizing_path")
    if not note:
        return _err("missing required param: note")
    if repo_root is None:
        return _err("sizing.supersede: repo_root is required")

    worktree = main_worktree_root(repo_root)
    p = Path(sizing_path_raw)
    if not p.is_absolute():
        p = worktree / p
    p = contained_path(p, [Path(record_homes.home_dir(str(worktree), "sizings"))])
    if p is None:
        return _err(f"sizing_path escapes state/sizings/: {sizing_path_raw!r}")
    if not p.is_file():
        return _err(f"sizing-object not found on disk: {sizing_path_raw}")

    state = {"applied": False, "from": ""}

    def mutate(old_text: str) -> str:
        status = read_fm_field_unquoted(old_text, "status")
        if status == "superseded":
            return old_text
        if status not in _SUPERSEDABLE_FROM:
            raise MutateAbort(f"refusing to supersede {p}: status is {status!r}, not 'routed' or 'draft'")
        state["from"] = status
        new_text = replace_fm_field(old_text, "status", "superseded")
        quoted = json.dumps(note, ensure_ascii=False)
        if read_fm_field_unquoted(new_text, "superseded_note") is None:
            sep = "" if new_text.endswith("\n") else "\n"
            new_text = f"{new_text}{sep}superseded_note: {quoted}\n"
        else:
            new_text = replace_fm_field_raw(new_text, "superseded_note", quoted)
        errors = _validate_sizing_fm(new_text)
        if errors:
            raise MutateAbort(
                f"supersede: post-mutation schema validation failed: "
                f"{format_validation_errors(errors)}"
            )
        state["applied"] = True
        return new_text

    try:
        locked_rmw(p, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"sizing-object not found: {p}")
    except LockTimeout as exc:
        return _err(f"timed out waiting for file lock on {p}: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "supersede: mutation aborted")

    if state["applied"]:
        return {
            "exit_code": 0,
            "applied": True,
            "message": f"{sizing_path_raw} superseded (status: {state['from']} -> superseded)",
        }
    return {
        "exit_code": 0,
        "applied": False,
        "message": f"{sizing_path_raw} already superseded — note kept, no write",
    }

"""
coordinator_core.ops.sizing_record_register — JSON-RPC "sizing.record_register".

Purpose: the single writer for a sizing-object's `requirement_register` block, taken from a
caller-supplied `{sources, rows[, rollup]}` mapping (`sizing-assemble --register <yaml>`).

What it writes: the register block only; `sources` and `rows` are kept as given and the
rollup is recomputed, so an input rollup is never trusted. Every byte outside the block is kept.

Re-record invariant:
  - identical sources+rows on disk -> idempotent no-op (the rollup timestamp alone is not a change).
  - different register, no supersede -> refused.
  - different register, supersede    -> replaced; engine-written row state is discarded.

Negative-spec: does NOT infer rows, rulings or statuses, and does NOT git-commit, cascade or
spawn a process.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from coordinator_core.frontmatter.schema_validate import (
    format_validation_errors,
    validate_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.session import record_homes

_SIZING_SCHEMA_PATH: Path = (
    Path(__file__).parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
)

_PARAMS_HINT = (
    "params: sizing (required, path under the sizings home), register (required mapping "
    "{sources, rows[, rollup]}), supersede"
)


def _err(msg: str) -> dict:
    return {"exit_code": 1, "applied": False, "error": msg, "message": msg}


def _identity(sources, rows) -> dict:
    return {"sources": sources, "rows": rows}


@register_op("sizing.record_register")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "sizing.record_register" handler.

    Plain `def`, deliberately not `async def`: every step is blocking (path stats,
    `locked_rmw`), so an `async def` would make DISPATCH_TIMEOUT_SECS unenforceable.

    Params:
        sizing    (str)     — path to the sizing-object under the sizings home. Required.
        register  (mapping) — `{sources, rows[, rollup]}`. Required.
        supersede (bool)    — replace a different register already on the sizing.

    Returns: {exit_code, applied, message|error, rollup?}.

    Exit-code contract:
        exit_code 1 — a missing param; a non-mapping register; a sizing escaping the sizings
                      home or absent; duplicate row ids; a deferred/waived row without a
                      ruling; a different register with `supersede` unset; schema validation
                      failure; lock timeout.
        exit_code 0, applied True  — the register was written.
        exit_code 0, applied False — identical sources and rows already recorded; no-op.
    """
    sizing_raw = params.get("sizing")
    sizing_raw = sizing_raw.strip() if isinstance(sizing_raw, str) else ""
    register = params.get("register")
    supersede = bool(params.get("supersede"))

    if not sizing_raw:
        return _err(f"missing required param: sizing — {_PARAMS_HINT}")
    if register is None or register == "" or register == {}:
        return _err(f"missing required param: register — {_PARAMS_HINT}")
    if not isinstance(register, dict):
        return _err("register must be a mapping {sources, rows[, rollup]}")
    rows = register.get(rr.ROWS_KEY)
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        return _err("register.rows must be a list of row mappings")
    if repo_root is None:
        return _err(
            "sizing.record_register: repo_root is required "
            "(no founding root available — handler called without socket-authoritative common_dir)"
        )

    dupes = rr.duplicate_row_ids(rows)
    if dupes:
        return _err(f"duplicate row ids: {', '.join(dupes)}")
    unruled = rr.unruled_rows(rows)
    if unruled:
        return _err(
            f"a deferred/waived row needs ruling {{source, quote, ref, on}}: {', '.join(unruled)}"
        )

    worktree = main_worktree_root(repo_root)
    p = Path(sizing_raw)
    if not p.is_absolute():
        p = worktree / p
    p = contained_path(p, [Path(record_homes.home_dir(str(worktree), "sizings"))])
    if p is None:
        return _err(f"sizing escapes the sizings home: {sizing_raw!r}")
    if not p.is_file():
        return _err(f"sizing-object not found on disk: {sizing_raw}")

    now = datetime.now(timezone.utc)
    # locked_rmw reads universal-newline text, so the CRLF of the file is invisible to mutate.
    crlf = b"\r\n" in p.read_bytes()
    state: dict = {"applied": False, "rollup": None}

    def mutate(old_text: str) -> str:
        existing = rr.read_register(old_text)
        wanted = _identity(register.get("sources"), rows)
        if existing is not None:
            if _identity(existing.raw.get("sources"), existing.rows) == wanted:
                state["rollup"] = existing.rollup
                return old_text
            if not supersede:
                raise MutateAbort(
                    f"refusing to record on {p}: a different requirement_register is present "
                    "— pass supersede (--supersede) to replace it; this discards engine-written "
                    "row state (wired, met_by, last_progress_at)"
                )
        text = rr.write_register_text(old_text, register, now)
        try:
            new_doc = yaml.safe_load(text) or {}
        except Exception as exc:  # noqa: BLE001
            raise MutateAbort(f"record_register: post-mutation YAML parse error: {exc}") from exc
        errors = validate_frontmatter(new_doc, _SIZING_SCHEMA_PATH)
        if errors:
            raise MutateAbort(
                "record_register: post-mutation schema validation failed: "
                f"{format_validation_errors(errors)}"
            )
        written = rr.read_register(text)
        state["rollup"] = written.rollup if written is not None else None
        state["applied"] = True
        return text.replace("\n", "\r\n") if crlf else text

    try:
        locked_rmw(p, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"sizing-object disappeared before the lock could be acquired: {p}")
    except LockTimeout as exc:
        return _err(f"timed out waiting for file lock on {p}: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "record_register: mutation aborted")
    except Exception as exc:  # noqa: BLE001
        return _err(f"record_register: {type(exc).__name__}: {exc}")

    if state["applied"]:
        return {
            "exit_code": 0,
            "applied": True,
            "message": f"recorded requirement_register ({len(rows)} rows) on {sizing_raw}",
            "rollup": state["rollup"],
        }
    return {
        "exit_code": 0,
        "applied": False,
        "message": f"{sizing_raw} already records this register — idempotent no-op",
        "rollup": state["rollup"],
    }

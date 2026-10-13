"""
coordinator_core.ops.sizing_record_register — JSON-RPC "sizing.record_register".

Purpose: the writer for a sizing-object's `requirement_register` block taken from a
caller-supplied `{sources, rows[, rollup]}` mapping (`sizing-assemble --register <yaml>`).
`sizing_assemble.write_back` also writes a register, derived from the PM's verbatims,
through the same `requirement_register.write_register_text`.

What it writes: the register block only; `sources` and `rows` are kept as given and the
rollup is recomputed, so an input rollup is never trusted. Every byte outside the block is kept.

Re-record invariant:
  - identical sources+rows+unrelated_turns on disk -> idempotent no-op (the rollup timestamp
    alone is not a change).
  - amend -> the input is an overlay on the recorded register: rows merged by id (a given
    field replaces the recorded one; a new id is appended, `status` defaulting to `open`,
    `surface` to `internal`, `claimed_by` to []), `drop: [ids]` removes rows, `unrelated_turns`
    merged by turn, new `sources` appended. With no register recorded the overlay is the
    register, defaults filled. Engine-written state on fields the overlay does not name is kept.
  - different register, no amend, no supersede -> refused.
  - different register, supersede               -> replaced; engine-written row state is discarded.

Negative-spec: does NOT infer rows, rulings or statuses beyond the amend defaults above, and
does NOT git-commit, cascade or spawn a process.
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
    "{sources, rows[, rollup]}; with amend an overlay {rows?, drop?, unrelated_turns?, sources?}), "
    "supersede, amend"
)

_ROW_DEFAULTS = ((rr.ROW_STATUS, rr.STATUS_OPEN), (rr.ROW_SURFACE, "internal"))
_DROP_KEY = "drop"


def _err(msg: str) -> dict:
    return {"exit_code": 1, "applied": False, "error": msg, "message": msg}


def _identity(block) -> dict:
    return {
        "sources": block.get(rr.SOURCES_KEY),
        "rows": block.get(rr.ROWS_KEY),
        "unrelated_turns": block.get(rr.UNRELATED_TURNS_KEY) or [],
    }


def _overlay(recorded: Optional[dict], overlay: dict) -> dict:
    """`overlay` applied to the `recorded` register block (None: nothing recorded), per the
    module docstring's amend rule. Raises ValueError naming what the overlay cannot apply."""
    base = recorded or {}
    rows = [dict(r) for r in (base.get(rr.ROWS_KEY) or []) if isinstance(r, dict)]
    known = {str(r.get(rr.ROW_ID)) for r in rows}
    drop = [str(d) for d in (overlay.get(_DROP_KEY) or [])]
    unknown = [d for d in drop if d not in known]
    if unknown:
        raise ValueError(f"drop names row(s) not on the register: {', '.join(unknown)}")
    rows = [r for r in rows if str(r.get(rr.ROW_ID)) not in drop]
    by_id = {str(r.get(rr.ROW_ID)): r for r in rows}
    for given in overlay.get(rr.ROWS_KEY) or []:
        rid = str(given.get(rr.ROW_ID) or "")
        if not rid:
            raise ValueError("an overlay row carries no id")
        if rid in by_id:
            by_id[rid].update(given)
            continue
        row = dict(given)
        for key, default in _ROW_DEFAULTS:
            row.setdefault(key, default)
        row.setdefault(rr.ROW_CLAIMED_BY, [])
        rows.append(row)
        by_id[rid] = row
    sources = list(base.get(rr.SOURCES_KEY) or [])
    for src in overlay.get(rr.SOURCES_KEY) or []:
        if src not in sources:
            sources.append(src)
    unrelated: dict = {}
    for e in [*(base.get(rr.UNRELATED_TURNS_KEY) or []), *(overlay.get(rr.UNRELATED_TURNS_KEY) or [])]:
        if isinstance(e, dict) and "turn" in e:
            unrelated[e["turn"]] = e
    out: dict = {rr.SOURCES_KEY: sources}
    if unrelated:
        out[rr.UNRELATED_TURNS_KEY] = [unrelated[t] for t in sorted(unrelated, key=lambda t: (not isinstance(t, int), t if isinstance(t, int) else 0, str(t)))]
    out[rr.ROWS_KEY] = rows
    return out


def _coverage_refusal(doc: dict, gaps: list, problems: list) -> str:
    embedded = rr.embedded_turns(doc)
    lines = [f"  turn {t}: {embedded[t][:80]!r}" for t in gaps]
    out = "register leaves PM turn(s) uncovered:\n" + "\n".join(lines) if gaps else ""
    if problems:
        out += ("\n" if out else "") + "; ".join(problems)
    if gaps:
        out += (
            "\nfix: add a row whose source_text is a verbatim substring of the turn, "
            "or an unrelated_turns entry {turn, reason}"
        )
    return out


@register_op("sizing.record_register")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "sizing.record_register" handler.

    Plain `def`, deliberately not `async def`: every step is blocking (path stats,
    `locked_rmw`), so an `async def` would make DISPATCH_TIMEOUT_SECS unenforceable.

    Params:
        sizing    (str)     — path to the sizing-object under the sizings home. Required.
        register  (mapping) — `{sources, rows[, rollup]}`, or with `amend` an overlay. Required.
        supersede (bool)    — replace a different register already on the sizing.
        amend     (bool)    — apply `register` as an overlay on the recorded one; `supersede` wins.

    Returns: {exit_code, applied, message|error, rollup?}.

    Exit-code contract:
        exit_code 1 — a missing param; a non-mapping register; a sizing escaping the sizings
                      home or absent; duplicate row ids; a deferred/waived row without a
                      ruling; an overlay dropping an unknown row; a different register with
                      neither `amend` nor `supersede`; schema validation failure; lock timeout.
        exit_code 0, applied True  — the register was written.
        exit_code 0, applied False — the register on disk already matches; no-op.
    """
    sizing_raw = params.get("sizing")
    sizing_raw = sizing_raw.strip() if isinstance(sizing_raw, str) else ""
    register = params.get("register")
    supersede = bool(params.get("supersede"))
    amend = bool(params.get("amend")) and not supersede

    if not sizing_raw:
        return _err(f"missing required param: sizing — {_PARAMS_HINT}")
    if register is None or register == "" or register == {}:
        return _err(f"missing required param: register — {_PARAMS_HINT}")
    if not isinstance(register, dict):
        return _err("register must be a mapping {sources, rows[, rollup]}")
    rows = register.get(rr.ROWS_KEY)
    if amend and rows is None:
        rows = []
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
    state: dict = {"applied": False, "rollup": None, "rows": len(rows)}

    def mutate(old_text: str) -> str:
        try:
            current = yaml.safe_load(old_text) or {}
        except Exception as exc:  # noqa: BLE001
            raise MutateAbort(f"record_register: YAML parse error: {exc}") from exc
        existing = rr.read_register(old_text)
        wanted = register
        if amend:
            try:
                wanted = _overlay(existing.raw if existing is not None else None, register)
            except ValueError as exc:
                raise MutateAbort(str(exc)) from exc
        unruled = rr.unruled_rows(wanted[rr.ROWS_KEY])
        if unruled:
            raise MutateAbort(
                f"a deferred/waived row needs ruling {{source, quote, ref, on}}: {', '.join(unruled)}"
            )
        state["rows"] = len(wanted[rr.ROWS_KEY])
        if isinstance(current, dict):
            would_be = {**current, rr.REGISTER_KEY: wanted}
            problems = rr.unrelated_turn_errors(current, wanted)
            gaps = rr.coverage_gaps(would_be)
            if gaps or problems:
                raise MutateAbort(_coverage_refusal(current, gaps, problems))
        if existing is not None:
            if _identity(existing.raw) == _identity(wanted):
                state["rollup"] = existing.rollup
                return old_text
            if not supersede and not amend:
                raise MutateAbort(
                    f"refusing to record on {p}: a different requirement_register is present "
                    "— pass supersede (--supersede) to replace it; this discards engine-written "
                    "row state (wired, met_by, last_progress_at)"
                )
        text = rr.write_register_text(old_text, wanted, now)
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
            "message": f"recorded requirement_register ({state['rows']} rows) on {sizing_raw}",
            "rollup": state["rollup"],
        }
    return {
        "exit_code": 0,
        "applied": False,
        "message": f"{sizing_raw} already records this register — idempotent no-op",
        "rollup": state["rollup"],
    }

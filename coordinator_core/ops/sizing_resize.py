"""
coordinator_core.ops.sizing_resize — JSON-RPC "sizing.resize".

Purpose: the single addressable writer for a sizing-object's `estimate.tshirt` and the
`route` the engine resolves from it (docs/plans/2026-10-01-fire-and-forget-xs-s.md § C2).
A sizing whose size moved after assemble is re-routed here rather than by a hand edit.

What it writes: `estimate.tshirt` (`provisional` kept true), `route`
(`sizing_assemble.route(estimate={"tshirt": tshirt})["route"]`), and `basis` under
`em_analysis.tshirt_resize_basis`.

Negative-spec:
  - Does NOT take `route` as a param — the route is always engine-resolved.
  - Does NOT add a schema field; `basis` lands in the free-form `em_analysis` map.
  - Does NOT touch a sizing in `shipped`, `declined` or `superseded`.
  - Does NOT git-commit or cascade; one-file `locked_rmw` mutation.

Spec backlink: docs/plans/2026-10-01-fire-and-forget-xs-s.md § C2
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    replace_fm_field,
    write_fm_nested_field,
)
from coordinator_core.frontmatter.schema_validate import (
    format_validation_errors,
    validate_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.session import record_homes

_SIZING_SCHEMA_PATH: Path = (
    Path(__file__).parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
)
_TSHIRTS = ("XS", "S", "M", "L", "XL", "XXL")
_TERMINAL = ("shipped", "declined", "superseded")
_BASIS_KEY = "tshirt_resize_basis"


def _err(msg: str) -> dict:
    return {"exit_code": 1, "applied": False, "error": msg}


def _dump_block(mapping: dict) -> str:
    dumped = yaml.safe_dump(
        mapping, default_flow_style=False, sort_keys=False, allow_unicode=True, width=100000
    )
    return "".join(f"  {line}\n" for line in dumped.rstrip("\n").split("\n"))


@register_op("sizing.resize")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "sizing.resize" handler (plain `def`: all steps block).

    Params:
        sizing (str) — path to the sizing-object under `state/sizings/`. Required.
        tshirt (str) — one of XS..XXL. Required.
        basis  (str) — why the size moved. Required, non-empty.

    Returns: {exit_code, applied, route, basis, message|error}.

    Exit-code contract:
        exit_code 1 — missing/invalid param; path escaping state/sizings/ or absent;
                      terminal status; post-mutation schema failure; lock timeout.
        exit_code 0, applied True  — estimate.tshirt and route written.
        exit_code 0, applied False — same size and route already recorded; no-op.
    """
    sizing_raw: str = (params.get("sizing") or "").strip()
    tshirt: str = (params.get("tshirt") or "").strip()
    basis: str = (params.get("basis") or "").strip()

    if not sizing_raw:
        return _err("missing required param: sizing")
    if tshirt not in _TSHIRTS:
        return _err(f"tshirt must be one of {list(_TSHIRTS)!r}, got {tshirt!r}")
    if not basis:
        return _err("missing required param: basis — why the size moved")
    if repo_root is None:
        return _err("sizing.resize: repo_root is required")

    from coordinator_core.sizing_assemble import route as _route

    new_route = _route(estimate={"tshirt": tshirt})["route"]

    worktree = main_worktree_root(repo_root)
    p = Path(sizing_raw)
    if not p.is_absolute():
        p = worktree / p
    p = contained_path(p, [Path(record_homes.home_dir(str(worktree), "sizings"))])
    if p is None:
        return _err(f"sizing escapes state/sizings/: {sizing_raw!r}")
    if not p.is_file():
        return _err(f"sizing-object not found on disk: {sizing_raw}")

    _state: dict = {"applied": False}

    def mutate(old_text: str) -> str:
        try:
            doc = yaml.safe_load(old_text) or {}
        except Exception as exc:  # noqa: BLE001
            raise MutateAbort(f"resize: YAML parse error: {exc}") from exc
        if not isinstance(doc, dict):
            raise MutateAbort("resize: sizing-object is not a YAML mapping")

        status = doc.get("status")
        if status in _TERMINAL:
            raise MutateAbort(f"refusing to resize {p}: status is terminal ({status})")

        est = doc.get("estimate")
        est = dict(est) if isinstance(est, dict) else {}
        if est.get("tshirt") == tshirt and doc.get("route") == new_route:
            return old_text

        est["tshirt"] = tshirt
        est["provisional"] = True
        new_text = write_fm_nested_field(old_text, "estimate", _dump_block(est))
        if "route" in doc:
            new_text = replace_fm_field(new_text, "route", new_route)
        else:
            new_text = insert_fm_field(new_text, "route", new_route)

        em = doc.get("em_analysis")
        em = dict(em) if isinstance(em, dict) else {}
        em[_BASIS_KEY] = basis
        new_text = write_fm_nested_field(new_text, "em_analysis", _dump_block(em))

        try:
            new_doc = yaml.safe_load(new_text) or {}
        except Exception as exc:  # noqa: BLE001
            raise MutateAbort(f"resize: post-mutation YAML parse error: {exc}") from exc
        errors = validate_frontmatter(new_doc, _SIZING_SCHEMA_PATH)
        if errors:
            raise MutateAbort(
                f"resize: post-mutation schema validation failed: {format_validation_errors(errors)}"
            )
        _state["applied"] = True
        return new_text

    try:
        locked_rmw(p, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"sizing-object disappeared before the lock could be acquired: {p}")
    except LockTimeout as exc:
        return _err(f"timed out waiting for file lock on {p}: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "resize: mutation aborted")

    msg = (
        f"resized {sizing_raw} to {tshirt}, route {new_route}"
        if _state["applied"]
        else f"{sizing_raw} already records {tshirt} and route {new_route} — idempotent no-op"
    )
    return {
        "exit_code": 0,
        "applied": _state["applied"],
        "route": new_route,
        "basis": basis,
        "message": msg,
    }

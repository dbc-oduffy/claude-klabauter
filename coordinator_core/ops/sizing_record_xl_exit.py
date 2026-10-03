"""
coordinator_core.ops.sizing_record_xl_exit — JSON-RPC "sizing.record_xl_exit".

Purpose: the single addressable writer for a sizing-object's `xl_exit` field and the
PM's verbatim words behind it. The engine never auto-selects an XL exit (a null
`xl_exit` on a `pm-decision` route is a legitimate open state); the PM's pick is
recorded here, with their own quote, instead of a hand-edited YAML field.

What it writes: `xl_exit = <pick>`, `pm_resolution.xl_exit = <pm_quote>` and
`pm_resolution.decided_on = <decided_on>` (the same `pm_resolution` shape
`sizing.discharge_surfaced` writes). Nothing else changes.

Re-record invariant (a recorded exit is the PM's decision; a later call must not
silently displace it):
  - no prior `xl_exit`           -> write.
  - same pick as recorded        -> idempotent no-op; the first quote is kept.
  - recorded `shape`, new pick is `roadmap` or `accept_multi_session`
                                 -> overwrite: shaping is a step toward an exit, so
                                    the PM moving on from it is the expected path.
  - any other change             -> refused; a settled exit is not re-decided here.

Negative-spec:
  - Does NOT compose or infer `pm_quote`; it is the caller's verbatim transcription.
  - Does NOT alter `route`, `status`, `fork`, or any other field, does NOT git-commit,
    spawns no process.

Spec backlink: sizing xl_exit recording (sibling of sizing.accept_exit_criterion).
"""

from __future__ import annotations

import re
from datetime import date
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

_SIZING_SCHEMA_PATH: Path = (
    Path(__file__).parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
)

XL_EXIT_PICKS = ("shape", "roadmap", "accept_multi_session")

# The only overwrite allowed: the PM moving on from `shape`.
_OVERWRITABLE_FROM = {"shape": frozenset({"roadmap", "accept_multi_session"})}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_PARAMS_HINT = (
    "params: sizing (required, path under state/sizings/), "
    f"xl_exit (required, one of {list(XL_EXIT_PICKS)}), pm_quote (required), decided_on"
)


def _err(msg: str) -> dict:
    return {"exit_code": 1, "applied": False, "error": msg, "message": msg}


def _render_pm_resolution(mapping: dict) -> str:
    dumped = yaml.safe_dump(
        mapping,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=100000,
    )
    return "".join(f"  {line}\n" for line in dumped.rstrip("\n").split("\n"))


@register_op("sizing.record_xl_exit")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "sizing.record_xl_exit" handler.

    Plain `def`, deliberately not `async def`: every step is blocking (path stats,
    `locked_rmw`), so an `async def` would make DISPATCH_TIMEOUT_SECS unenforceable.

    Params:
        sizing     (str) — path to the sizing-object under `state/sizings/`. Required.
        xl_exit    (str) — shape | roadmap | accept_multi_session. Required.
        pm_quote   (str) — the PM's verbatim words picking the exit. Required, non-empty.
        decided_on (str) — YYYY-MM-DD the PM decided. Defaults to today.

    Returns: {exit_code, applied, message|error}.

    Exit-code contract:
        exit_code 1 — a missing param; an unknown xl_exit; a malformed decided_on; a
                      sizing escaping state/sizings/ or absent; a re-record the
                      invariant refuses; schema validation failure; lock timeout.
        exit_code 0, applied True  — xl_exit and pm_resolution were written.
        exit_code 0, applied False — the same pick was already recorded; no-op.
    """
    sizing_raw: str = (params.get("sizing") or "").strip()
    pick: str = (params.get("xl_exit") or "").strip()
    pm_quote: str = (params.get("pm_quote") or "").strip()
    decided_on: str = (params.get("decided_on") or "").strip() or date.today().isoformat()

    if not sizing_raw:
        return _err(f"missing required param: sizing — {_PARAMS_HINT}")
    if pick not in XL_EXIT_PICKS:
        return _err(f"xl_exit must be one of {list(XL_EXIT_PICKS)!r}, got {pick!r}")
    if not pm_quote:
        return _err(
            "missing required param: pm_quote — the PM's verbatim pick; this op never "
            f"composes or infers one; {_PARAMS_HINT}"
        )
    if not _DATE_RE.match(decided_on):
        return _err(f"decided_on must be YYYY-MM-DD, got {decided_on!r}")
    if repo_root is None:
        return _err(
            "sizing.record_xl_exit: repo_root is required "
            "(no founding root available — handler called without socket-authoritative common_dir)"
        )

    worktree = main_worktree_root(repo_root)
    p = Path(sizing_raw)
    if not p.is_absolute():
        p = worktree / p
    p = contained_path(p, [worktree / "state" / "sizings"])
    if p is None:
        return _err(f"sizing escapes state/sizings/: {sizing_raw!r}")
    if not p.is_file():
        return _err(f"sizing-object not found on disk: {sizing_raw}")

    _state: dict = {"applied": False}

    def mutate(old_text: str) -> str:
        try:
            doc = yaml.safe_load(old_text) or {}
        except Exception as exc:  # noqa: BLE001
            raise MutateAbort(f"record_xl_exit: YAML parse error: {exc}") from exc
        if not isinstance(doc, dict):
            raise MutateAbort("record_xl_exit: sizing-object is not a YAML mapping")

        prior = doc.get("xl_exit")
        if prior == pick:
            return old_text
        if prior is not None and pick not in _OVERWRITABLE_FROM.get(prior, frozenset()):
            raise MutateAbort(
                f"refusing to record on {p}: xl_exit is already {prior!r} — only "
                f"'shape' may be overwritten, and only by roadmap or accept_multi_session"
            )

        text = (
            replace_fm_field(old_text, "xl_exit", pick)
            if "xl_exit" in doc
            else insert_fm_field(old_text, "xl_exit", pick)
        )
        prior_res = doc.get("pm_resolution")
        mapping: dict = dict(prior_res) if isinstance(prior_res, dict) else {}
        mapping.pop("decided_on", None)
        mapping["xl_exit"] = pm_quote
        text = write_fm_nested_field(
            text, "pm_resolution", _render_pm_resolution({"decided_on": decided_on, **mapping})
        )

        try:
            new_doc = yaml.safe_load(text) or {}
        except Exception as exc:  # noqa: BLE001
            raise MutateAbort(f"record_xl_exit: post-mutation YAML parse error: {exc}") from exc
        errors = validate_frontmatter(new_doc, _SIZING_SCHEMA_PATH)
        if errors:
            raise MutateAbort(
                "record_xl_exit: post-mutation schema validation failed: "
                f"{format_validation_errors(errors)}"
            )
        _state["applied"] = True
        return text

    try:
        locked_rmw(p, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"sizing-object disappeared before the lock could be acquired: {p}")
    except LockTimeout as exc:
        return _err(f"timed out waiting for file lock on {p}: {exc}")
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "record_xl_exit: mutation aborted")
    except Exception as exc:  # noqa: BLE001
        return _err(f"record_xl_exit: {type(exc).__name__}: {exc}")

    if _state["applied"]:
        return {
            "exit_code": 0,
            "applied": True,
            "message": f"recorded xl_exit {pick!r} with the PM's quote on {sizing_raw}",
        }
    return {
        "exit_code": 0,
        "applied": False,
        "message": f"{sizing_raw} already records xl_exit {pick!r} — idempotent no-op",
    }

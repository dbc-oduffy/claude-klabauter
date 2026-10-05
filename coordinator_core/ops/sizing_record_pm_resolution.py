"""
coordinator_core.ops.sizing_record_pm_resolution — JSON-RPC "sizing.record_pm_resolution".

Purpose: the single addressable writer for an arbitrary `pm_resolution.<key>` on a
sizing-object — the PM's verbatim ruling on a question the sizing surfaced (for
example `post_size_prompt` or `xl_route_assent`). `pm_resolution` is an open map, so
the key is the caller's; the value is always the PM's own words.

What it writes: `pm_resolution.<key> = <pm_quote>` and
`pm_resolution.decided_on = <decided_on>` (rendered first), every other existing key
preserved. Nothing else changes.

Re-record invariant (a recorded ruling is the PM's decision; a later call must not
silently displace it):
  - no value under `key`          -> write.
  - identical value under `key`   -> idempotent no-op; `decided_on` is left untouched.
  - different value, no supersede -> refused, quoting the existing value.
  - different value, supersede    -> overwrite.

Negative-spec:
  - Does NOT compose or infer `pm_quote`; it is the caller's verbatim transcription.
  - Does NOT write `xl_exit` (`sizing.record_xl_exit` owns it with its top-level field)
    or `fork` (the top-level `fork` field is authoritative), and refuses `decided_on`
    as a key.
  - Does NOT alter `route`, `status`, or any other field, does NOT git-commit,
    cascade, or spawn a process.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Optional

import yaml

from coordinator_core.session import record_homes
from coordinator_core.frontmatter.primitives import write_fm_nested_field
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

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Keys with an authoritative top-level field or a reserved meaning.
_OWNED_KEYS = {
    "decided_on": "decided_on is pm_resolution's own reserved key",
    "xl_exit": (
        "xl_exit has an authoritative top-level field — use sizing.record_xl_exit "
        "(sizing-assemble --xl-exit)"
    ),
    "fork": "the top-level `fork` field is authoritative; this op does not write it",
}

_PARAMS_HINT = (
    "params: sizing (required, path under the sizings home), key (required, "
    "^[a-z][a-z0-9_]{0,63}$), pm_quote (required), decided_on, supersede"
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


@register_op("sizing.record_pm_resolution")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "sizing.record_pm_resolution" handler.

    Plain `def`, deliberately not `async def`: every step is blocking (path stats,
    `locked_rmw`), so an `async def` would make DISPATCH_TIMEOUT_SECS unenforceable.

    Params:
        sizing     (str)  — path to the sizing-object under the sizings home. Required.
        key        (str)  — the `pm_resolution` key, `^[a-z][a-z0-9_]{0,63}$`. Required.
        pm_quote   (str)  — the PM's verbatim words. Required, non-empty.
        decided_on (str)  — YYYY-MM-DD the PM decided. Defaults to today.
        supersede  (bool) — overwrite a different existing value under `key`.

    Returns: {exit_code, applied, message|error}.

    Exit-code contract:
        exit_code 1 — a missing param; a malformed key or decided_on; a reserved or
                      owned key; a sizing escaping the sizings home or absent; a
                      different value under `key` with `supersede` unset; schema
                      validation failure; lock timeout.
        exit_code 0, applied True  — the ruling was written.
        exit_code 0, applied False — an identical value was already recorded; no-op.
    """
    sizing_raw: str = (params.get("sizing") or "").strip()
    key: str = (params.get("key") or "").strip()
    pm_quote: str = (params.get("pm_quote") or "").strip()
    decided_on: str = (params.get("decided_on") or "").strip() or date.today().isoformat()
    supersede: bool = bool(params.get("supersede"))

    if not sizing_raw:
        return _err(f"missing required param: sizing — {_PARAMS_HINT}")
    if not key:
        return _err(f"missing required param: key — {_PARAMS_HINT}")
    if not _KEY_RE.match(key):
        return _err(f"key must match {_KEY_RE.pattern}, got {key!r}")
    if key in _OWNED_KEYS:
        return _err(f"refusing key {key!r}: {_OWNED_KEYS[key]}")
    if not pm_quote:
        return _err(
            "missing required param: pm_quote — the PM's verbatim words; this op never "
            f"composes or infers one; {_PARAMS_HINT}"
        )
    if not _DATE_RE.match(decided_on):
        return _err(f"decided_on must be YYYY-MM-DD, got {decided_on!r}")
    if repo_root is None:
        return _err(
            "sizing.record_pm_resolution: repo_root is required "
            "(no founding root available — handler called without socket-authoritative common_dir)"
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

    _state: dict = {"applied": False}

    def mutate(old_text: str) -> str:
        try:
            doc = yaml.safe_load(old_text) or {}
        except Exception as exc:  # noqa: BLE001
            raise MutateAbort(f"record_pm_resolution: YAML parse error: {exc}") from exc
        if not isinstance(doc, dict):
            raise MutateAbort("record_pm_resolution: sizing-object is not a YAML mapping")

        prior = doc.get("pm_resolution")
        mapping: dict = dict(prior) if isinstance(prior, dict) else {}
        existing = mapping.get(key)
        if existing == pm_quote:
            return old_text
        if existing is not None and not supersede:
            raise MutateAbort(
                f"refusing to record on {p}: pm_resolution already carries {key!r} "
                f"({str(existing)[:120]!r}) — pass supersede to replace it"
            )

        mapping.pop("decided_on", None)
        mapping[key] = pm_quote
        text = write_fm_nested_field(
            old_text, "pm_resolution", _render_pm_resolution({"decided_on": decided_on, **mapping})
        )

        try:
            new_doc = yaml.safe_load(text) or {}
        except Exception as exc:  # noqa: BLE001
            raise MutateAbort(
                f"record_pm_resolution: post-mutation YAML parse error: {exc}"
            ) from exc
        errors = validate_frontmatter(new_doc, _SIZING_SCHEMA_PATH)
        if errors:
            raise MutateAbort(
                "record_pm_resolution: post-mutation schema validation failed: "
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
        return _err(str(exc.args[0]) if exc.args else "record_pm_resolution: mutation aborted")
    except Exception as exc:  # noqa: BLE001
        return _err(f"record_pm_resolution: {type(exc).__name__}: {exc}")

    if _state["applied"]:
        return {
            "exit_code": 0,
            "applied": True,
            "message": f"recorded pm_resolution.{key} with the PM's quote on {sizing_raw}",
        }
    return {
        "exit_code": 0,
        "applied": False,
        "message": f"{sizing_raw} already records pm_resolution.{key} — idempotent no-op",
    }

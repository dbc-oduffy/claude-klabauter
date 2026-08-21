"""
coordinator_core.ops.read_sizing_object_fields — JSON-RPC "sizing.read_object_fields"
operation.

Purpose: a sizing-object (`state/sizings/*.yaml`) is BARE YAML WITH NO `---`
FENCE — the whole document IS the record (mirrors
`deliverable_cascade._read_sizing_meta`'s own docstring: "Unlike `_read_meta`
... which parses `---`-delimited frontmatter out of a `.md` file ... a
sizing-object IS the entire document with no fences"). `read_frontmatter_field`
(and `frontmatter.primitives.read_fm_field_unquoted`, the reader this
package's existing sizing appliers — `sizing_ship.py`, `sizing_decline.py` —
use for the single scalar field `status`) cannot serve the fields this op
reaches: `intent` and `appetite` are simple scalars a line-based `^field:`
reader COULD extract, but `estimate` (a nested object, `{tshirt, provisional}`)
and `scout_evidence` (an array of strings or `{kind, finding, note}` objects)
are structured YAML values a single-line scalar extractor cannot represent —
it would return only the raw suffix text of the `estimate:`/`scout_evidence:`
key line itself (typically empty, since both are block-nested under their
key), never the nested/array content beneath it.

This is therefore a WHOLE-DOCUMENT YAML PARSE (`yaml.safe_load`), same
recipe as `deliverable_cascade._read_sizing_meta` and
`sizing_ship._validate_sizing_fm`/`sizing_decline._validate_sizing_fm` — read
the full mapping, then project out exactly the four fields this op names.
Unlike those three existing private, per-module helpers (each reads
`deliverable_id`/`status` for its own narrow purpose and is not shared), this
op is the first shared reader for the intent/estimate/scout_evidence/appetite
quartet — the fields a sizing-lobby-facing consumer (a callout, a query
surface) needs to summarize a sizing-object without re-deriving its own
whole-document YAML parse.

Wire params:
    sizing_path (str, required) — absolute or repo-relative path to the
                                   sizing-object under `state/sizings/`
                                   (or `archive/sizings/**`, mirroring
                                   `_sizing_citation`'s own live-then-archive
                                   resolution scope for reads).

Reply fields (result object):
    sizing_path      (str)         — echoed, as given.
    intent            (str | None) — `intent`, verbatim (schema: required string).
    estimate          (dict | None) — `estimate` object verbatim
                                       (`{tshirt, provisional}` + any other
                                       schema-legal keys), or None if absent.
    scout_evidence     (list | None) — `scout_evidence` array verbatim
                                        (string or `{kind, finding, note}`
                                        items), or None if absent.
    appetite           (str | None) — `appetite`, or None if absent
                                       (schema: optional, "not yet asked, or
                                       not volunteered").

Exit-code contract, mirroring `read_frontmatter_field`'s own always-readable
posture where feasible but RAISING rather than swallowing a genuine parse
failure (matching `_read_sizing_meta`'s stricter contract, since this op's
whole purpose is reaching structured fields a caller cannot silently
misread as empty):
    - Missing/empty `sizing_path` -> ValueError.
    - `sizing_path` not found on disk (after `_sizing_citation`-style
      live-then-archive resolution) -> ValueError.
    - File does not parse to a YAML mapping -> ValueError.
    - A named field absent from the parsed mapping -> that field is None in
      the reply (NOT an error) — absence is a legitimate sizing-object state
      for `appetite`/`scout_evidence`/`estimate` (see schema docstrings) and
      is reported as such, not conflated with a read failure.

Negative-spec:
  - Does NOT validate the sizing-object against its schema — this is a pure
    reader; `_validate_sizing_fm` (deliverable_cascade.py, sizing_ship.py,
    sizing_decline.py) already owns schema validation for each mutating op's
    own write path, and this op never writes.
  - Does NOT read `deliverable_id`/`status`/any field outside the named
    quartet — those are the existing private helpers' job; this op does not
    duplicate or replace them.
  - Does NOT resolve an archive fallback by itself; see Wire params — this
    op resolves the literal path only. (No caller in this dispatch needs the
    archive leg; adding it unasked would be scope creep — see the module's
    "Consumption status" convention on other ops in this package.)

Spec backlink: state/dispatch-briefs/2026-08-21-engine-half-of-the-roadmap-
sprint-spine-split/C6.md (hard-requirement partial 1, "fenceless sizing
read").
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root

#: The four fields this op reaches, in reply order.
_SIZING_OBJECT_FIELDS = ("intent", "estimate", "scout_evidence", "appetite")


def _read_sizing_object_fields(sizing_path: str) -> dict:
    """Whole-document YAML parse of `sizing_path`, projected to the
    intent/estimate/scout_evidence/appetite quartet.

    Raises ValueError if the file cannot be read or does not parse to a
    YAML mapping. A field absent from the mapping is returned as None —
    absence is legitimate for `appetite`/`scout_evidence`/`estimate` per the
    sizing-object schema's own field descriptions.
    """
    path = Path(sizing_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"sizing-object not found or unreadable: {sizing_path}: {exc}")

    try:
        parsed = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"sizing-object at {sizing_path} failed to parse as YAML: {exc}")

    if not isinstance(parsed, dict):
        raise ValueError(f"sizing-object at {sizing_path} did not parse to a YAML mapping")

    return {field: parsed.get(field) for field in _SIZING_OBJECT_FIELDS}


@register_op("sizing.read_object_fields")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "sizing.read_object_fields" handler.

    Params:
        sizing_path (str) — absolute or repo-relative path to the
                             sizing-object under `state/sizings/`. Required.

    Returns: {sizing_path, intent, estimate, scout_evidence, appetite} (see
    module docstring "Reply fields").

    Raises ValueError on a missing param, an escaped/absent path, or a
    non-mapping parse (see module docstring "Exit-code contract").
    """
    sizing_path_raw: str = (params.get("sizing_path") or "").strip()
    if not sizing_path_raw:
        raise ValueError("sizing.read_object_fields requires param: sizing_path")
    if repo_root is None:
        raise ValueError(
            "sizing.read_object_fields: repo_root is required "
            "(no founding root available — handler called without socket-authoritative common_dir)"
        )

    worktree = main_worktree_root(repo_root)

    p = Path(sizing_path_raw)
    if not p.is_absolute():
        p = worktree / p
    allowed_roots = [worktree / "state" / "sizings", worktree / "archive" / "sizings"]
    p = contained_path(p, allowed_roots)
    if p is None:
        raise ValueError(f"sizing_path escapes state/sizings/ or archive/sizings/: {sizing_path_raw!r}")
    if not p.is_file():
        raise ValueError(f"sizing-object not found on disk: {sizing_path_raw}")

    fields = _read_sizing_object_fields(str(p))
    return {"sizing_path": sizing_path_raw, **fields}

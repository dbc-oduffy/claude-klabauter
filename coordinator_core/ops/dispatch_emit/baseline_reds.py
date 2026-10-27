"""coordinator_core.ops.dispatch_emit.baseline_reds — reader for a sandbox's `.baseline-reds.json`.

Purpose: the manifest `{test_ids, errors}` lists reds that were already red before a run's change.
Emit composes it into the generated Workflow script as a JS literal plus an `_isBaselineRed`
helper; membership lives in that JS helper only. Compose-time file read only: no spawn, no git.
A missing, malformed or wrongly-shaped manifest reads as empty (excludes nothing), never raises.
"""

from __future__ import annotations

import json
from pathlib import Path

MANIFEST_NAME = ".baseline-reds.json"

_JS_HELPER = (
    "const _baselineNorm = (s) => String(s ?? '').replace(/\\\\/g, '/');\n"
    "const _baselineIdSet = new Set((_baselineReds.test_ids || []).map(_baselineNorm));\n"
    "const _baselineErrSet = new Set(_baselineReds.errors || []);\n"
    "const _isBaselineRed = (id, err) =>\n"
    "  (id != null && id !== '' && _baselineIdSet.has(_baselineNorm(id)))\n"
    "  || (err != null && err !== '' && _baselineErrSet.has(String(err)));\n"
)


def _str_list(value: object) -> list[str]:
    """A list of strings, or empty when `value` is not a list; non-string items are dropped."""
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


def load_manifest(root: Path) -> dict:
    """`{test_ids: [str], errors: [str]}` from `<root>/.baseline-reds.json`; empty on any fault."""
    empty: dict = {"test_ids": [], "errors": []}
    try:
        data = json.loads((Path(root) / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    return {"test_ids": _str_list(data.get("test_ids")), "errors": _str_list(data.get("errors"))}


def manifest_js(root: Path) -> str:
    """JS source declaring `_baselineReds` and `_isBaselineRed(id, err)` for the emitted script.

    Ids are normalised to posix '/' before set membership; an `err` matches the `errors` list.
    """
    literal = json.dumps(load_manifest(root), sort_keys=True)
    return f"const _baselineReds = {literal};\n{_JS_HELPER}"

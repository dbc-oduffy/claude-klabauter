"""Plan scope projection and repair-class delta classifier.

``scope_projection(text)`` records what in a plan can change what gets built or judged, with the
repair-class parts kept apart; ``classify(prior, current_text)`` says whether only repair-class
content changed since the projection was taken. Pure Python over plan text, no subprocess.
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional

import yaml

from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
from coordinator_core.ops.dispatch_emit.spine_read import (
    load_frontmatter_doc,
    load_rows_memo,
    split_frontmatter_memo,
)

VERSION = 1

#: Spine-row keys whose edits are repair-class. ``reads`` is the pre-consumes spelling of ``consumes``.
_ROW_REPAIR_KEYS = frozenset({"reads_at_head", "consumes", "reads", "register_rows"})
_FM_REPAIR_KEYS = frozenset({"census", "anchor_premises", "register_claims"})
#: Frontmatter that moves with the plan's lifecycle, not with what it builds.
_FM_VOLATILE_KEYS = frozenset({"status", "grouping_approvals", "updated", "last_updated", "modified", "branch"})
_PATH_KEYS = frozenset({"writes", "writes_under", "surface", "reads", "reads_at_head", "consumes"})


def _plain(value):
    return json.loads(json.dumps(value, default=str, sort_keys=True))


def _slashes(value):
    if isinstance(value, str):
        return value.replace("\\", "/")
    if isinstance(value, list):
        return [_slashes(v) for v in value]
    return value


def _row_parts(row: dict) -> tuple[dict, dict]:
    scope, repair = {}, {}
    for key, value in row.items():
        if key in _PATH_KEYS:
            value = _slashes(value)
        (repair if key in _ROW_REPAIR_KEYS else scope)[str(key)] = value
    return scope, repair


def _falsifier_parts(falsifier) -> tuple[Optional[dict], dict]:
    if falsifier is None:
        return None, {}
    if not isinstance(falsifier, dict):
        return {"raw": falsifier}, {}
    scope, repair = {}, {}
    for key, value in falsifier.items():
        (repair if key == "how" or str(key).startswith("baseline_") else scope)[str(key)] = value
    return scope, repair


def scope_projection(text: str) -> Optional[dict]:
    """JSON-able ``{v, frontmatter, prime, falsifier, body_sha256, rows, repair}`` of plan ``text``;
    None when its frontmatter or task spine cannot be parsed. Never raises."""
    try:
        return _project(text.replace("\r\n", "\n"))
    except Exception:
        return None


def _project(text: str) -> Optional[dict]:
    split = split_frontmatter_memo(text)
    fm: dict = {}
    body = text
    if split is not None:
        try:
            doc = load_frontmatter_doc(split.fm_text)
        except yaml.YAMLError:
            return None
        if doc is not None and not isinstance(doc, dict):
            return None
        fm = doc or {}
        body = split.body_with_leading_newline
    located = locate_fenced_block(body)
    rows = load_rows_memo(body)
    if located.status is not LocateStatus.LOCATED or rows.status is not LocateStatus.LOCATED:
        return None
    start, end = located.span
    prose = body[:start] + body[end:]

    pec = fm.get("prime_exit_criterion")
    prime: object = None
    falsifier_scope, falsifier_repair = None, {}
    if isinstance(pec, dict):
        prime = {str(k): v for k, v in pec.items() if k != "falsifier"}
        falsifier_scope, falsifier_repair = _falsifier_parts(pec.get("falsifier"))
    elif pec is not None:
        prime = {"raw": pec}

    out_rows: dict = {}
    repair_rows: dict = {}
    for i, row in enumerate(rows.rows):
        key = str(row.get("id")) if row.get("id") is not None else f"#{i}"
        out_rows[key], repair_rows[key] = _row_parts(row)

    return _plain(
        {
            "v": VERSION,
            "frontmatter": {
                str(k): v
                for k, v in fm.items()
                if k not in _FM_REPAIR_KEYS | _FM_VOLATILE_KEYS and k != "prime_exit_criterion"
            },
            "prime": prime,
            "falsifier": falsifier_scope,
            "body_sha256": hashlib.sha256(prose.strip().encode("utf-8")).hexdigest(),
            "rows": out_rows,
            "repair": {
                "frontmatter": {str(k): fm[k] for k in _FM_REPAIR_KEYS if k in fm},
                "falsifier": falsifier_repair,
                "rows": repair_rows,
            },
        }
    )


def _is_test_path(path: object) -> bool:
    if not isinstance(path, str) or not path:
        return False
    parts = path.replace("\\", "/").split("/")
    base = parts[-1]
    return (base.startswith("test_") and base.endswith(".py")) or "tests" in parts[:-1]


def _test_only_row(row: dict) -> bool:
    writes = row.get("writes")
    return (
        isinstance(writes, list)
        and bool(writes)
        and all(_is_test_path(p) for p in writes)
        and not row.get("writes_under")
    )


def classify(prior: object, current_text: str) -> str:
    """``"unchanged"`` when ``current_text`` projects equal to ``prior``; ``"repair"`` when only
    repair-class content differs; else ``"scope"``. An unparsable plan, or a ``prior`` that is not a
    current-version projection, is ``"scope"``."""
    cur = scope_projection(current_text)
    if cur is None or not isinstance(prior, dict) or prior.get("v") != VERSION:
        return "scope"
    if prior == cur:
        return "unchanged"
    try:
        return "repair" if _only_repair(prior, cur) else "scope"
    except (AttributeError, KeyError, TypeError):
        return "scope"


def _only_repair(prior: dict, cur: dict) -> bool:
    for key in ("frontmatter", "prime", "body_sha256"):
        if prior[key] != cur[key]:
            return False
    if prior["falsifier"] is not None and prior["falsifier"] != cur["falsifier"]:
        return False
    for row_id in prior["rows"]:
        if row_id not in cur["rows"] or prior["rows"][row_id] != cur["rows"][row_id]:
            return False
    return all(_test_only_row(cur["rows"][r]) for r in cur["rows"] if r not in prior["rows"])

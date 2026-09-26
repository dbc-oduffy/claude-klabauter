"""coordinator_core.write_guards.block_sizing_object_schema_violation —
hard-deny guard: refuse a ``state/sizings/*.yaml`` write whose content fails
``sizing-object.schema.json``, catching at write time what previously only
surfaced later as a plan-scaffold failure one artifact and one session away.

Scope: matched EXACTLY as tight as ``sizing-object.schema.json``'s own
``applies_to`` (``state/sizings/*.yaml``) — never widened to any other doc
type. Blocks on ANY schema violation, not only ``pm_resolution`` — the
plan-scaffold path this guard pre-empts validates the whole document, not a
single field.

Negative-spec:
  - Does NOT touch ``validate_frontmatter_schema_advisory.py`` or
    ``validate_frontmatter_schema_deny.py`` — those stay the general-purpose
    warn-by-default / ``COORDINATOR_SCHEMA_STRICT``-gated pair for every
    OTHER schema; this guard is a narrower, unconditional sibling for
    exactly one.
  - Does NOT require a sibling DoE-claude checkout, a registry manifest, or
    any repo-root git spawn — the schema it validates against is claude-klabauter's
    own vendored copy (``coordinator_core/frontmatter/schemas/``), and the
    path match is a path-tail regex, never a resolve-and-compare against a
    git root.
  - Fail-open on anything that is not itself a schema violation: an
    unparseable prospective YAML document is the generic schema-validation
    guard's territory; this guard only fires once the document PARSES and
    the parsed object fails schema validation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.frontmatter.schema_validate import parse_yaml, validate_frontmatter_obj
from coordinator_core.write_guards._repo_root import resolve_repo_root as _shared_resolve_repo_root
from coordinator_core.write_guards.validate_frontmatter_schema_advisory import (
    apply_edit,
    to_repo_relative,
    write_or_multiedit_content,
)

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit"]
# Runs ahead of the generic schema-validation pair (advisory PRIORITY 100,
# deny PRIORITY 5) so this narrower, sizing-specific block wins the "first
PRIORITY = 4

_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_SIZING_SCHEMA_BLOCK"

_SIZING_PATH_RE = re.compile(r"(^|/)state/sizings/[^/]+\.ya?ml$", re.IGNORECASE)

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "frontmatter" / "schemas" / "sizing-object.schema.json"
)


def _load_schema() -> Optional[dict]:
    try:
        return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — fail-open, never block on infra
        return None


def _compute_prospective_content(
    tool_name: str, tool_input: Dict[str, Any], abs_file_path: str
) -> Optional[str]:
    if tool_name == "Edit":
        try:
            with open(abs_file_path, "r", encoding="utf-8") as fh:
                existing = fh.read()
        except OSError:
            return None
        result, matched = apply_edit(
            existing, tool_input.get("old_string") or "", tool_input.get("new_string") or ""
        )
        return result if matched else None
    return write_or_multiedit_content(tool_name, tool_input, abs_file_path)


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name not in MATCHERS:
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        abs_file_path = tool_input.get("file_path") or ""
        if not abs_file_path:
            return None

        cwd = payload.get("cwd") or "."
        try:
            repo_root = _shared_resolve_repo_root(cwd) or cwd
        except Exception:  # noqa: BLE001 — fail-open, never block on infra
            repo_root = cwd

        repo_rel = to_repo_relative(
            str(abs_file_path).replace("\\", "/"), str(repo_root).replace("\\", "/")
        )
        if not repo_rel or not _SIZING_PATH_RE.search(repo_rel):
            return None

        import os as _os

        if _os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
            return None

        prospective_content = _compute_prospective_content(tool_name, tool_input, abs_file_path)
        if prospective_content is None:
            return None

        schema = _load_schema()
        if not isinstance(schema, dict):
            return None

        try:
            parsed = parse_yaml(prospective_content)
        except Exception:  # noqa: BLE001 — a parse failure is the generic
            return None
        if not isinstance(parsed, dict):
            return None

        try:
            result = validate_frontmatter_obj(parsed, schema)
        except Exception:  # noqa: BLE001 — fail-open, never block on infra
            return None

        if result.get("ok"):
            return None
        errors = list(result.get("errors") or [])
        if not errors:
            return None

        parts = []
        for err in errors:
            field = err.get("field") or "(unknown)"
            hint = f" (expected: {err['hint']})" if err.get("hint") else ""
            parts.append(f"{field}: {err.get('error')}{hint}")
        message = "; ".join(parts)

        note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload)
        reason = (
            "[sizing-object schema] refused: "
            f"{message}. Expected `pm_resolution` shape: "
            "pm_resolution: {decided_on: <YYYY-MM-DD>, <question>: <answer>}."
            + ("\n\n" + note if note else "")
        )

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    except Exception:  # noqa: BLE001 — fail-open on any unexpected error,
        return None

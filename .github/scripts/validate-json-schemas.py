#!/usr/bin/env python3
"""Every published .json file must parse, and JSON Schema documents must be valid schemas.

The engine vendors a JSON Schema bundle and validates against it in-process.
A schema that parses as JSON but is not a legal schema fails at runtime inside
a consumer's session rather than here, which is the wrong place to find out.

Two tiers:
  1. Parse — every .json file must be valid JSON. Unconditional.
  2. Schema — any document carrying a ``$schema`` key, or living under a
     directory named ``schema``/``schemas``, is checked with ``jsonschema``'s
     meta-validator when that library is importable.

``jsonschema`` is a declared runtime dependency of this package, so tier 2
normally runs. If it is absent (a checks-only CI job that skips the install),
tier 2 reports itself SKIPPED rather than silently passing — a check that
cannot distinguish "verified" from "not run" is not a check.

EXIT CONTRACT
  0 — all .json files parse and every inspected schema is legal (or there are none)
  1 — a parse failure or an invalid schema
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.dont_write_bytecode = True  # never litter the published tree with a __pycache__
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _repo import read_text, repo_files, repo_root  # noqa: E402

SCHEMA_DIR_NAMES = {"schema", "schemas"}


def looks_like_schema(rel: str, data: object) -> bool:
    if isinstance(data, dict) and "$schema" in data:
        return True
    return any(part in SCHEMA_DIR_NAMES for part in rel.split("/")[:-1])


def main() -> int:
    root = repo_root()
    json_files = [p for p in repo_files(root) if p.endswith(".json")]

    if not json_files:
        print("JSON validation: no .json files in tree (mid-bootstrap) — nothing to validate.")
        return 0

    try:
        import jsonschema
        from jsonschema.validators import validator_for
        schema_checking = True
    except ImportError:
        jsonschema = None
        schema_checking = False

    errors: list[str] = []
    parsed = 0
    checked_schemas = 0
    skipped_schemas = 0

    for rel in json_files:
        text = read_text(root / rel)
        if text is None:
            errors.append(f"{rel}: unreadable or binary content in a .json file")
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            errors.append(f"{rel}:{exc.lineno}: invalid JSON — {exc.msg}")
            continue
        parsed += 1

        if not looks_like_schema(rel, data):
            continue
        if not schema_checking:
            skipped_schemas += 1
            continue
        try:
            validator_for(data).check_schema(data)
            checked_schemas += 1
        except jsonschema.exceptions.SchemaError as exc:
            location = "/".join(str(p) for p in exc.absolute_path) or "<root>"
            errors.append(f"{rel}: invalid JSON Schema at {location} — {exc.message}")

    if errors:
        print("JSON validation FAILED:")
        for err in errors:
            print(f"  {err}")
        return 1

    print(f"JSON validation passed ({parsed} files parsed, {checked_schemas} schemas meta-validated).")
    if skipped_schemas:
        print(f"  SKIPPED schema meta-validation for {skipped_schemas} file(s): "
              "'jsonschema' is not importable in this environment.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

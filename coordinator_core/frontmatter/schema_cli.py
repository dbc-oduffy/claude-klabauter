"""
coordinator_core.frontmatter.schema_cli — byte-identical parity CLI over
schema_validate's describe()/validate(), PLUS dual registration of the same
logic as the "schema.describe"/"schema.validate" JSON-RPC ops.

Purpose: Python port of DoE-claude coordinator/bin/schema-cli.js — a
subprocess-friendly JSON bridge callers can shell out to
(``python3 -m coordinator_core.frontmatter.schema_cli --describe <name>``),
with an OWN argv/output contract that intentionally does NOT route through
coordinator_core.invoke's JSON-RPC envelope (coordinator_core/invoke/__main__.py,
dispatch via register_op at coordinator_core/ipc.py:828). Per DR-215 §2/§6 the
enveloped surface is "ops plus a thin generic command entrypoint"; this module
is the second, non-enveloped argv contract living ALONGSIDE it — the JSON-RPC
envelope shape (request id, jsonrpc version, params/result wrapping) is
fundamentally incompatible with the byte-identical-parity requirement schema-cli.js
sets (two-space JSON + trailing newline, three-way exit codes, no envelope).

Invariant: ONE implementation (schema_validate.describe()/schema_validate.validate()),
TWO front doors — this parity CLI (argv contract below) AND the registered
"schema.describe"/"schema.validate" ops (register_op side-effect below, JSON-RPC
envelope contract). Never two implementations to keep in sync — both front
doors call the exact same schema_validate functions.

Argv contract (byte-identical parity with schema-cli.js):
    --describe <schema-name>
        Prints two-space pretty-printed JSON + trailing newline:
          {"required": [...], "optional": [...], "enums": {...}, "applies_to": str|None}
        required/optional are ORDERED field-name arrays (schema declaration order).
        applies_to is ALWAYS present in the output (value null when the schema
        declares none) — mirrors schema.js's ``schema.applies_to ?? null``.
        Exit 0 on success.

    --validate <schema-name>
        Reads a record as JSON from stdin, validates via schema_validate.validate(),
        prints two-space pretty-printed JSON + trailing newline:
          {"ok": bool, "errors": [str, ...]}
        Each error is the schema_validate ErrorDict flattened to schema-cli.js's
        "field: error" string form (schema-cli.js:220-224) — this module does the
        flattening; schema_validate.validate() itself returns the richer
        {field, error, hint} dict shape for structured callers.
        Exit 0 if ok, exit 1 if not ok, exit 2 on malformed stdin JSON.

Exit codes (THREE-way, schema-cli.js:28/227):
    0 — --describe success, OR --validate with ok:true.
    1 — --validate with ok:false, OR any failLoud() usage/lookup error
        (missing mode, unknown schema, missing schema name).
    2 — --validate with malformed JSON on stdin. Distinct from 1 — a naive
        two-way ok/not-ok implementation collapsing this into 1 is the single
        easiest parity break to ship by accident (schema-cli.js:211-213).

stderr error prefix is exactly ``schema-cli: error: `` (schema-cli.js:59),
byte-identical including the trailing space before the message.

Negative-spec:
    - Does NOT support COORDINATOR_SCHEMAS_DIR override — schema_validate.describe()/
      validate() always read claude-klabauter's own vendored schema set
      (coordinator_core/frontmatter/schemas/); claude-klabauter vendors a single fixed schema
      set with no consumer-test fixture redirection (see schema_validate.py's
      describe() docstring). This is a deliberate, narrower scope than
      schema-cli.js's env-override — claude-klabauter has no consumer-test schema-dir
      isolation need today.
    - Does NOT reimplement schema shape validation or cross-field rules — both
      op handlers and main() call schema_validate.describe()/validate() only.

Spec backlink: docs/plans/2026-07-14-dual-yaml-parser-option-d.md § C7 [DEAD-CITATION: plan file never committed to this repo]
  (chunk C7 — parity CLI + dual op-registration)
DoE oracle: DoE-claude coordinator/bin/schema-cli.js (229 lines)
DoE conformance fixture: DoE-claude coordinator/bin/tests/test-schema-cli.bats
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, NoReturn, Optional

from coordinator_core.frontmatter.schema_validate import describe, validate
from coordinator_core.ipc import register_op


_ERROR_PREFIX = "schema-cli: error: "


def _fail_loud(msg: str) -> NoReturn:
    """Write an error to stderr with the parity-pinned prefix and exit 1.

    Port of schema-cli.js failLoud() (lines 54-61). schema-cli.js's failLoud
    always exits 1 — the malformed-stdin-JSON case (exit 2) is a SEPARATE path
    in --validate mode (schema-cli.js:211-213) that does NOT go through failLoud;
    it writes its own stderr line and exits 2 directly (mirrored in _cmd_validate
    below, NOT routed through this function).
    """
    sys.stderr.write(f"{_ERROR_PREFIX}{msg}\n")
    sys.exit(1)


def _flatten_errors(errors: list[dict]) -> list[str]:
    flattened: list[str] = []
    for e in errors:
        field = e.get("field") if isinstance(e, dict) else None
        error = e.get("error") if isinstance(e, dict) else None
        field_part = f"{field}: " if field else ""
        flattened.append(f"{field_part}{error or ''}")
    return flattened


def _print_json(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, indent=2) + "\n")


def _cmd_describe(schema_name: str) -> dict:
    return describe(schema_name)


def _cmd_validate(schema_name: str, record: dict) -> dict:
    result = validate(schema_name, record)
    ok = result.get("ok") is True
    errors = [] if ok else _flatten_errors(result.get("errors") or [])
    return {"ok": ok, "errors": errors}


def main(argv: Optional[list[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    mode = args[0] if len(args) >= 1 else None
    schema_name = args[1] if len(args) >= 2 else None

    if not mode:
        _fail_loud(
            "missing mode. Usage: schema-cli.js --describe <schema-name> | "
            "--validate <schema-name>"
        )

    if mode not in ("--describe", "--validate"):
        _fail_loud(f'unknown mode "{mode}". Use --describe or --validate')

    if not schema_name:
        _fail_loud(f"missing schema name. Usage: schema-cli.js {mode} <schema-name>")

    if mode == "--describe":
        try:
            result = _cmd_describe(schema_name)
        except ValueError as exc:
            _fail_loud(str(exc))
        _print_json(result)
        return 0

    stdin_text = sys.stdin.read()
    try:
        record = json.loads(stdin_text)
    except (json.JSONDecodeError, ValueError) as exc:
        sys.stderr.write(f"{_ERROR_PREFIX}malformed JSON on stdin: {exc}\n")
        return 2

    try:
        result = _cmd_validate(schema_name, record)
    except ValueError as exc:
        _fail_loud(str(exc))

    _print_json(result)
    return 0 if result["ok"] else 1


# -> schema_validate.describe()/validate()), a DIFFERENT front door: the
# JSON-RPC envelope (register_op dispatch, coordinator_core/ipc.py:828) rather
# COMPUTE_ONLY (read-only: schema_validate.describe()/validate() only read the
# OP_CLASSIFICATION requires a "schema.describe": OpClass.COMPUTE_ONLY and
# "schema.validate": OpClass.COMPUTE_ONLY entry (DR-208 "new ops default to
# MUTATING until a reviewer affirms COMPUTE_ONLY") plus a
# coordinator_core/ipc.py::_OP_KEY_SCOPE) to wire the eager (non-lazy)

@register_op("schema.describe")
async def _op_schema_describe(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "schema.describe" handler — wraps schema_validate.describe().

    Params:
        schema_name (str): required. The vendored schema name to describe
            (e.g. "lesson-entry"). repo_root is unused — describe() always
            reads claude-klabauter's own fixed vendored schema set, never a per-repo path.

    Returns:
        {"required": [...], "optional": [...], "enums": {...}, "applies_to": str|None}

    Raises:
        ValueError: schema_name is missing, or is not a registered vendored
            schema (or is one of the internal "_byGlob"/"_byKind" index keys) —
            propagates from schema_validate.describe().
    """
    schema_name = params.get("schema_name")
    if not isinstance(schema_name, str) or not schema_name:
        raise ValueError("schema.describe: missing required param \"schema_name\"")
    return _cmd_describe(schema_name)


@register_op("schema.validate")
async def _op_schema_validate(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "schema.validate" handler — wraps schema_validate.validate().

    Params:
        schema_name (str): required. The vendored schema name to validate against.
        fields (dict): required. The record to validate — the JSON-RPC-native
            equivalent of the argv contract's stdin JSON (already a parsed dict;
            no malformed-JSON exit-2 case exists on this front door, since the
            JSON-RPC transport itself rejects a malformed request before this
            handler runs).

    Returns:
        {"ok": bool, "errors": [str, ...]}
        errors entries are flattened "field: error" strings, matching the argv
        contract's --validate output (parity with schema-cli.js:220-224).

    Raises:
        ValueError: schema_name is missing/not a string, fields is not a dict,
            or schema_name is not a registered vendored schema — propagates
            from schema_validate.validate() for the last case.
    """
    schema_name = params.get("schema_name")
    if not isinstance(schema_name, str) or not schema_name:
        raise ValueError("schema.validate: missing required param \"schema_name\"")
    fields = params.get("fields")
    if not isinstance(fields, dict):
        raise ValueError("schema.validate: missing or non-object required param \"fields\"")
    return _cmd_validate(schema_name, fields)


if __name__ == "__main__":
    sys.exit(main())

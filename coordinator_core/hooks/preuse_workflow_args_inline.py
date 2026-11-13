"""coordinator_core.hooks.preuse_workflow_args_inline: PreToolUse(Workflow) args-file expansion.

A `Workflow` call whose `args` is ``{"argsFile": <path>, "argsSha256": <hex>}`` is rewritten
(`updatedInput`) so `args` becomes the parsed file content, verbatim. The file is what
`distill.launch_args.build` wrote; the sha pins that file's content, not its origin.

Never denies: a missing file, a sha mismatch or unparseable content returns an advisory and leaves
the call unchanged. Every other call shape is silent. Reads one file; no git, no subprocess.

Not registered as an op here: wiring (op registration, classification, hook config) is the
external gate. `handle(params)` is the body a registration wraps.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping

from coordinator_core._hook_envelope import context_only, no_advisory, payload_of, rewrite_input

__all__ = ["handle", "ARGS_FILE_KEY", "ARGS_SHA_KEY"]

ARGS_FILE_KEY = "argsFile"
ARGS_SHA_KEY = "argsSha256"


def _stub_of(tool_input: Mapping) -> "Mapping | None":
    args = tool_input.get("args")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    if isinstance(args, Mapping) and args.get(ARGS_FILE_KEY) and args.get(ARGS_SHA_KEY):
        return args
    return None


def _advisory(reason: str) -> dict:
    return context_only("PreToolUse", f"Workflow args file not inlined: {reason}. Call left unchanged.")


def handle(params: dict) -> dict:
    """Rewrite a stub-args Workflow call to its inlined args; silent for any other call."""
    params = payload_of(params)
    if params.get("tool_name") != "Workflow":
        return no_advisory()
    tool_input = params.get("tool_input")
    if not isinstance(tool_input, Mapping):
        return no_advisory()
    stub = _stub_of(tool_input)
    if stub is None:
        return no_advisory()
    try:
        path = Path(str(stub[ARGS_FILE_KEY]))
        if not path.is_absolute():
            path = Path(params.get("cwd") or ".") / path
        body = path.read_bytes()
    except OSError as exc:
        return _advisory(f"unreadable ({type(exc).__name__})")
    if hashlib.sha256(body).hexdigest() != str(stub[ARGS_SHA_KEY]).lower():
        return _advisory("sha256 mismatch")
    try:
        content = json.loads(body.decode("utf-8"))
    except ValueError:
        return _advisory("not valid JSON")
    return rewrite_input(
        "PreToolUse",
        {**tool_input, "args": content},
        f"Workflow args inlined from {path.name}.",
    )

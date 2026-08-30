"""coordinator_core.merge_assemble.cli — the zero-dependency LEAF module
holding merge-assemble's argv parse and result print functions, split out of
`merge_assemble.main`/`merge_assemble.apply.main_apply` (each of which fused
parse -> in-process call -> print into one function).

Import closure, by construction: stdlib only (`json`, `sys`) PLUS the ONE
carve-out AC10 names, `coordinator_core.ceremony_common.json_payload_flag`
(itself stdlib-only — `json`, `typing` — so the carve-out cannot widen
silently; `test_cli_parse_and_print.py` machine-checks both closures). This
module imports NOTHING from `merge_assemble/__init__.py`, `apply.py`, or
`coordinator_core.contract.apply_base` — the warm entry point reaches this
module directly and never touches any of those three, which is the whole
point of pulling parse/print out here (AC1 forbids the warm path importing
`coordinator_core.merge_assemble`; AC2 needed the shared functions to live
somewhere the warm path CAN legally import).

`apply.py :: main_apply` imports FROM this module, so the cold path's
observable behaviour — argv handling, printed bytes, the usage-error
exit-2 path — is unchanged.

ADDITIVE TO BEHAVIOUR, not to output: nothing about what this CLI prints or
returns moved in this chunk.

Negative-spec:
    - Do NOT import `coordinator_core.merge_assemble` (the `__init__.py`
      package module), `coordinator_core.merge_assemble.apply`, or
      `coordinator_core.contract.apply_base` from this file, directly or
      transitively — that is the exact import this module exists to be
      absent, and the warm entry point's whole legality rests on it staying
      absent. `test_cli_parse_and_print.py` asserts this via `sys.modules`
      inspection of a fresh import, not by review.
    - Do NOT widen the `json_payload_flag` carve-out to any other
      `coordinator_core.ceremony_common` sibling — only
      `json_payload_flag` is named, and only because its own closure is
      itself stdlib-only.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from coordinator_core.ceremony_common.json_payload_flag import (
    detect_conflicting_payload_channels,
    resolve_json_payload_flag,
)


class UsageError(Exception):
    """Raised by a parse function on a usage error — the caller renders its
    own `_usage(prog)`-shaped diagnostic and exit code (both `main` and
    `main_apply` return `EXIT_USAGE`/`APPLY_EXIT_TRANSPORT_FAIL`
    respectively today; this module carries no opinion on which). `message`
    is `None` when the caller should print its own bare usage text with no
    extra diagnostic line (e.g. no argv at all)."""

    def __init__(self, message: Optional[str] = None):
        super().__init__(message or "")
        self.message = message


def parse_apply_argv(argv: list[str]) -> dict[str, Any]:
    """Parses `merge-assemble apply`'s argv into a params dict —
    `session_id`/`decisions`/`force`/`tag_prefix`, the same keys the op
    adapters read — ported as-is from `main_apply`'s existing loop,
    including its two multi-token helpers:

    - `detect_conflicting_payload_channels(argv)`, called before the token
      loop, rejecting `--decisions`/`--decisions-file` supplied together.
    - `resolve_json_payload_flag(argv, i)`, whose `.consumed`/`.error`
      protocol resolves either channel at the current token and advances
      `i` by `.consumed`.

    Raises `UsageError` (message `None` or a diagnostic string) on any
    usage error, matching `main_apply`'s existing `_usage(...)` return
    path exactly — including the payload-channel conflict and malformed-
    JSON cases, which `main_apply` also routes through `_usage`."""
    session_id: Optional[str] = None
    decisions: Optional[dict[str, Any]] = None
    force = False
    tag_prefix = "v"

    conflict = detect_conflicting_payload_channels(argv)
    if conflict is not None:
        raise UsageError(f"merge-assemble apply: {conflict}")

    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--session-id":
            if i + 1 >= len(argv):
                raise UsageError(None)
            session_id = argv[i + 1]
            i += 2
        elif tok == "--force":
            force = True
            i += 1
        elif tok == "--tag-prefix":
            if i + 1 >= len(argv):
                raise UsageError(None)
            tag_prefix = argv[i + 1]
            i += 2
        elif (payload := resolve_json_payload_flag(argv, i)).consumed:
            if payload.error is not None:
                raise UsageError(f"merge-assemble apply: {payload.error}")
            decisions = payload.value
            i += payload.consumed
        else:
            raise UsageError(f"merge-assemble apply: unrecognized argument {tok!r}")

    return {
        "session_id": session_id,
        "decisions": decisions,
        "force": force,
        "tag_prefix": tag_prefix,
    }


def print_apply_result(report: dict[str, Any]) -> None:
    """Prints `report` byte-identical to today's
    `json.dumps(report, indent=2, sort_keys=True)`."""
    print(json.dumps(report, indent=2, sort_keys=True))

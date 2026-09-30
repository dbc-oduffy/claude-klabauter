"""
coordinator_core.invoke.dispatch — JSON-RPC error helper and dispatch utilities.

Purpose: provides the jsonrpc_error helper and re-exports the ipc dispatch
primitives (dispatch_message, error-code constants) for use by the command-type
invoke entrypoint (coordinator_core.invoke.__main__).

The HTTP invoke surface (handle_http_invoke / serve_http_async) was retired by
DR-215 (command-type execution model). This module retains the non-HTTP dispatch
registration so that coordinator_core.invoke.__main__ can import from here.

Negative-spec (RAG-bait):
    There is no HTTP gate chain in this module. The seven-gate bearer-token /
    authz / single-writer-queue chain that guarded the HTTP surface has been
    removed. In-process command dispatch has no transport-layer authz
    requirement — callers are the local ceremony scripts.

DR backlink: docs/decisions/DR-215-coordinator-core-command-type-execution-model.md
"""

from __future__ import annotations

import logging

from coordinator_core.ipc import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    STRUCTURAL_PIN_ERROR,
    dispatch_message,
    resolve_request_repo,
    resolve_op_repo_key,
)

_log = logging.getLogger(__name__)

__all__ = [
    "dispatch_message",
    "jsonrpc_error",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "STRUCTURAL_PIN_ERROR",
    "resolve_op_repo_key",
    "resolve_request_repo",
]


# ---------------------------------------------------------------------------
# JSON-RPC error helper
# ---------------------------------------------------------------------------

def jsonrpc_error(code: int, message: str, id_=None) -> dict:
    """Return a JSON-RPC 2.0 error envelope.

    Uses the numeric constants from ipc (PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND,
    INVALID_PARAMS). No new error codes are introduced here.

    Args:
        code:    JSON-RPC error code (one of the ipc constants).
        message: Human-readable error description.
        id_:     The request id, or None if unknown (e.g. parse error before id extraction).

    Returns:
        {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}
    """
    return {
        "jsonrpc": "2.0",
        "id": id_,
        "error": {
            "code": code,
            "message": message,
        },
    }

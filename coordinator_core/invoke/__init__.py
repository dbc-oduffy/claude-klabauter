"""
coordinator_core.invoke — command-type dispatch package for coordinator_core.

Purpose: exposes the JSON-RPC dispatch utilities for the command-type execution
model (DR-215). The HTTP invoke surface (serve_http_async / http_server) was
retired; see docs/decisions/DR-215-coordinator-core-command-type-execution-model.md.

The command entrypoint (python -m coordinator_core.invoke) is added by C5.

DR backlink: docs/decisions/DR-215-coordinator-core-command-type-execution-model.md
"""

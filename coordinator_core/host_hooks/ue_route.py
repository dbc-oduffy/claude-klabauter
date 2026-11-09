"""Bounded, read-only MCP readiness hint for the configured UE retrieval route.

This never starts a server or treats a source checkout as an installed add-on.
A failed/slow probe gives an unverified launch notice, not a training-data fallback.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
import tomllib
import urllib.request
import urllib.error
import urllib.parse

REQUIRED_TOOLS = frozenset({"example_retrieval_repo_instructions", "engine_domain_status", "project_engine_member_signature", "check_cpp_structure_live"})


class _ProbeFailure(ValueError):
    """A controlled diagnostic code; never raw config, headers or server text."""


def _source_hint(source):
    # A server's source locator can itself contain credentials or query tokens.
    # Keep ordinary installed paths useful without reflecting credential URLs.
    if "://" in source or any(char in source for char in ("@", "?", "#")):
        return "(redacted source locator)"
    return source


def _object(response):
    if response.get("error"):
        raise _ProbeFailure("mcp_request_rejected")
    result = response.get("result", {})
    if result.get("isError"):
        raise _ProbeFailure("mcp_tool_failed")
    value = result.get("structuredContent")
    if isinstance(value, dict):
        return value
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                value = json.loads(block["text"])
            except (ValueError, KeyError):
                continue
            if isinstance(value, dict):
                return value
    return {}


def probe_example_retrieval_repo(*, config_path=None, budget=0.2, clock=time.monotonic, opener=urllib.request.urlopen):
    deadline = clock() + budget
    stage = "configuration"
    route = {"server": "project-rag", "configured": False, "transport": "http"}
    names = set()
    budget_exhausted = False

    def evidence():
        return {"stage": stage, "route": dict(route),
                "tools": sorted(REQUIRED_TOOLS & names), "missing_tools": sorted(REQUIRED_TOOLS - names),
                "evidence_scope": "readiness_hint_only", "ue_api_retrieval_verified": False, "lsp_verified": False}
    try:
        path = Path(config_path) if config_path else Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
        server = tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"]["project-rag"]
        route["configured"] = True
        if server.get("enabled", True) is not True:
            raise _ProbeFailure("route_disabled")
        if not isinstance(server.get("url"), str) or urllib.parse.urlsplit(server["url"]).scheme not in {"http", "https"}:
            raise _ProbeFailure("http_route_unavailable")
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        headers.update(server.get("http_headers", {}))
        for name, variable in server.get("env_http_headers", {}).items():
            if os.environ.get(variable):
                headers[name] = os.environ[variable]
        token = os.environ.get(server.get("bearer_token_env_var", ""))
        if token:
            headers["Authorization"] = "Bearer " + token

        def rpc(method, params, ident=None, label=None):
            nonlocal stage, budget_exhausted
            stage = label or method
            remaining = deadline - clock()
            if remaining <= 0:
                budget_exhausted = True
                raise TimeoutError("UE retrieval capability probe budget exhausted")
            message = {"jsonrpc": "2.0", "method": method, "params": params}
            if ident is not None:
                message["id"] = ident
            request = urllib.request.Request(server["url"], data=json.dumps(message).encode("utf-8"), headers=headers, method="POST")
            with opener(request, timeout=remaining) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    headers["Mcp-Session-Id"] = session
                body = response.read(1_000_000).decode("utf-8")
            if not body.strip():
                return {}
            try:
                decoded = json.loads(body)
            except ValueError:
                data = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
                decoded = json.loads(data[-1]) if data else {}
            if isinstance(decoded, dict) and decoded.get("error"):
                raise _ProbeFailure("mcp_request_rejected")
            return decoded

        rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "coordinator-session-context", "version": "1"}}, 1)
        rpc("notifications/initialized", {})
        listed = rpc("tools/list", {}, 2)
        names = {tool["name"] for tool in listed.get("result", {}).get("tools", [])
                 if isinstance(tool, dict) and isinstance(tool.get("name"), str)}
        if not REQUIRED_TOOLS <= names:
            raise _ProbeFailure("required_tools_missing")
        status = _object(rpc("tools/call", {"name": "engine_domain_status", "arguments": {}}, 3, label="engine_domain_status"))
        source = str(status.get("source_root_path", ""))
        if status.get("engine_queryable") is not True or "example-retrieval-repo-ue-addon" not in source:
            raise _ProbeFailure("addon_serving_unverified")
        return {"ready": True, "addon": "example-retrieval-repo-ue-addon", **evidence(),
                "engine_versions": status.get("engine_versions", []), "source_root": _source_hint(source)}
    except Exception as exc:
        if isinstance(exc, _ProbeFailure):
            reason = str(exc)  # Only codes raised above, never an external exception's message.
        elif isinstance(exc, TimeoutError):
            reason = "budget_exhausted" if budget_exhausted else "transport_timeout"
        elif isinstance(exc, FileNotFoundError):
            reason = "configuration_missing"
        elif isinstance(exc, KeyError) and stage == "configuration":
            reason = "route_not_configured"
        elif isinstance(exc, urllib.error.HTTPError):
            reason = "http_request_failed"
        elif isinstance(exc, urllib.error.URLError):
            reason = "transport_unavailable"
        elif isinstance(exc, (ValueError, TypeError, AttributeError)):
            reason = "configuration_invalid" if stage == "configuration" else "invalid_mcp_response"
        else:
            reason = "probe_failed"
        result = {"ready": False, "reason": reason, "error_type": type(exc).__name__, **evidence()}
        if isinstance(exc, urllib.error.HTTPError) and isinstance(exc.code, int):
            result["http_status"] = exc.code
        return result

"""
coordinator_core.ops.resolve_mcp_server_cli_path — "mcp.resolve_server_cli_path" op.

Purpose: native replacement for the inline `python3 -c "import json,os; ..."`
fence pair embedded in `skills/workstream-start/SKILL.md` (DoE-claude) that
parses `~/.claude.json`'s `mcpServers.<name>.args` list to recover an MCP
server's CLI entrypoint path and its project root, so callers stop
re-deriving that two-line parse inline at every call site (the oracle's own
comment: "same resolution as `commands/workday-start.md` Step 3.6 — do not
duplicate threshold logic"). This op is the one place that resolution lives.

Wire contract (per state/audits/2026-07-22-command-payload-inventory/
op-classification.tsv row "resolve-mcp-server-cli-path"):
    params:   {server_name: str (required) — the mcpServers key to resolve,
               claude_json_path: str (optional, test/override seam — the
                         path to the `.claude.json` file; production callers
                         omit it and get the home-relative default below)}
    response: {cli_path: str, project_root: str}
              on failure: {cli_path: None, project_root: None, error: str}

Resolution mirrors the oracle exactly: `cli_path` is the first entry in
`args` ending in `.py` or `cli`; `project_root` is `args[-1]`. Both come
from the SAME `args` list read once — the oracle's two separate `python3 -c`
invocations each re-read and re-parse the file independently; this op reads
and parses it exactly once per call.

Home resolution: `${CLAUDE_HOME:-$HOME}/.claude.json` — the same
CLAUDE_HOME-env-first-then-`expanduser` fallback already in use elsewhere in
this tree (see CLAUDE.md § Architecture "Path-resolution baseline"), not a
new pattern. `~/.claude.json` is a fixed per-user home artifact (Claude
Code's own MCP-registration file), not a coordinator settings-home concept,
so it is resolved independently of `coordinator_core._settings_home` rather
than nested under it.

Spec backlinks:
  - Plan (Wave 2): docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md § Mandated resolvers
  - Oracle: state/audits/2026-07-22-command-payload-inventory/distinct-ops-new.tsv row "resolve-mcp-server-cli-path"
  - Classification: state/audits/2026-07-22-command-payload-inventory/op-classification.tsv row "resolve-mcp-server-cli-path"
  - Fence example (read-only reference, not ported verbatim):
    coordinator/skills/workstream-start/SKILL.md § "Freshness nudge" (DoE-claude,
    two `python3 -c` lines resolving `_rag_cli` / `_rag_root`)

Idempotency (AC7): read-only parse of a per-user config file, no state
mutation — a repeat invocation with identical params re-reads the same file
and returns the same result by construction (manifest idempotency-hazard:
none). No DEC-7 note required (idempotency- and platform-hazard both rate
below medium on op-classification.tsv).

Negative-spec:
  - Does NOT use `subprocess`/`python3 -c` — pure Python stdlib `json`,
    replacing the oracle's inline-python shellout entirely (the oracle's own
    risk note: "no shell spawn").
  - Does NOT invoke any downstream MCP-server behavior after resolution —
    covers exactly the two resolution lines the oracle fence contains, per
    the classification row's rationale ("this op covers exactly those two
    resolution lines ... not any downstream invocation the fence may do
    after").
  - Does NOT require `repo_root` — this op reads a global per-user home
    file, never caller-repo state (manifest scope-verdict: none).
  - Does NOT hardcode `~/.claude.json` as a literal path — resolved via
    `CLAUDE_HOME` env override then `Path.home()`, both handled by
    `pathlib.Path`, so an MSYS/cygdrive-style `HOME` still resolves natively.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op

_MCP_SERVERS_KEY = "mcpServers"
_ARGS_KEY = "args"


def _default_claude_json_path() -> Path:
    """Resolve `${CLAUDE_HOME:-$HOME}/.claude.json` — the CLAUDE_HOME-env-first-
    then-home-default precedence already in use elsewhere in this tree."""
    base = os.environ.get("CLAUDE_HOME") or Path.home()
    return Path(base) / ".claude.json"


def _pick_cli_path(args: list) -> Optional[str]:
    """First arg ending in `.py` or `cli`, mirroring the oracle's
    `next(a for a in args if a.endswith('.py') or a.endswith('cli'))`."""
    for arg in args:
        if isinstance(arg, str) and (arg.endswith(".py") or arg.endswith("cli")):
            return arg
    return None


def resolve_cli_path_and_root(claude_json_path: Path, server_name: str) -> dict:
    """Parse `claude_json_path`'s `mcpServers.<server_name>.args` and return
    `{"cli_path": str, "project_root": str}`, or `{"cli_path": None,
    "project_root": None, "error": str}` on any resolution failure.

    Never raises — every failure mode (missing file, malformed JSON, missing
    server, empty/malformed args) is reported in the `error` key so callers
    get a uniform envelope regardless of failure shape.
    """
    if not claude_json_path.is_file():
        return {
            "cli_path": None,
            "project_root": None,
            "error": f"claude_json not found: {claude_json_path}",
        }

    try:
        text = claude_json_path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "cli_path": None,
            "project_root": None,
            "error": f"could not read/parse {claude_json_path}: {exc}",
        }

    servers = data.get(_MCP_SERVERS_KEY)
    if not isinstance(servers, dict) or server_name not in servers:
        return {
            "cli_path": None,
            "project_root": None,
            "error": f"mcpServers.{server_name!r} not found in {claude_json_path}",
        }

    server = servers[server_name]
    args = server.get(_ARGS_KEY) if isinstance(server, dict) else None
    if not isinstance(args, list) or not args:
        return {
            "cli_path": None,
            "project_root": None,
            "error": f"mcpServers.{server_name!r}.args missing or empty",
        }

    cli_path = _pick_cli_path(args)
    project_root = args[-1] if isinstance(args[-1], str) else None

    if cli_path is None or project_root is None:
        return {
            "cli_path": None,
            "project_root": None,
            "error": f"mcpServers.{server_name!r}.args did not yield both cli_path and project_root",
        }

    return {"cli_path": cli_path, "project_root": project_root}


@register_op("mcp.resolve_server_cli_path")
def _handler(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'mcp.resolve_server_cli_path' handler.

    Params:
        server_name (str, required): the `mcpServers` key to resolve
            (e.g. "project-rag").
        claude_json_path (str, optional): override path to the
            `.claude.json` file — test/override seam only; production
            callers omit it and get `${CLAUDE_HOME:-$HOME}/.claude.json`.

    Returns: {"cli_path": str, "project_root": str} on success, or
    {"cli_path": None, "project_root": None, "error": str} on failure.
    No repo_root use — this op is scope "none" (global per-user file, no
    caller-repo state).
    """
    server_name = params.get("server_name")
    if not isinstance(server_name, str) or not server_name:
        return {
            "cli_path": None,
            "project_root": None,
            "error": "'server_name' must be a non-empty str",
        }

    override = params.get("claude_json_path")
    claude_json_path = Path(override) if override else _default_claude_json_path()

    return resolve_cli_path_and_root(claude_json_path, server_name)

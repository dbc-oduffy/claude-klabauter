"""agy writer: one example-retrieval-repo mcpServers entry in mcp_config.json.

apply() reads and writes one config file: the JSON-merged example-retrieval-repo entry in
HostPlan.config_path. It never writes agy's hooks.json (wiring is DoE's). Other servers and top-level keys are preserved
(values and order; whitespace is re-serialised). An existing example-retrieval-repo entry is never overwritten:
equal to ours it is current, otherwise drift. A config that is unreadable or not a JSON object, or
whose mcpServers is not an object, is reported malformed and left byte-identical.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from coordinator_core.install.write_surface import StaticClause, WriteSurfaceDeclaration, WriteSurfaceEntry
from coordinator_core.install.host_targets import HOST_AGY, MCP_SERVER_NAME, HostPlan, plan

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = "8767"

#: mcp status values.
MCP_WRITTEN = "written"
MCP_CURRENT = "current"
MCP_WOULD_WRITE = "would-write"
MCP_DRIFT = "drift"
MCP_MALFORMED = "malformed"


@dataclass(frozen=True)
class AgyApply:
    """Outcome of one apply(): the MCP status plus human-readable notes."""

    mcp: str
    config_path: Path
    notes: tuple[str, ...] = field(default_factory=tuple)


def expected_url(env: Mapping[str, str]) -> str:
    """The example-retrieval-repo Streamable HTTP address: EXAMPLE_RETRIEVAL_REPO_HTTP_HOST/PORT over the 127.0.0.1:8767 default."""
    host = env.get("EXAMPLE_RETRIEVAL_REPO_HTTP_HOST") or DEFAULT_HOST
    port = env.get("EXAMPLE_RETRIEVAL_REPO_HTTP_PORT") or DEFAULT_PORT
    return f"http://{host}:{port}/mcp"


def render_entry(url: str) -> dict[str, Any]:
    """The example-retrieval-repo mcpServers value; agy remote servers use `serverUrl` (httpUrl and SSE are unsupported)."""
    return {"serverUrl": url}


class HostInstallRefused(RuntimeError):
    """A real host-config write refused by COORDINATOR_DISABLE_MACHINE_MUTATION."""


def _atomic_write(path: Path, data: str) -> None:
    # Trap: these are the operator's own host configs, outside the install territory, so the
    # machine-mutation switch must reach them; temp paths are allowed (tests root home in tmp_path).
    from coordinator_core.install import substrate as _substrate_mod

    blocked = _substrate_mod._refuse_machine_mutation(str(path), what="agy host config write", check_temp_path=False)
    if blocked:
        raise HostInstallRefused(blocked)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        tmp.write_bytes(data.encode("utf-8"))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _apply_mcp(hp: HostPlan, url: str, check: bool, notes: list[str]) -> str:
    path = hp.config_path
    doc: dict[str, Any] = {}
    if path.exists():
        try:
            raw = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            notes.append(f"{path}: unreadable ({exc}); left untouched")
            return MCP_MALFORMED
        if raw.strip():
            try:
                loaded = json.loads(raw)
            except json.JSONDecodeError as exc:
                notes.append(f"{path}: not valid JSON ({exc}); left untouched")
                return MCP_MALFORMED
            if not isinstance(loaded, dict):
                notes.append(f"{path}: top level is not a JSON object; left untouched")
                return MCP_MALFORMED
            doc = loaded
    servers = doc.get("mcpServers", {})
    if not isinstance(servers, dict):
        notes.append(f"{path}: mcpServers is not a JSON object; left untouched")
        return MCP_MALFORMED
    wanted = render_entry(url)
    existing = servers.get(MCP_SERVER_NAME)
    if existing is not None:
        got = existing.get("serverUrl") if isinstance(existing, dict) else None
        if got == url:
            return MCP_CURRENT
        notes.append(
            f"{path}: existing {MCP_SERVER_NAME} entry differs from {url} "
            f"(found {got if got else 'a non-serverUrl transport'}); not overwritten"
        )
        return MCP_DRIFT
    if check:
        return MCP_WOULD_WRITE
    servers[MCP_SERVER_NAME] = wanted
    doc["mcpServers"] = servers
    _atomic_write(path, json.dumps(doc, indent=2) + "\n")
    return MCP_WRITTEN


def apply(
    home: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    check: bool = False,
    platform: Optional[str] = None,
) -> AgyApply:
    """Install the agy example-retrieval-repo entry; check=True writes nothing."""
    env = os.environ if env is None else env
    hp = plan(HOST_AGY, home, env, platform)
    notes: list[str] = []
    mcp = _apply_mcp(hp, expected_url(env), check, notes)
    return AgyApply(mcp, hp.config_path, tuple(notes))


WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="host-install-agy",
    source_module="coordinator_core.install.host_install_agy",
    clauses=(
        StaticClause(
            entries=(
                WriteSurfaceEntry(kind="file-path", path="<home>/.gemini/config/mcp_config.json", reason="_apply_mcp: merges the managed example-retrieval-repo mcpServers entry into the agy MCP config"),
            ),
        ),
    ),
)

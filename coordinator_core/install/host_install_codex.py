"""Codex writer: a managed example-retrieval-repo MCP block in config.toml.

apply() reads and writes one config file: the delimited managed block in $CODEX_HOME/config.toml.
It never writes hooks.json (wiring is DoE's, and an edit would break
Codex's hook-hash trust) and never touches a foreign [mcp_servers.project-rag] entry: one that
exists is reported present (same url) or drift (different), and config.toml is left byte-identical.
Everything outside the managed block is preserved byte for byte.
"""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from coordinator_core.install.write_surface import StaticClause, WriteSurfaceDeclaration, WriteSurfaceEntry
from coordinator_core.install.host_targets import HOST_CODEX, MCP_SERVER_NAME, HostPlan, plan

BLOCK_BEGIN = f"# >>> coordinator-managed mcp_servers.{MCP_SERVER_NAME} >>>"
BLOCK_END = f"# <<< coordinator-managed mcp_servers.{MCP_SERVER_NAME} <<<"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = "8767"

#: mcp status values.
MCP_WRITTEN = "written"
MCP_CURRENT = "current"
MCP_WOULD_WRITE = "would-write"
MCP_PRESENT = "present"
MCP_DRIFT = "drift"
MCP_MALFORMED = "malformed"


@dataclass(frozen=True)
class CodexApply:
    """Outcome of one apply(): the MCP status plus human-readable notes."""

    mcp: str
    config_path: Path
    notes: tuple[str, ...] = field(default_factory=tuple)


def expected_url(env: Mapping[str, str]) -> str:
    """The example-retrieval-repo HTTP MCP address: EXAMPLE_RETRIEVAL_REPO_HTTP_HOST/PORT over the 127.0.0.1:8767 default."""
    host = env.get("EXAMPLE_RETRIEVAL_REPO_HTTP_HOST") or DEFAULT_HOST
    port = env.get("EXAMPLE_RETRIEVAL_REPO_HTTP_PORT") or DEFAULT_PORT
    return f"http://{host}:{port}/mcp"


def render_block(url: str) -> str:
    """The managed block text, newline-terminated, with a TOML-escaped url."""
    return (
        f"{BLOCK_BEGIN}\n"
        f"[mcp_servers.{MCP_SERVER_NAME}]\n"
        f"url = {json.dumps(url)}\n"
        f"{BLOCK_END}\n"
    )


def _split_block(text: str) -> tuple[str, Optional[str], str]:
    """(before, managed block or None, after); the block spans BEGIN line through END line."""
    start = text.find(BLOCK_BEGIN)
    if start < 0:
        return text, None, ""
    end = text.find(BLOCK_END, start)
    if end < 0:
        return text, None, ""
    stop = text.find("\n", end)
    stop = len(text) if stop < 0 else stop + 1
    return text[:start], text[start:stop], text[stop:]


class HostInstallRefused(RuntimeError):
    """A real host-config write refused by COORDINATOR_DISABLE_MACHINE_MUTATION."""


def _atomic_write(path: Path, data: str) -> None:
    # Trap: these are the operator's own host configs, outside the install territory, so the
    # machine-mutation switch must reach them; temp paths are allowed (tests root home in tmp_path).
    from coordinator_core.install import substrate as _substrate_mod

    blocked = _substrate_mod._refuse_machine_mutation(str(path), what="codex host config write", check_temp_path=False)
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
    try:
        text = hp.config_path.read_bytes().decode("utf-8") if hp.config_path.exists() else ""
    except (OSError, UnicodeDecodeError) as exc:
        notes.append(f"{hp.config_path}: unreadable ({exc}); left untouched")
        return MCP_MALFORMED
    before, block, after = _split_block(text)
    if block is None and BLOCK_BEGIN in text:
        notes.append(f"{hp.config_path}: managed block has no END marker; left untouched")
        return MCP_MALFORMED
    foreign_text = before + after
    try:
        data = tomllib.loads(foreign_text)
    except tomllib.TOMLDecodeError as exc:
        notes.append(f"{hp.config_path}: not valid TOML ({exc}); left untouched")
        return MCP_MALFORMED
    entry = (data.get("mcp_servers") or {}).get(MCP_SERVER_NAME)
    if entry is not None:
        if block is not None:
            notes.append(f"{hp.config_path}: managed block coexists with a foreign {MCP_SERVER_NAME} entry; left untouched")
        got = entry.get("url") if isinstance(entry, dict) else None
        if got == url:
            return MCP_PRESENT
        notes.append(
            f"{hp.config_path}: foreign {MCP_SERVER_NAME} entry differs from {url} "
            f"(found {got if got else 'a non-url transport'}); not overwritten"
        )
        return MCP_DRIFT
    wanted = render_block(url)
    if block == wanted:
        return MCP_CURRENT
    if check:
        return MCP_WOULD_WRITE
    if block is not None:
        _atomic_write(hp.config_path, before + wanted + after)
    else:
        sep = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
        _atomic_write(hp.config_path, text + sep + wanted)
    return MCP_WRITTEN


def apply(
    home: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    check: bool = False,
    platform: Optional[str] = None,
) -> CodexApply:
    """Install the Codex managed MCP block; check=True writes nothing."""
    env = os.environ if env is None else env
    hp = plan(HOST_CODEX, home, env, platform)
    notes: list[str] = []
    mcp = _apply_mcp(hp, expected_url(env), check, notes)
    return CodexApply(mcp, hp.config_path, tuple(notes))


WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="host-install-codex",
    source_module="coordinator_core.install.host_install_codex",
    clauses=(
        StaticClause(
            entries=(
                WriteSurfaceEntry(kind="file-path", path="<home>/.codex/config.toml (or $CODEX_HOME/config.toml)", reason="_apply_mcp: merges the managed example-retrieval-repo MCP block into the Codex config"),
            ),
        ),
    ),
)

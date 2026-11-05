"""Pure per-host install planner for the Codex and agy foreign hosts.

plan(host, home, env) answers "what would an install change, what must the host provide, and what
can we not reach" for one host. No writes, no subprocess: paths derive from home/env, binary
detection is shutil.which (Codex also via resolve_codex). The per-host writers and the setup step
consume HostPlan.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from coordinator_core.codex_runner.resolve import CodexUnavailable, resolve_codex

HOST_CODEX = "codex"
HOST_AGY = "agy"
HOSTS = (HOST_CODEX, HOST_AGY)

#: Managed MCP server name both writers own.
MCP_SERVER_NAME = "project-rag"


@dataclass(frozen=True)
class HostPlan:
    """What an install onto one host changes, needs and cannot reach.

    config_path is the host's MCP config file, hooks_path the host's own hooks file (never written
    by claude-klabauter). binary is the detected host executable argv head, or None when the host is not
    installed.
    """

    host: str
    binary: Optional[str]
    config_path: Path
    hooks_path: Path
    instructions_path: Path
    skills_dir: Path
    changes: tuple[str, ...]
    host_provides: tuple[str, ...]
    unreachable: tuple[str, ...]


def _is_windows(platform: Optional[str]) -> bool:
    return (platform or sys.platform).startswith("win")


def resolve_home(env: Mapping[str, str], platform: Optional[str] = None) -> Path:
    """USERPROFILE on Windows, HOME elsewhere, else Path.home()."""
    key = "USERPROFILE" if _is_windows(platform) else "HOME"
    raw = env.get(key)
    return Path(raw) if raw else Path.home()


def _detect(host: str, env: Mapping[str, str], platform: Optional[str]) -> Optional[str]:
    path = env.get("PATH", "")
    if host == HOST_CODEX:
        try:
            return resolve_codex(platform, dict(env))[-1]  # argv is [node, codex_js] under fnm, else [codex]
        except CodexUnavailable:
            return shutil.which("codex", path=path)
    return shutil.which("agy", path=path)


def plan(
    host: str,
    home: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    platform: Optional[str] = None,
) -> HostPlan:
    """Return the HostPlan for `host`; raise ValueError for a host outside HOSTS."""
    if host not in HOSTS:
        raise ValueError(f"unknown host {host!r}; expected one of {HOSTS}")
    env = os.environ if env is None else env
    home = Path(home) if home is not None else resolve_home(env, platform)
    binary = _detect(host, env, platform)
    if host == HOST_CODEX:
        root = Path(env["CODEX_HOME"]) if env.get("CODEX_HOME") else home / ".codex"
        return HostPlan(
            host=host,
            binary=binary,
            config_path=root / "config.toml",
            hooks_path=root / "hooks.json",
            instructions_path=root / "AGENTS.md",
            skills_dir=root / "skills",
            changes=(
                f"managed [mcp_servers.{MCP_SERVER_NAME}] block in {root / 'config.toml'} "
                "(skipped when a example-retrieval-repo entry already exists)",
            ),
            host_provides=(
                "hooks.json PreToolUse/SessionStart wiring, trusted by hash in config.toml",
                "SKILL.md skills discovery under $CODEX_HOME/skills",
                "MCP stdio and HTTP servers via [mcp_servers.*]",
                "AGENTS.md doctrine loading",
            ),
            unreachable=(
                f"{root / 'hooks.json'}: wiring is DoE's, never written here (hook-hash trust)",
                "AGENTS.md doctrine delivery and per-host skill rendering (DoE-owned)",
                "agent_id in hook payloads: no Codex subagent-identity analogue",
            ),
        )
    gemini = home / ".gemini"
    return HostPlan(
        host=host,
        binary=binary,
        config_path=gemini / "config" / "mcp_config.json",
        hooks_path=gemini / "config" / "hooks.json",
        instructions_path=gemini / "GEMINI.md",
        skills_dir=gemini / "config" / "skills",
        changes=(
            f"managed {MCP_SERVER_NAME} entry merged into {gemini / 'config' / 'mcp_config.json'}",
        ),
        host_provides=(
            "mcp_config.json mcpServers loading",
            # Per the live agy self-report (2026-11-04): tool events group under matcher/hooks; SessionStart takes flat handlers.
            "hooks.json events SessionStart/PreToolUse/PostToolUse/PreInvocation/PostInvocation/Stop",
            "skills/<name>/SKILL.md and .agents/rules discovery",
            "plugin import route (agy plugin import claude)",
        ),
        unreachable=(
            f"{gemini / 'config' / 'hooks.json'}: wiring is DoE's, never written here",
            "GEMINI.md/AGENTS.md doctrine delivery and skill rendering (DoE-owned)",
            "Claude-tier model mapping and subagent dispatch semantics",
        ),
    )

"""The one host declaration every installer reads: which hosts exist, their CLI binaries and home dirs.

Stdlib only: host_hooks/shim.py imports contract.py on a cold interpreter, and setup.py,
coordinator_install.py and unimatrix-install.py load this by file path before the engine is importable.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


@dataclass(frozen=True)
class HostSpec:
    """One host. home_env, when set, overrides the host's state dir (or, when home_env_is_base, the
    base the home_default dir hangs off). foreign: not Claude. plugin_host: the hosts
    coordinator_install.build_plan installs the Claude plugin into."""

    id: str
    binaries: tuple[str, ...]
    home_env: Optional[str]
    home_default: str
    foreign: bool
    plugin_host: bool
    home_env_is_base: bool = False


REGISTRY: tuple[HostSpec, ...] = (
    HostSpec("claude", ("claude", "claude.exe", "claude.cmd"), "CLAUDE_HOME", ".claude", False, True, True),
    HostSpec("codex", ("codex", "codex.exe", "codex.cmd"), "CODEX_HOME", ".codex", True, False),
    HostSpec("agy", ("agy", "agy.exe", "agy.cmd"), None, ".gemini", True, False),
    HostSpec("grok", ("grok", "grok.exe", "grok.cmd"), "GROK_HOME", ".grok", True, True),
)

#: Ids of every registered host, registry order.
IDS: tuple[str, ...] = tuple(s.id for s in REGISTRY)
#: Ids of the non-Claude hosts, registry order.
FOREIGN: tuple[str, ...] = tuple(s.id for s in REGISTRY if s.foreign)


def ids() -> tuple[str, ...]:
    """Every registered host id."""
    return IDS


def foreign_ids() -> tuple[str, ...]:
    """Ids of the non-Claude hosts."""
    return FOREIGN


def plugin_host_ids() -> tuple[str, ...]:
    """Ids of the hosts the Claude plugin installs into."""
    return tuple(s.id for s in REGISTRY if s.plugin_host)


def spec(host_id: str) -> HostSpec:
    """The HostSpec for host_id; ValueError when unregistered."""
    for s in REGISTRY:
        if s.id == host_id:
            return s
    raise ValueError(f"unknown host {host_id!r}; expected one of {IDS}")


def user_home(env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None) -> Path:
    """USERPROFILE on Windows, HOME elsewhere, else Path.home()."""
    env = os.environ if env is None else env
    key = "USERPROFILE" if (platform or sys.platform).startswith("win") else "HOME"
    raw = env.get(key)
    return Path(raw) if raw else Path.home()


def home(host: HostSpec, env: Optional[Mapping[str, str]] = None, platform: Optional[str] = None) -> Path:
    """The host's state dir: its home_env override, else <user home>/<home_default>."""
    env = os.environ if env is None else env
    override = env.get(host.home_env) if host.home_env else None
    if override:
        return Path(override) / host.home_default if host.home_env_is_base else Path(override)
    return user_home(env, platform) / host.home_default

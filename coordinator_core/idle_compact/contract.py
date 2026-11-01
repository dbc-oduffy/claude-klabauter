"""Idle-compaction watcher contract: thresholds, log record, path resolvers, Injector protocol.

Every other idle_compact module builds on these names. Negative-spec: the settings
home is resolved exactly as ``session.context_usage_sidecar.sidecar_path`` does
(``COORDINATOR_SETTINGS_HOME`` else ``~/.coordinator-claude-settings``) — never a tempdir.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

IDLE_SECONDS = 300
CONTEXT_PCT = 18.0
TICK_SECONDS = 30

_SETTINGS_HOME_ENV = "COORDINATOR_SETTINGS_HOME"
_DEFAULT_SETTINGS_HOME = ".coordinator-claude-settings"

OPTOUT_MARKER_NAME = "idle-compact-optout"
COMPACT_HOLD_MARKER_NAME = "compact-hold"


@dataclass(frozen=True)
class InjectResult:
    """Outcome of one injection attempt; ``reason`` is a terse human-readable cause."""

    ok: bool
    reason: str


@runtime_checkable
class Injector(Protocol):
    """Per-OS keystroke injector: types ``/compact`` into the console owned by ``pid``."""

    def inject(self, pid: int) -> InjectResult: ...


@dataclass(frozen=True)
class LogRecord:
    """One line of the injection log (``injections.jsonl``)."""

    ts: str
    run_id: str
    session_id: str
    pid: int
    action: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def settings_home() -> Path:
    home = os.environ.get(_SETTINGS_HOME_ENV)
    return Path(home) if home else Path.home() / _DEFAULT_SETTINGS_HOME


def injection_log_path() -> Path:
    """``<settings>/state/idle-compact/injections.jsonl``."""
    return settings_home() / "state" / "idle-compact" / "injections.jsonl"


def allowlist_path() -> Path:
    """``<settings>/idle-compact-allowlist.json``: a JSON list of session names / repo cwd
    basenames. An absent file means no session is allowlisted."""
    return settings_home() / "idle-compact-allowlist.json"


def _hub_marker(session_id: str, name: str, cwd: Optional[str]) -> Optional[Path]:
    from coordinator_core.session.core import session_dir

    hub = session_dir(session_id, cwd)
    return Path(hub) / name if hub else None


def optout_marker_path(session_id: str, cwd: Optional[str] = None) -> Optional[Path]:
    """``<git-common-dir>/coordinator-sessions/<sid>/idle-compact-optout``; None outside a git repo."""
    return _hub_marker(session_id, OPTOUT_MARKER_NAME, cwd)


def compact_hold_marker_path(session_id: str, cwd: Optional[str] = None) -> Optional[Path]:
    """``<git-common-dir>/coordinator-sessions/<sid>/compact-hold``; None outside a git repo."""
    return _hub_marker(session_id, COMPACT_HOLD_MARKER_NAME, cwd)

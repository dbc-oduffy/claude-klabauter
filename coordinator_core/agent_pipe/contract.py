"""agent_pipe.contract -- message shape, adapter protocol and on-disk locations every agent_pipe module shares.

Layout under `<settings_home>/agent-pipe`: `runs/<run_id>` per pipe run, `mailboxes/<session_id>.jsonl`
per receiving Claude session (one JSON object per line).
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional, Protocol

from coordinator_core._settings_home import settings_home

AGENTS = ("antigravity", "codex")

PIPE_DIRNAME = "agent-pipe"

ORIGIN_AGENT_INITIATED = "agent-initiated"


@dataclass(frozen=True)
class PipeMessage:
    """One unprompted reply from a headless agent session to a live Claude session.

    `origin` is provenance only (e.g. ORIGIN_AGENT_INITIATED); content is never rewritten to carry it.
    """

    id: str
    sender_agent: str
    sender_conversation: str
    to_session: str
    content: str
    ts: str
    origin: Optional[str] = None

    def to_json_line(self) -> str:
        """Single-line JSON, no trailing newline; embedded newlines in content stay escaped.

        A None origin is dropped so lines without provenance stay byte-identical to the six-key shape.
        """
        d = asdict(self)
        if d["origin"] is None:
            del d["origin"]
        return json.dumps(d, ensure_ascii=False, separators=(",", ":"))


KIND_CLAUDE = "claude"
KIND_CODEX = "codex"
KIND_AGY = "agy"
KIND_GROK = "grok"
KINDS = (KIND_CLAUDE, KIND_CODEX, KIND_AGY, KIND_GROK)


def provenance_envelope(
    kind: str,
    source: str,
    *,
    origin: Optional[str] = None,
    ts: Optional[str] = None,
    cli_version: Optional[str] = None,
    session: Optional[str] = None,
) -> dict:
    """Provenance dict for a fact emitted by vendor `kind`: the bridge's from_kind/origin/ts shape plus vendor/source/cli_version.

    `session` is the emitting vendor session id (carried as from_identity); None-valued keys are dropped.
    """
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    env = {
        "from_identity": session,
        "from_kind": kind,
        "origin": origin,
        "ts": ts,
        "vendor": kind,
        "source": source,
        "cli_version": cli_version,
    }
    return {k: v for k, v in env.items() if v is not None}


@dataclass(frozen=True)
class BridgeMessage:
    """One labelled message between two roster identities (any pair of claude, codex, agy).

    `from_identity`/`to_identity` are durable roster names, never session ids or tokens. `origin` and
    `label` are provenance only; `content` is never rewritten to carry them.
    """

    id: str
    from_identity: str
    from_kind: str
    to_identity: str
    content: str
    ts: str
    origin: Optional[str] = None
    label: Optional[str] = None

    def to_json_line(self) -> str:
        """Single-line JSON, no trailing newline; None origin/label are dropped."""
        d = {k: v for k, v in asdict(self).items() if v is not None}
        return json.dumps(d, ensure_ascii=False, separators=(",", ":"))


class Adapter(Protocol):
    """Per-agent CLI knowledge: how to start, resume and read one headless session."""

    def spawn_argv(self, prompt: str, conversation: Optional[str] = None) -> list[str]: ...

    def resume_argv(self, conversation: str, text: str) -> list[str]: ...

    def parse_line(self, line: str) -> Optional[tuple[str, str]]:
        """Return (conversation, content) for a line carrying a message, else None."""
        ...


CAUSE_QUOTA = "quota_exhausted"
CAUSE_TURN_TIMEOUT = "turn_timeout"
CAUSE_HANDSHAKE_TIMEOUT = "handshake_timeout"
CAUSE_LOGIN_REQUIRED = "login_required"

_QUOTA_RE = re.compile(
    r"usage[ _-]?limit|quota|rate[ _-]?limit|too many requests|resource[ _-]?exhausted|\b429\b|out of credits|insufficient[ _-]credit",
    re.IGNORECASE,
)
_LOGIN_RE = re.compile(r"not logged in|login required|unauthenticated|\b401\b", re.IGNORECASE)
_HANDSHAKE_RE = re.compile(r"no upgrade reply|upgrade refused|handshake", re.IGNORECASE)
_TURN_TIMEOUT_RE = re.compile(r"timed out|timeout", re.IGNORECASE)
_ERROR_EVENTS = ("error", "turn.failed", "turn_failed", "failed")


def _event_error_text(ev: dict) -> Optional[str]:
    """The error text of a stream event that reports a failure, else None (agent output is never an error)."""
    kind = ev.get("type") or ev.get("event")
    err = ev.get("error")
    if kind not in _ERROR_EVENTS and not err and not (kind == "result" and ev.get("is_error")):
        return None
    parts: list[str] = []
    for v in (err, ev.get("message"), ev.get("result")):
        if isinstance(v, dict):
            v = v.get("message") or json.dumps(v)
        if isinstance(v, str):
            parts.append(v)
    return " ".join(parts) or json.dumps(ev)


def classify_exit(line_or_status: str) -> Optional[str]:
    """Cause of a failed agent leg: quota_exhausted | login_required | handshake_timeout | turn_timeout, or None.

    Takes a stream line (only error-typed JSON events count; a message body mentioning "rate limit" is
    not a cause) or a plain status/exception text.
    """
    text = line_or_status.strip() if isinstance(line_or_status, str) else ""
    if not text:
        return None
    if text.startswith("{"):
        try:
            ev = json.loads(text)
        except ValueError:
            ev = None
        if isinstance(ev, dict):
            text = _event_error_text(ev) or ""
            if not text:
                return None
    if _QUOTA_RE.search(text):
        return CAUSE_QUOTA
    if _LOGIN_RE.search(text):
        return CAUSE_LOGIN_REQUIRED
    if _HANDSHAKE_RE.search(text):
        return CAUSE_HANDSHAKE_TIMEOUT
    if _TURN_TIMEOUT_RE.search(text):
        return CAUSE_TURN_TIMEOUT
    return None


def _checked_id(kind: str, value: str) -> str:
    if not value or value in (".", "..") or any(c in value for c in "/\\\0"):
        raise ValueError(f"invalid {kind}: {value!r}")
    return value


def _pipe_root() -> Path:
    return settings_home() / PIPE_DIRNAME


def run_dir(run_id: str) -> Path:
    """Run directory `<settings_home>/agent-pipe/runs/<run_id>`; rejects ids that could escape it."""
    return _pipe_root() / "runs" / _checked_id("run_id", run_id)


def mailbox_path(session_id: str) -> Path:
    """Mailbox file `<settings_home>/agent-pipe/mailboxes/<session_id>.jsonl`; rejects ids that could escape it."""
    return _pipe_root() / "mailboxes" / (_checked_id("session_id", session_id) + ".jsonl")


def roster_path() -> Path:
    """Durable roster `<settings_home>/agent-pipe/roster.json`."""
    return _pipe_root() / "roster.json"


def peers_path() -> Path:
    """Peer file `<settings_home>/unimatrix/peers.json`, the one the Codex SessionStart hook writes."""
    return settings_home() / "unimatrix" / "peers.json"


def routes_path() -> Path:
    """PM routing control `<settings_home>/agent-pipe/routes.json`."""
    return _pipe_root() / "routes.json"


def session_lock_path(agent: str, conversation: str) -> Path:
    """Per-session send-queue lock `<settings_home>/agent-pipe/sessions/<agent>/<conversation>.lock`; rejects escaping ids."""
    return _pipe_root() / "sessions" / _checked_id("agent", agent) / (_checked_id("conversation", conversation) + ".lock")

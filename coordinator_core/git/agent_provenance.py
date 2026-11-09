"""coordinator_core.git.agent_provenance -- engine-computed vendor/model/tier/session provenance
for commits made by non-Claude agent sessions.

Detection reads a caller-supplied env mapping only; no subprocess, no git. The result is
attribution for cooperative agents (env is not a credential), never a security boundary.
The four trailer keys are frozen: git `%(trailers:key=X)` matches exactly.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from coordinator_core.agent_pipe.contract import KIND_AGY, KIND_CLAUDE, KIND_CODEX, KINDS

TRAILER_KEYS = ("Agent-Vendor", "Agent-Model", "Agent-Tier", "Agent-Session-Id")
# Frozen producer contract §5.2: checked-in consumer ACK references, never an env bypass.
# Populate only after BOTH consumers explicitly confirm reader readiness.
WRITER_READINESS_ACKS: dict[str, str] = {}


def emitted_provenance_trailers(p: "AgentProvenance") -> list[str]:
    """Emit only after rag and cockpit ACK the optional-key widening."""
    if not all(WRITER_READINESS_ACKS.get(consumer, "").strip()
               for consumer in ("project-rag", "example-cockpit-repo")):
        return []
    return provenance_trailers(p)

VENDOR_ENV = "COORDINATOR_AGENT_VENDOR"
MODEL_ENV = "COORDINATOR_AGENT_MODEL"
SESSION_ENV = "COORDINATOR_AGENT_SESSION"

CODEX_SESSION_ENV = "CODEX_THREAD_ID"
AGY_SESSION_ENV = "ANTIGRAVITY_CONVERSATION_ID"
CLAUDE_ENV = "CLAUDECODE"

ADMISSION_MARKER = ".coordinator/agent-admission.json"

UNKNOWN = "unknown"
TIER_FRONTIER = "frontier"
TIER_STANDARD = "standard"

# Ordered (vendor, model-id prefix, tier); first prefix match wins. Light-weight markers are
# checked before the frontier prefixes. An id matching nothing is `unknown`, never guessed.
_STANDARD_MARKERS = ("mini", "flash", "lite", "nano", "haiku")
_FRONTIER_PREFIXES = (
    ("codex", "gpt-5"),
    ("agy", "gemini-4"),
    ("grok", "grok-4"),
)


@dataclass(frozen=True)
class AgentProvenance:
    """Who is committing: `vendor` is an agent_pipe KIND other than claude."""

    vendor: str
    model: str
    tier: str
    session: str


def _tier(vendor: str, model: str) -> str:
    from coordinator_core.agent_pipe.model_tiers import model_tier

    mapped = model_tier(vendor, model)
    if mapped != UNKNOWN:
        return mapped
    low = model.lower()
    if "terra" in low:
        return UNKNOWN
    if low == UNKNOWN:
        return UNKNOWN
    if any(m in low for m in _STANDARD_MARKERS):
        return TIER_STANDARD
    for v, prefix in _FRONTIER_PREFIXES:
        if v == vendor and low.startswith(prefix):
            return TIER_FRONTIER
    return UNKNOWN


def _clean(value: str) -> str:
    return " ".join(value.split())


def detect_agent_provenance(env: Mapping[str, str]) -> Optional[AgentProvenance]:
    """Provenance of a non-Claude session, or None for Claude, a human, or an unrecognised env.

    Ladder, first hit wins: launcher-stamped COORDINATOR_AGENT_VENDOR (a claude or unknown
    vendor value is a miss, not a hit); CODEX_THREAD_ID -> codex; ANTIGRAVITY_CONVERSATION_ID
    -> agy. CLAUDECODE alone, or nothing, is None.
    """
    vendor = _clean(env.get(VENDOR_ENV, "")).lower()
    native_session = ""
    if vendor in KINDS and vendor != KIND_CLAUDE:
        pass
    elif env.get(CODEX_SESSION_ENV):
        vendor, native_session = KIND_CODEX, env[CODEX_SESSION_ENV]
    elif env.get(AGY_SESSION_ENV):
        vendor, native_session = KIND_AGY, env[AGY_SESSION_ENV]
    else:
        return None
    if not native_session:
        native_session = {
            KIND_CODEX: env.get(CODEX_SESSION_ENV, ""),
            KIND_AGY: env.get(AGY_SESSION_ENV, ""),
        }.get(vendor, "")
    model = _clean(env.get(MODEL_ENV, "")) or UNKNOWN
    session = _clean(env.get(SESSION_ENV, "")) or _clean(native_session) or UNKNOWN
    return AgentProvenance(vendor=vendor, model=model, tier=_tier(vendor, model), session=session)


def provenance_trailers(p: AgentProvenance) -> list[str]:
    """`Key: value` lines in TRAILER_KEYS order."""
    values = (p.vendor, p.model, p.tier, p.session)
    return [f"{k}: {v}" for k, v in zip(TRAILER_KEYS, values)]


def detect_caller_agent_provenance(env: Mapping[str, str]) -> Optional[AgentProvenance]:
    """Attribute a warm request only when its native session matches the carried caller.

    A daemon's ambient native variables belong to its spawner. Actual warm entry points
    supply CALLER-mode variables; this extra identity check keeps unbound/reentrant callers
    from accidentally attributing a commit to that spawner.
    """
    provenance = detect_agent_provenance(env)
    if provenance is None:
        return None
    from coordinator_core.session import core

    if core.in_warm_served_request():
        name = {KIND_CODEX: CODEX_SESSION_ENV, KIND_AGY: AGY_SESSION_ENV}.get(provenance.vendor, SESSION_ENV)
        sid = core.canonical_session_env_value(name, provenance.session, vendor=provenance.vendor)
        if sid != core.carried_session_id():
            return None
    return provenance


_AGENT_LINE = re.compile(
    r"^(?:" + "|".join(re.escape(k) for k in TRAILER_KEYS) + r")[ \t]*:", re.IGNORECASE
)


def strip_agent_trailers(message: str) -> str:
    """Drop every Agent-* line from the message's final (trailer) paragraph; the rest is byte-identical."""
    lines = message.splitlines(keepends=True)
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    start = end
    while start and lines[start - 1].strip():
        start -= 1
    kept = [ln for ln in lines[start:end] if not _AGENT_LINE.match(ln)]
    if len(kept) == end - start:
        return message
    return "".join(lines[:start] + kept + lines[end:])


def admission_advisory(repo_root: "str | Path", p: Optional[AgentProvenance]) -> Optional[str]:
    """One-line advisory when `p` is non-Claude and the repo marker closes it to non-Claude agents.

    An absent, unreadable or malformed marker means open: None.
    """
    if p is None:
        return None
    try:
        data = json.loads((Path(repo_root) / ADMISSION_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("closed_to_non_claude") is not True:
        return None
    return (
        f"{p.vendor} commit into a repo closed to non-Claude agents ({ADMISSION_MARKER}). "
        f"Route the change through a Claude EM."
    )


def provenance_grep_pattern(vendor: Optional[str] = None) -> str:
    """`git log --grep` pattern (extended regex, use with -E) selecting non-Claude commits.

    `vendor` None matches any Agent-Vendor trailer; a name matches that vendor only.
    Exclude Claude commits with `--invert-grep`.
    """
    value = re.escape(vendor) if vendor else ".+"
    return rf"^{TRAILER_KEYS[0]}: {value}$"

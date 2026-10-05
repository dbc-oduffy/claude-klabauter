"""coordinator_core.hooks.postuse_subagent_compaction_warning —
PostToolUse warm-engine op.

Port of: coordinator-content-repo coordinator/hooks/scripts/postuse-subagent-compaction-warning.py.
Warns a subagent (payload carries `agent_id`) once per band when its transcript-tail
context size nears the auto-compaction threshold. Advisory only: any failure returns
no advisory.

Divergence from DoE: the compaction threshold inputs
(CLAUDE_CODE_AUTO_COMPACT_WINDOW, CLAUDE_AUTOCOMPACT_PCT_OVERRIDE) are read from
`payload["env"]`, then the `env` block of `<claude_home>/settings.json`
(`claude_home` = `payload["env"]["CLAUDE_HOME"]`, else `~/.claude`). `os.environ`
is never read: the engine's process env belongs to no session.

The marker path is identical to DoE's on purpose: while both copies are live, a
warning from either suppresses the other.

No module-level mutable state.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from coordinator_core._hook_envelope import no_advisory, payload_of
from coordinator_core._settings_home import settings_home
from coordinator_core.hooks._envelope import context_only
from coordinator_core.hooks._payload import field
from coordinator_core.ipc import register_op

# Writes warned-band marker files under settings home state, outside the tracked tree.
GENERATES = []

WARN_BANDS = (0.75, 0.90)
_TAIL_BYTES = 262144
_WINDOW_DEFAULT = 200000
_WINDOW_1M = 1000000
_BUFFER_TOKENS = 13000
_PCT_ENV = "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"
_WINDOW_ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"

MESSAGE = (
    "Context compaction is near for this subagent (~{pct}% of auto-compaction, ~{tokens} tokens). "
    "Write your forward log now: decisions, files touched, what remains."
)


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def _env_blocks(payload: dict) -> tuple[dict, dict]:
    """(payload env, settings.json env), each {} when absent or malformed."""
    penv = payload.get("env")
    if not isinstance(penv, dict):
        penv = {}
    home = penv.get("CLAUDE_HOME")
    claude_home = Path(home) / ".claude" if isinstance(home, str) and home else Path.home() / ".claude"
    try:
        block = json.loads((claude_home / "settings.json").read_text(encoding="utf-8")).get("env")
    except (OSError, ValueError, AttributeError):
        block = None
    return penv, block if isinstance(block, dict) else {}


def _threshold_tokens(model_window: float, payload: dict) -> float:
    penv, senv = _env_blocks(payload)

    def configured(name: str) -> float | None:
        value = _number(penv.get(name))
        return value if value is not None else _number(senv.get(name))

    window = min(configured(_WINDOW_ENV) or model_window, model_window)
    budget = window - _BUFFER_TOKENS
    pct = configured(_PCT_ENV)
    if pct is not None:
        budget = min(budget, window * pct / 100.0)
    return budget


def _safe(value: str) -> str:
    return "".join(c for c in value if c.isalnum() or c in "-_")


def _context_tokens(transcript: Path) -> tuple[int | None, str | None]:
    """(tokens, model) from the last non-zero usage row in the transcript tail."""
    try:
        with transcript.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - _TAIL_BYTES))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None, None
    for line in reversed(lines):
        try:
            message = json.loads(line).get("message")
        except (ValueError, AttributeError):
            continue
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            continue
        total = sum(
            v
            for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
            if isinstance(v := usage.get(k), (int, float)) and not isinstance(v, bool)
        )
        if total > 1:
            model = message.get("model")
            return int(total), model if isinstance(model, str) else None
    return None, None


def _window(model: str | None, tokens: int) -> int:
    if (model and "1m" in model.lower()) or tokens > _WINDOW_DEFAULT:
        return _WINDOW_1M
    return _WINDOW_DEFAULT


def _reap_old_markers(directory: Path, max_age_s: float = 86400.0) -> None:
    cutoff = time.time() - max_age_s
    for entry in directory.iterdir():
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue


def _evaluate(payload: dict) -> str | None:
    agent_id = payload.get("agent_id")
    transcript = payload.get("agent_transcript_path") or payload.get("transcript_path")
    if not isinstance(agent_id, str) or not agent_id or not isinstance(transcript, str):
        return None
    tokens, model = _context_tokens(Path(transcript))
    if tokens is None:
        return None
    threshold = _threshold_tokens(_window(model, tokens), payload)
    if not threshold or threshold <= 0:
        return None
    ratio = tokens / threshold
    band = max((i + 1 for i, edge in enumerate(WARN_BANDS) if ratio >= edge), default=0)
    if not band:
        return None
    marker = settings_home() / "state" / "compaction-warned" / f"{_safe(agent_id)}.{band}"
    if marker.exists():
        return None
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        _reap_old_markers(marker.parent)
        marker.write_text("1", encoding="utf-8", newline="\n")
    except OSError:
        return None
    return MESSAGE.format(pct=min(100, round(ratio * 100)), tokens=f"{tokens / 1000:.0f}k")


@register_op("hooks.postuse_subagent_compaction_warning")
def _handler(params: dict, repo_root=None) -> dict:
    payload = payload_of(params)
    try:
        if not field(payload, "agent_id"):
            return no_advisory()
        text = _evaluate(payload)
    except Exception:
        return no_advisory()
    if not text:
        return no_advisory()
    return context_only("PostToolUse", text)

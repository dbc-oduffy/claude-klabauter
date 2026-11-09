"""Model-free commit attribution from the caller, never the warm server."""
from __future__ import annotations

import os
from contextlib import contextmanager
from contextvars import ContextVar

ATTRIBUTIONS = {
    "claude": "Claude <noreply@anthropic.com>",
    "codex": "Codex <noreply@openai.com>",
    "agy": "agy <noreply@google.com>",
    "grok": "Grok <noreply@x.ai>",
    "mistral": "Mistral <noreply@mistral.ai>",
    "deepseek": "DeepSeek <noreply@deepseek.com>",
}
_CALLER = ContextVar("commit_attribution_caller", default=None)


def _provider(env):
    explicit = (env.get("COORDINATOR_AGENT_PROVIDER") or env.get("COORDINATOR_AGENT_VENDOR") or "").strip().lower()
    if explicit:
        return explicit if explicit in ATTRIBUTIONS else ""
    if env.get("CODEX_THREAD_ID"):
        return "codex"
    if env.get("CLAUDE_SESSION_ID") or env.get("CLAUDE_CODE_SESSION_ID"):
        return "claude"
    return ""


@contextmanager
def caller_attribution(env):
    """Bind even an absent identity; nested calls restore the outer caller."""
    token = _CALLER.set(_provider(env or {}))
    try:
        yield
    finally:
        _CALLER.reset(token)


def attribution_value():
    bound = _CALLER.get()
    if bound is not None:
        return ATTRIBUTIONS.get(bound, "")
    from coordinator_core.session.core import in_warm_served_request

    if in_warm_served_request():
        return ""
    provider = _provider(os.environ)
    if os.environ.get("COORDINATOR_AGENT_PROVIDER", "").strip() and not provider:
        return ""
    return ATTRIBUTIONS.get(provider or "claude", "")

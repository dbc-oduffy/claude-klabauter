"""Independent provider children retain ancestry, never the parent's own identity."""
from collections.abc import Mapping

IDENTITY_ENV = (
    "CODEX_THREAD_ID", "ANTIGRAVITY_CONVERSATION_ID", "COORDINATOR_AGENT_VENDOR",
    "COORDINATOR_AGENT_MODEL", "COORDINATOR_AGENT_SESSION", "COORDINATOR_SESSION_ID",
    "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID",
)


def independent_child_env(base: Mapping[str, str], *, provider: str, role: str,
                          parent: str | None = None, model: str | None = None) -> dict[str, str]:
    """Final scrub after caller projection; no native child UUID is invented.

    Existing caller-scoped delegation fields survive unchanged. They grant no EM
    role: the workflow authority gate still validates the exact assigned receipt.
    """
    if role not in ("worker", "reviewer"):
        raise ValueError("independent provider child must be worker or reviewer")
    env = dict(base)
    for name in ("CODEX_THREAD_ID", "ANTIGRAVITY_CONVERSATION_ID", "AGY_SESSION_ID",
                 "COORDINATOR_AGENT_SESSION", "COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID",
                 "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "COORDINATOR_AGENT_MODEL",
                 "COORDINATOR_WORKFLOW_AGENT_TYPE", "COORDINATOR_WORKFLOW_AGENT_LABEL",
                 "COORDINATOR_AGENT_TYPE", "COORDINATOR_AGENT_LABEL", "COORDINATOR_AGENT_WORKER",
                 "COORDINATOR_PARENT_SESSION_ID"):
        env.pop(name, None)
    actual = {"openai":"codex", "anthropic":"claude", "google":"agy", "xai":"grok"}.get(provider, provider)
    env.update(COORDINATOR_AGENT_ROLE=role, COORDINATOR_AGENT_PROVIDER=actual,
               COORDINATOR_AGENT_VENDOR=actual)
    if isinstance(parent, str) and parent:
        env["COORDINATOR_PARENT_SESSION_ID"] = parent
    if isinstance(model, str) and model:
        env["COORDINATOR_AGENT_MODEL"] = model
    return env


def provider_identity_env(base: Mapping[str, str], source: Mapping[str, str], *,
                          provider: str = "codex", role: str = "reviewer") -> dict[str, str]:
    """Apply warm/cold caller projection, then make the independent child fresh."""
    from coordinator_core.session.core import attributable_session_id, subprocess_identity_env
    env = dict(base)
    env.update({key: source[key] for key in IDENTITY_ENV if source.get(key)})
    projected = subprocess_identity_env(base=env)
    return independent_child_env(projected, provider=provider, role=role,
                                 parent=attributable_session_id())

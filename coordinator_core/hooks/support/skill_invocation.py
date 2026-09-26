
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import json


def normalize_command_name(name: object) -> str:
    if not isinstance(name, str):
        return ""
    return name.rsplit(":", 1)[-1]


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _as_optional_str(value: object) -> Optional[str]:
    return value if isinstance(value, str) else None


@dataclass(frozen=True)
class Invocation:

    command_name: str
    command_args: str
    session_id: str
    cwd: str
    agent_id: Optional[str]


def _read_user_prompt_expansion(payload: dict) -> Invocation:
    return Invocation(
        command_name=normalize_command_name(payload.get("command_name")),
        command_args=_as_str(payload.get("command_args")).strip(),
        session_id=_as_str(payload.get("session_id")),
        cwd=_as_str(payload.get("cwd")),
        agent_id=_as_optional_str(payload.get("agent_id")),
    )


def _read_pre_tool_use_skill(payload: dict) -> Optional[Invocation]:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    verb = tool_input.get("skill")
    if not (isinstance(verb, str) and verb):
        verb = tool_input.get("command")

    return Invocation(
        command_name=normalize_command_name(verb),
        command_args=_as_str(tool_input.get("args")).strip(),
        session_id=_as_str(payload.get("session_id")),
        cwd=_as_str(payload.get("cwd")),
        agent_id=_as_optional_str(payload.get("agent_id")),
    )


def read_invocation(payload: dict) -> Optional[Invocation]:
    if not isinstance(payload, dict):
        return None

    try:
        if "command_name" in payload:
            return _read_user_prompt_expansion(payload)
        if payload.get("tool_name") == "Skill":
            return _read_pre_tool_use_skill(payload)
    except Exception:
        return None

    return None


def context_envelope(event: str, text: str) -> str:
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    )

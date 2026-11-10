"""agent_pipe.agy -- Antigravity adapter: headless `agy -p ... --output-format stream-json` in, `--conversation <id> -p` out.

Messages are `step_update` events of `step_type == "agent_response"` with non-blank `text_delta`;
every other event (init, user_input, whitespace-only DONE delta, result) is not a message. The
conversation id rides on each step_update.
"""
from __future__ import annotations

import json
import shutil
from typing import Optional, Sequence

from coordinator_core.agent_pipe.acp import AcpDriver
from coordinator_core.agent_pipe.contract import KIND_AGY, classify_exit
from coordinator_core.agent_pipe.session_driver import ApprovalCallback

_OUTPUT_FLAGS = ["--output-format", "stream-json"]
_LEG_SCRIPTS = (
    "unimatrix-agy-leg.py",
    "unimatrix-agy-hook-launch.py",
    "unimatrix-peer-agy.py",
    "unimatrix-roster.py",
)
_GIT_VERBS = ("status", "diff", "log", "show", "rev-parse", "add", "commit")
_GIT_ALLOWED_VERBS = frozenset(_GIT_VERBS)
_INTERPRETERS = frozenset({"python", "python3"})
_SHELL_META = frozenset(";&|`$<>\n\r")
DEFAULT_ALLOW_COMMANDS: tuple[str, ...] = (
    *(f"{py} coordinator/bin/{s}" for py in ("python", "python3") for s in _LEG_SCRIPTS),
    *(f"nohup {py} coordinator/bin/{s}" for py in ("python", "python3") for s in _LEG_SCRIPTS),
    *(f"git {v}" for v in _GIT_VERBS),
)


def _settings_flags(allow_commands: Sequence[str]) -> list[str]:
    """Inline --settings granting each command prefix as `command(<prefix> *)`; headless agy auto-denies unlisted commands."""
    rules = [f"command({c} *)" for c in allow_commands]
    return ["--settings", json.dumps({"permissions": {"allow": rules}})]


def _validated(allow_commands: Sequence[str]) -> tuple[str, ...]:
    """Exact command prefixes (whitespace-normalised, multi-token allowed); refuses wildcards, shell metacharacters, a bare interpreter/git/nohup, `python -c`, and any git verb outside the local set (so never push or fetch)."""
    cleaned = tuple(" ".join(str(c).split()) for c in allow_commands)
    for c in cleaned:
        if not c or "*" in c or "(" in c or ")" in c or any(ch in _SHELL_META for ch in c):
            raise ValueError(f"agy command allowlist entry must be a plain command prefix, got {c!r}")
        tokens = c.split(" ")
        if tokens[0] == "nohup":
            tokens = tokens[1:]
        head = tokens[0] if tokens else ""
        if not tokens or head == "nohup":
            raise ValueError(f"agy command allowlist entry must not be a bare nohup, got {c!r}")
        if head in _INTERPRETERS and (len(tokens) < 2 or tokens[1].startswith("-")):
            raise ValueError(f"agy command allowlist entry must name a script path after the interpreter, got {c!r}")
        if head == "git" and (len(tokens) < 2 or tokens[1] not in _GIT_ALLOWED_VERBS):
            raise ValueError(f"agy command allowlist git entry must name a local verb {sorted(_GIT_ALLOWED_VERBS)}, got {c!r}")
    if not cleaned:
        raise ValueError("agy command allowlist must not be empty")
    return cleaned


def _agy() -> str:
    return shutil.which("agy") or "agy"


class AgyAdapter:
    """Adapter for the Antigravity CLI; argv carries scoped `command(<prefix> *)` allow rules via --settings and never a permission-bypass flag or a bare wildcard.

    `allow_commands` is the caller's list of command prefixes the leg may run (default: the unimatrix leg scripts under coordinator/bin via python/python3 or nohup of those, plus local git status/diff/log/show/rev-parse/add/commit); an entry failing `_validated` raises ValueError.
    `parse_line` records the first failure `cause` (see contract.classify_exit); stateful per run.
    """

    def __init__(self, allow_commands: Optional[Sequence[str]] = None) -> None:
        self.cause: Optional[str] = None
        self._settings = _settings_flags(_validated(DEFAULT_ALLOW_COMMANDS if allow_commands is None else allow_commands))

    def spawn_argv(self, prompt: str, conversation: Optional[str] = None) -> list[str]:
        """argv for a fresh headless run; a given `conversation` resumes it instead."""
        if conversation:
            return self.resume_argv(conversation, prompt)
        return [_agy(), "-p", prompt, *_OUTPUT_FLAGS, *self._settings]

    def resume_argv(self, conversation: str, text: str) -> list[str]:
        """argv delivering `text` into an existing agy conversation."""
        return [_agy(), "--conversation", conversation, "-p", text, *_OUTPUT_FLAGS, *self._settings]

    def parse_line(self, line: str) -> Optional[tuple[str, str]]:
        """(conversation, content) for an agent_response step_update carrying text, else None."""
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return None
        if not isinstance(event, dict):
            return None
        if event.get("event") != "step_update":
            self.cause = self.cause or classify_exit(line)
            return None
        step = event.get("step_update")
        if not isinstance(step, dict) or step.get("step_type") != "agent_response":
            return None
        text = step.get("text_delta")
        conversation = step.get("conversation_id")
        if not isinstance(text, str) or not text.strip() or not isinstance(conversation, str) or not conversation:
            return None
        return conversation, text.strip()


class AgyAcpDriver(AcpDriver):
    """SessionDriver for agy: the generic ACP client over `agy acp`; `argv` is overridable so tests drive a fake agent."""

    def __init__(self, approve: Optional[ApprovalCallback] = None, argv: Optional[list[str]] = None, **kw) -> None:
        super().__init__(list(argv) if argv is not None else [_agy(), "acp"], approve, kind=KIND_AGY, **kw)

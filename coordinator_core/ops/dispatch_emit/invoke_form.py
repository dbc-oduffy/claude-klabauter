"""One renderer from the engine's POSIX-spelled launcher command to the worker host shell's spelling.

Prompt sites spell the launcher ``"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/<cli>"``.
PowerShell parses neither the ``${VAR:-default}`` expansion nor a quoted string followed by a bare word
(``ParserError: Unexpected token``), and the Grok allow gate denies the ``&`` call operator, so the Windows
form is the bare command name -- the stage's PATH carries the settings-home bin (see the Windows preamble in
``codex_runner.workflow``). POSIX is returned byte-identical.

Trap: apply this at the one point every worker prompt passes (``codex_runner.workflow.agent``), not per
site -- prompts are also assembled at runtime inside emitted JS, where no compose-time pass can reach them.
"""
from __future__ import annotations

import os
import re

POSIX = "posix"
POWERSHELL = "powershell"

_LAUNCHER = re.compile(
    r'"?\$\{COORDINATOR_SETTINGS_HOME:-\$HOME/\.coordinator-claude-settings\}/bin/([A-Za-z0-9._-]+)"?'
)


def host_shell(os_name: str | None = None) -> str:
    return POWERSHELL if (os_name or os.name) == "nt" else POSIX


def render_invoke_for_shell(prompt: str, shell: str) -> str:
    """Rewrite every launcher spelling in ``prompt`` for ``shell``; POSIX is returned unchanged."""
    if shell != POWERSHELL:
        return prompt
    return _LAUNCHER.sub(r"\1", prompt)

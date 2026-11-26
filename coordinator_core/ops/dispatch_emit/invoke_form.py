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
from pathlib import Path

POSIX = "posix"
POWERSHELL = "powershell"
GROK = "grok"

# Canonical POSIX spelling nests the default:
# ${COORDINATOR_SETTINGS_HOME:-${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings}
# The flat $HOME default is the older prompt-site form. Both reach /bin/<cli>.
_SETTINGS_DEFAULT = r"(?:\$HOME|\$\{CLAUDE_HOME:-\$HOME\})"
_SETTINGS_HOME = re.compile(
    rf"\$\{{COORDINATOR_SETTINGS_HOME:-{_SETTINGS_DEFAULT}/\.coordinator-claude-settings\}}"
)
_LAUNCHER = re.compile(
    rf'"?{_SETTINGS_HOME.pattern}/bin/([A-Za-z0-9._-]+|<[A-Za-z0-9._-]+>)"?'
)


def host_shell(os_name: str | None = None) -> str:
    return POWERSHELL if (os_name or os.name) == "nt" else POSIX


def render_invoke_for_shell(prompt: str, shell: str, bin_dir: Path | None = None) -> str:
    """Rewrite every launcher spelling in ``prompt`` for ``shell``.

    POSIX is returned unchanged. PowerShell becomes the bare command name.
    Grok becomes the absolute settings-home launcher: its allow rules do not
    expand ``${COORDINATOR_SETTINGS_HOME:-$HOME/...}``.
    """
    if shell == GROK:
        root = bin_dir if bin_dir is not None else Path(
            os.environ.get("COORDINATOR_SETTINGS_HOME") or Path.home() / ".coordinator-claude-settings"
        ) / "bin"
        root = Path(root)

        def _absolute_launcher(match: re.Match[str]) -> str:
            return (root / match.group(1)).as_posix()

        rewritten = _LAUNCHER.sub(_absolute_launcher, prompt)
        # A bare expansion (ls of the settings home, no /bin/<cli>) still has to
        # be absolute: Grok allow rules do not expand either spelling.
        return _SETTINGS_HOME.sub(root.parent.as_posix(), rewritten)
    if shell != POWERSHELL:
        return prompt
    return _LAUNCHER.sub(r"\1", prompt)

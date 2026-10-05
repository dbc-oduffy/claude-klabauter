"""coordinator_core.bash_guards.guard_piped_pytest_exit --
``check_piped_pytest_exit``: advisory (never a deny) when a command pipes
``pytest`` into ``tail``/``head``/``grep`` (PowerShell: ``Select-Object``/
``Select-String``) with no ``pipefail``, ``PIPESTATUS`` or ``$LASTEXITCODE``
anywhere in the command -- the pipeline's exit code is the filter's, so a red
run reads green. Pure string work on the command text; no spawn.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._rewrite_support import _advisory
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES

CLASS = "advisory"
MATCHERS = COMMAND_TOOL_NAMES

_QUOTED = re.compile(r'"(?:\\.|[^"\\])*"|\'[^\']*\'')
_PYTEST = re.compile(
    r"(?:^|[\s;&|(])(?:(?:python3?|py)(?:\.exe)?\s+(?:-\S+\s+)*-m\s+pytest|pytest(?:\.exe)?)(?=\s|$|[;&|)])",
    re.IGNORECASE,
)
_SEGMENT_END = re.compile(r"&&|\|\||;|\n")
_FILTER_PIPE = re.compile(
    r"(?<!\|)\|(?!\|)\s*(?:\S*[\\/])?"
    r"(?:tail|head|grep|egrep|fgrep|select-object|select-string|select|sls)(?:\.exe)?(?=\s|$|[;&|)])",
    re.IGNORECASE,
)
_SAFE_MARKERS = ("pipefail", "PIPESTATUS", "$LASTEXITCODE")

_MESSAGE = (
    "PIPED-EXIT-CODE-IS-THE-PIPES: a piped pytest reports the filter's exit code. "
    "Bash: `set -o pipefail` or `${PIPESTATUS[0]}`. PowerShell: read `$LASTEXITCODE` "
    "before any pipe. Either: redirect to a file, then tail it."
)


def check_piped_pytest_exit(
    cmd: str, session_id: str = "", payload: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    if not cmd or "pytest" not in cmd.lower() or "|" not in cmd:
        return None
    if any(marker in cmd for marker in _SAFE_MARKERS):
        return None
    bare = _QUOTED.sub('""', cmd)
    for match in _PYTEST.finditer(bare):
        rest = bare[match.end():]
        end = _SEGMENT_END.search(rest)
        if end:
            rest = rest[: end.start()]
        if _FILTER_PIPE.search(rest):
            return _advisory(_MESSAGE)
    return None

"""Single source of the read-only-probe remedy a refusing bash guard names.

`probe_remedy` is pure: no spawn, no I/O. Each caller class gets the one outlet
that every refusing guard in the dispatch chain admits for it, so a guard's
refusal text can name an action the reader can perform as written.
"""

from __future__ import annotations

from typing import Tuple

from coordinator_core.bash_guards._dialect import Dialect

CALLER_EM = "em"
CALLER_SUBAGENT = "subagent"
CALLER_REVIEWER = "reviewer"

CALLER_CLASSES = (CALLER_EM, CALLER_SUBAGENT, CALLER_REVIEWER)

_SCRIPT_PLACEHOLDER = "<your session scratchpad>/multiprobe.py"

_EM_REMEDY = "one in-process `python3 -c` batching every probe"
_EM_EXEMPLAR = (
    "python3 -c 'import subprocess; "
    'print(subprocess.run(["git", "status", "--porcelain=v2", "--branch"], '
    "capture_output=True, text=True).stdout)'"
)

_REVIEWER_REMEDY = (
    "the Read, Grep and Glob tools; this agent type runs no interpreter script"
)


def _quote_path(path: str) -> str:
    if any(ch.isspace() for ch in path):
        return '"%s"' % path
    return path


def probe_remedy(
    caller_class: str, dialect: Dialect, script_hint: str = ""
) -> Tuple[str, str]:
    """Return ``(remedy_text, exemplar_command)`` for a refused probe shape.

    ``exemplar_command`` is a command line the caller's dialect admits, or the
    empty string for the reviewer class, whose outlet is the harness Read, Grep
    and Glob tools rather than a command. ``script_hint`` is the subagent's
    session-scratchpad script path; blank renders a placeholder path.
    Raises ``ValueError`` on an unknown caller class or dialect.
    """
    if not isinstance(dialect, Dialect):
        raise ValueError("unknown dialect: %r" % (dialect,))
    if caller_class == CALLER_EM:
        return _EM_REMEDY, _EM_EXEMPLAR
    if caller_class == CALLER_SUBAGENT:
        path = _quote_path(script_hint or _SCRIPT_PLACEHOLDER)
        return (
            "a script at `%s`, run as `python3 %s`" % (path, path),
            "python3 %s" % path,
        )
    if caller_class == CALLER_REVIEWER:
        return _REVIEWER_REMEDY, ""
    raise ValueError("unknown caller class: %r" % (caller_class,))

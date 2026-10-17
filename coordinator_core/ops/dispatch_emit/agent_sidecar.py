"""Per-agent sidecar dir and the heredoc-fallback clause every emitted Workflow agent brief ends with.

Pure functions, stdlib only, no I/O. Never imports ``emit`` (emit imports this module).
"""

from __future__ import annotations

import json
import re

SIDECAR_ROOT = ".coordinator-local/workflow-sidecars"
SIDECAR_DIR_MARKER_PREFIX = "sidecar_dir: "

#: Charset shared with the JS slug in ``sidecar_clause_js``; one literal so they cannot drift.
_SLUG_CHARSET = "A-Za-z0-9._-"
_SLUG_BAD_RE = re.compile(f"[^{_SLUG_CHARSET}]")
_JS_SLUG_EXPR = "String({x}).replace(/[^" + _SLUG_CHARSET + "]/g, '-')"

_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

_CLAUSE_HEAD = (
    "\n\n"
    "Sidecar dir (yours, always writable): create it with `mkdir -p`; keep run notes there. "
    "If the harness denies the Write tool on a file your task declares or on a file in this dir "
    "(\"Subagents should return findings as text\"), write it with a Bash heredoc, "
    "`cat > <path> <<'EOF'` -- never skip it -- and name the path in your report. "
    "Use the heredoc only for a path the Write tool refused; once Write has written a path, "
    "keep editing it with Write/Edit, never overwrite it by heredoc (blob drift at commit). "
    "A coordinator guard's denial stays a stop; the cat-heredoc advisory is not a denial. "
    "Quote the delimiter; a frontmatter file written this way skips the write guard, "
    "so report it as unvalidated. "
    "No Bash tool: put the content in your reply. "
    "A provisioned `sidecar_path:` file, when you have one, is still where that report goes.\n"
)


def slug(segment: str) -> str:
    """Map every char outside ``[A-Za-z0-9._-]`` to ``-``."""
    return _SLUG_BAD_RE.sub("-", str(segment))


def _checked_segment(name: str, raw: str) -> str:
    seg = slug(raw)
    if not seg or seg in (".", ".."):
        raise ValueError(f"{name} {raw!r} yields an empty or dot-only sidecar path segment")
    if seg.endswith(".") or seg.endswith(" "):
        raise ValueError(f"{name} {raw!r} yields a trailing dot or space; Windows strips it")
    if seg.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"{name} {raw!r} is a Windows reserved device name")
    return seg


def sidecar_dir(workflow: str, run_key: str, label: str) -> str:
    """Repo-relative forward-slash dir, trailing slash: ``<root>/<workflow>/<run_key>/<label>/``.

    Raises ValueError on an empty segment, a trailing dot/space, or a Windows reserved device name.
    """
    parts = [
        _checked_segment("workflow", workflow),
        _checked_segment("run_key", run_key),
        _checked_segment("label", label),
    ]
    return f"{SIDECAR_ROOT}/{'/'.join(parts)}/"


def sidecar_clause(dir: str) -> str:  # noqa: A002 - name fixed by the plan contract
    """Literal prompt tail starting ``\\n\\n``; its last line is ``sidecar_dir: <dir>``."""
    return f"{_CLAUSE_HEAD}{SIDECAR_DIR_MARKER_PREFIX}{dir}"


def sidecar_clause_js(workflow: str, run_key: str, label_js: str) -> str:
    """JS expression yielding the same text as ``sidecar_clause`` for a label known only at fire time.

    ``label_js`` is a JS expression; workflow and run_key are validated and slugged here.
    """
    prefix = sidecar_dir(workflow, run_key, "x")[: -len("x/")]
    head = json.dumps(f"{_CLAUSE_HEAD}{SIDECAR_DIR_MARKER_PREFIX}{prefix}")
    return f"({head} + {_JS_SLUG_EXPR.format(x=label_js)} + '/')"

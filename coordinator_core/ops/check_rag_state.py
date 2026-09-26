
from __future__ import annotations

import os
import sys
from typing import Optional, Tuple

from coordinator_core.data_root import content_root_for
from coordinator_core.trusted_root_guard import is_trusted as _is_trusted_root

_VALID_TOKENS = ("absent", "stale", "fresh")
_SITE_LABEL = "coordinator/bin/check-rag-state.sh"


def _claude_home() -> str:
    """Mirror the bash oracle's `${CLAUDE_HOME:-$HOME}/.claude`."""
    base = (
        os.environ.get("CLAUDE_HOME")
        or os.environ.get("HOME")
        or os.environ.get("USERPROFILE")
        or os.path.expanduser("~")
    )
    return os.path.join(base, ".claude")


def _read_doe_root(claude_home: str) -> str:
    doe_root_file = os.path.join(claude_home, ".doe-root")
    try:
        with open(doe_root_file, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        print(f"skip: _read_doe_root: with open(doe_root_file, encoding=\"utf-8\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""


def check_rag_state() -> Tuple[str, int]:
    claude_home = _claude_home()
    doe_root = _read_doe_root(claude_home)
    content_root = content_root_for(doe_root)
    if content_root is None:
        return ("", 1)

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT") or str(content_root)

    if not _is_trusted_root(plugin_root):
        return ("", 1)

    marker_path = os.environ.get("CLAUDE_RAG_STATE_FILE") or os.path.join(
        plugin_root, "tasks", ".rag-state"
    )

    # 1. env var fast-path — a caller or hook may inject RAG_STATE directly.
    rag_state = os.environ.get("RAG_STATE", "")
    if rag_state in _VALID_TOKENS:
        return (rag_state, 0)
    if rag_state == "unknown":
        return ("unknown", 1)

    if os.path.isfile(marker_path):
        try:
            with open(marker_path, encoding="utf-8") as fh:
                first_line = fh.readline()
        except OSError:
            first_line = ""
        state = "".join(first_line.split())
        if state in _VALID_TOKENS:
            return (state, 0)
        if state == "unknown":
            return ("unknown", 1)

    return ("unknown", 1)


def _doe_root_error(doe_root: str) -> Optional[str]:
    if content_root_for(doe_root) is not None:
        return None
    return (
        "ERROR: ~/.claude/.doe-root missing/invalid — re-run "
        "python3 <claude-klabauter>/scripts/setup.py"
    )


def _trust_error(plugin_root: str) -> str:
    return (
        f"ERROR: {_SITE_LABEL} '{plugin_root}' outside trusted prefix — refusing to "
        "source; re-run python3 <claude-klabauter>/scripts/setup.py (or set "
        "COORDINATOR_PLUGIN_ROOT_TRUSTED=1 for a sanctioned --plugin-dir spike)"
    )


def main(argv) -> int:  # noqa: ARG001 — takes no arguments, mirrors bash oracle
    claude_home = _claude_home()
    doe_root = _read_doe_root(claude_home)

    err = _doe_root_error(doe_root)
    if err is not None:
        print(err, file=sys.stderr)
        return 1

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT") or str(content_root_for(doe_root))
    if not _is_trusted_root(plugin_root):
        print(_trust_error(plugin_root), file=sys.stderr)
        return 1

    text, rc = check_rag_state()
    print(text)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

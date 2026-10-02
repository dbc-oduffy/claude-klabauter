
"""Python port of the bash `check-rag-state.sh` oracle: resolves the example-retrieval-repo content root, checks it is a trusted plugin root, and reads the persisted RAG state marker (absent/stale/fresh)."""

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


def _read_content_root(claude_home: str) -> str:
    from coordinator_core.content_root import POINTER_NAME, _LEGACY_POINTER  # compat-fallback: private-name-ok

    for name in (POINTER_NAME, _LEGACY_POINTER):
        try:
            with open(os.path.join(claude_home, name), encoding="utf-8") as fh:
                value = fh.read().strip()
        except OSError:
            continue
        if value:
            return value
    return ""


def _rag_configured() -> bool:
    """A RAG is configured when a state override is injected or the registry names one."""
    if os.environ.get("RAG_STATE") or os.environ.get("CLAUDE_RAG_STATE_FILE"):
        return True
    from coordinator_core.machine_resolver import registry_get

    try:
        return bool(registry_get("repos.project_rag"))
    except Exception:  # noqa: BLE001 -- unreadable registry means not configured
        return False


def _skip_on_consumer() -> bool:
    from coordinator_core.machine_profile import machine_profile

    return machine_profile() != "author" and not _rag_configured()


def check_rag_state() -> Tuple[str, int]:
    if _skip_on_consumer():
        return ("", 1)
    claude_home = _claude_home()
    pointed_root = _read_content_root(claude_home)
    content_root = content_root_for(pointed_root)
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


def _content_root_error(pointed_root: str) -> Optional[str]:
    if content_root_for(pointed_root) is not None:
        return None
    return (
        "ERROR: ~/.claude/.coordinator-content-root missing/invalid — re-run "
        "python3 <claude-klabauter>/scripts/setup.py"
    )


def _trust_error(plugin_root: str) -> str:
    return (
        f"ERROR: {_SITE_LABEL} '{plugin_root}' outside trusted prefix — refusing to "
        "source; re-run python3 <claude-klabauter>/scripts/setup.py (or set "
        "COORDINATOR_PLUGIN_ROOT_TRUSTED=1 for a sanctioned --plugin-dir spike)"
    )


def main(argv) -> int:  # noqa: ARG001 — takes no arguments, mirrors bash oracle
    if _skip_on_consumer():
        return 1
    claude_home = _claude_home()
    pointed_root = _read_content_root(claude_home)

    err = _content_root_error(pointed_root)
    if err is not None:
        print(err, file=sys.stderr)
        return 1

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT") or str(content_root_for(pointed_root))
    if not _is_trusted_root(plugin_root):
        print(_trust_error(plugin_root), file=sys.stderr)
        return 1

    text, rc = check_rag_state()
    print(text)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

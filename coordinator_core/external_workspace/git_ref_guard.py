"""Body of the reference-transaction hook that refuses ref writes into registered external workspaces.

The hook is scoped to registered paths by the includeIf stanza (``vcs_guard_contract``); this
module re-checks the registry so an unreadable registry fails closed. A ref update outside
``refs/remotes/`` is refused unless ``TOKEN_ENV`` is set, which only the rehydrate op does.
"""

from __future__ import annotations

import os
import sys
from typing import Mapping, Optional, Sequence

from coordinator_core import machine_resolver
from coordinator_core.external_workspace.vcs_guard_contract import TOKEN_ENV

_ALLOWED_PREFIX = "refs/remotes/"


def _canon(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _contains(root: str, target: str) -> bool:
    return target == root or target.startswith(root.rstrip(os.sep) + os.sep)


def _writes_outside_remotes(stdin_text: str) -> bool:
    for line in stdin_text.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and not parts[2].startswith(_ALLOWED_PREFIX):
            return True
    return False


def decide(
    state: str,
    stdin_text: str,
    cwd: str,
    git_dir: Optional[str],
    env: Mapping[str, str],
) -> Optional[str]:
    """Return the one-line refusal for this transaction, or ``None`` to allow it.

    Allows when ``state`` is not ``prepared``, ``TOKEN_ENV`` is set, or every updated ref
    sits under ``refs/remotes/``. Otherwise refuses when the registry is unreadable or the
    repo (``cwd``, ``git_dir`` or its parent) lies inside a registered workspace path,
    existing or not.
    """
    if state != "prepared" or env.get(TOKEN_ENV):
        return None
    if not _writes_outside_remotes(stdin_text):
        return None
    if not machine_resolver.external_workspace_registry_readable():
        return "refused: external-workspace registry unreadable; fix it or use external_workspace.rehydrate"
    candidates = [_canon(cwd)]
    if git_dir:
        gd = _canon(os.path.join(cwd, git_dir))
        candidates += [gd, os.path.dirname(gd)]
    for key, entry in machine_resolver.external_workspace_registered_paths().items():
        root = _canon(entry["path"])
        if any(_contains(root, c) for c in candidates):
            return f"refused: git ref write in external workspace {key}; use external_workspace.rehydrate"
    return None


def main(argv: Sequence[str], stdin_text: str) -> int:
    """Hook entry: exit 0 to allow, 1 to refuse (message on stderr). Any failure refuses."""
    try:
        msg = decide(
            argv[1] if len(argv) > 1 else "",
            stdin_text,
            os.getcwd(),
            os.environ.get("GIT_DIR"),
            os.environ,
        )
    except Exception as exc:  # noqa: BLE001 — fail closed
        msg = f"refused: external-workspace guard error ({type(exc).__name__}); use external_workspace.rehydrate"
    if msg:
        sys.stderr.write(msg + "\n")
        return 1
    return 0

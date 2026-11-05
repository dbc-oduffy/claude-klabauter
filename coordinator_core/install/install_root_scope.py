"""coordinator_core.install.install_root_scope — is this install a redirected (sandbox) root?

Machine-global side effects (scheduler registration, warm-engine start) belong to the real
install only. This predicate compares the three root-redirecting env vars against the real
home defaults directly, never through ``_settings_home`` (those resolvers read the same env
and would always agree).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _canon(p: object) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def redirected_install_reason() -> Optional[str]:
    """Return a one-line reason naming the first redirected root var, or None.

    A var that is unset, empty, or resolves (realpath + normcase, both sides) to its
    default under ``Path.home()`` is not a redirect.
    """
    home = Path.home()
    defaults = (
        ("COORDINATOR_SETTINGS_HOME", home / ".coordinator-claude-settings"),
        ("CLAUDE_HOME", home),
        ("CLAUDE_CONFIG_DIR", home / ".claude"),
    )
    for var, default in defaults:
        value = os.environ.get(var)
        if not value:
            continue
        if _canon(value) != _canon(default):
            return f"{var}={value} is not the default {default}"
    return None


def unregistered_clone_reason(repo_root: object) -> Optional[str]:
    """Return a one-line reason when ``repo_root`` is not the registered engine root, or None.

    Real-settings-home installs from a second or moved clone have no env redirect, so this
    compares the clone running setup against the one ``repos.claude_klabauter`` registers
    (via ``resolve_engine_root_for_install``, never a second resolver). An unresolvable
    registry reads as unregistered: the machine-global job stays untouched.
    """
    from coordinator_core.install.engine_root_for_install import resolve_engine_root_for_install

    try:
        resolved = resolve_engine_root_for_install()
    except Exception:
        resolved = None
    registered = getattr(resolved, "root", None)
    if registered is None:
        return f"no registered engine root to compare against install root {repo_root}"
    if _canon(registered) != _canon(repo_root):
        return f"install root {repo_root} is not the registered engine root {registered}"
    return None

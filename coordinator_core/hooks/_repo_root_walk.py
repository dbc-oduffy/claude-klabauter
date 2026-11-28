"""Leaf: the nearest ancestor of a hook payload's cwd that holds a `.git` entry.

Split out of `nudge_unrouted_sizing` so a Bash guard that needs only this walk does not import that
module's session-scope closure on the cold hook path."""

from __future__ import annotations

import os


def repo_root(payload: dict) -> str | None:
    """The git work-tree root above `payload["cwd"]` (default: the process cwd), or None. Walks only."""
    cwd = payload.get("cwd") or os.getcwd()
    if not isinstance(cwd, str):
        return None
    probe = os.path.abspath(cwd)
    while True:
        if os.path.exists(os.path.join(probe, ".git")):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent

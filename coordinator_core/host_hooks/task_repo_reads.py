"""Refuse a grok read of files outside a task repository.

A headless comparison whose cwd is a scratch task repo spent the extra whole-EM
dollars reading sizing skills and coordinator plugin docs. Those paths are
outside the repo. A fleet source repo (CLAUDE.md, coordinator/, or
coordinator_core/ at the git root) still reads them.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.host_hooks.contract import HOST_GROK, HostEvent

_READ_TOOLS = frozenset({"read_file", "Read", "grep", "Grep"})
_PATH_KEYS = ("target_file", "file_path", "path")
_REASON = "Read only files inside this repository."


def _git_root(cwd: Path) -> Path | None:
    """The git root of `cwd`, resolved like the read target.

    macOS `/tmp` is a symlink to `/private/tmp`. An unresolved cwd and a
    resolved target are not `relative_to` each other, so an in-repo read
    would be denied.
    """
    if not cwd.is_absolute():
        return None
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            try:
                return candidate.resolve()
            except OSError:
                return None
    return None


def _fleet_source(root: Path) -> bool:
    return (root / "CLAUDE.md").is_file() or (root / "coordinator").is_dir() or (root / "coordinator_core").is_dir()


def _target(event: HostEvent) -> Path | None:
    raw = next((event.tool_input.get(key) for key in _PATH_KEYS if event.tool_input.get(key)), None)
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path(event.cwd) / path
    try:
        return path.resolve()
    except OSError:
        return None


def match(event: HostEvent) -> dict | None:
    """A grok deny when `event` reads outside a non-fleet git repo, else None."""
    if event.host != HOST_GROK or event.tool_name not in _READ_TOOLS:
        return None
    root = _git_root(Path(event.cwd)) if event.cwd else None
    if root is None or _fleet_source(root):
        return None
    target = _target(event)
    if target is None:
        return None
    try:
        target.relative_to(root)
    except ValueError:
        return {"permissionDecision": "deny", "permissionDecisionReason": _REASON}
    return None

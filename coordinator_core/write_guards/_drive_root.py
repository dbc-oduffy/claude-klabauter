"""Shared predicate for the drive-root and home new-top-level-entry guards.

Underscore-prefixed so `engine._discover_guards` skips it. Stdlib plus the path seam in
`bash_guards._write_bump_sink_shapes`; spawns nothing, never walks a directory, costs one
`os.path.lexists`. A shell-parsed target set cannot see writes made inside a script, an
interpreter body or `xargs` (DR-258), so this is a speedbump and the Stop-time leg stays the backstop.
"""

from __future__ import annotations

import os
import re
from typing import Any, Mapping, Optional, Tuple

from coordinator_core.bash_guards import _write_bump_sink_shapes as _sinks
from coordinator_core.bash_guards._rewrite_support import _override
from coordinator_core.write_guards._home_root import home_roots

OVERRIDE_KEY = "COORDINATOR_ALLOW_DRIVE_ROOT_NEW_DIR"
_ROOTS_KEY = "COORDINATOR_DRIVE_ROOTS"


def configured_drive_roots(env: Optional[Mapping[str, str]] = None) -> Tuple[str, ...]:
    """`COORDINATOR_DRIVE_ROOTS` split on `os.pathsep`, or `()` when unset.

    `env` is the caller's mapping; `os.environ` is read only when the caller passes none.
    """
    source = os.environ if env is None else env
    raw = source.get(_ROOTS_KEY) or ""
    return tuple(r for r in raw.split(os.pathsep) if r)


def is_checkout_or_link(path: str) -> bool:
    """True for a symlink, a junction, or a directory holding `.git`."""
    try:
        if os.path.islink(path) or getattr(os.path, "isjunction", lambda _p: False)(path):
            return True
        return os.path.exists(os.path.join(path, ".git"))
    except OSError:
        return False


def override_active(payload: Optional[Mapping[str, Any]]) -> bool:
    """True when the caller's env (`payload["env"]` first, ambient `os.environ` as fallback) sets the override to 1."""
    return _override(OVERRIDE_KEY, payload=payload if isinstance(payload, dict) else None)


def deny_reason(name: str) -> str:
    """Refusal text: the entry's leaf name only (no absolute path, no override key) plus the alternative."""
    return (
        f"DENY: '{name}' would be a new top-level folder on a drive root or in the home dir. "
        "Instead: write under <repo>/scratch/ (tests: tmp_path)."
    )


def _first_below(nt, np: str, nn: str, root: str) -> Optional[Tuple[str, str]]:
    """(root as spelled in `np`, first component below it) when `nn` is strictly under `root`, else None."""
    sep = nt.sep
    base = root if root.endswith(sep) else root + sep
    if len(nn) <= len(base) or not nn.startswith(base):
        return None
    rest = np[len(base):]
    name = rest.split(sep, 1)[0]
    return (np[: len(base)], name) if name else None


_MSYS_DRIVE_SHAPE = re.compile(r"^/[A-Za-z](?:/|$)")


def new_top_level_entry(
    target: str,
    cwd: Optional[str],
    *,
    bare_slash_is_drive_root: bool = False,
    env: Optional[Mapping[str, str]] = None,
    interpreter: bool = False,
) -> Optional[str]:
    """Leaf name of the new drive-root or home top-level entry `target` would create, else None.

    A target directly under home is the home-root pair's case and returns None here. Fails open.
    `interpreter=True` (a Windows-native interpreter resolves `/x/...` on the cwd drive, MSYS does
    not) also judges the cwd-drive reading `<cwd drive>:\\x\\...` and flags either reading.
    """
    hit = _new_top_level_entry(
        target, cwd, bare_slash_is_drive_root=bare_slash_is_drive_root, env=env
    )
    if hit or not interpreter:
        return hit
    try:
        stripped = (target or "").strip()
        if not (_sinks._host_is_windows() and _MSYS_DRIVE_SHAPE.match(stripped)):
            return None
        cwd_native = _sinks.translate_msys_path(cwd) if cwd else None
        drive = _sinks._native_path().splitdrive(cwd_native or "")[0]
        if not drive or drive.startswith("\\\\") or drive[0].upper() == stripped[1].upper():
            return None
        alt = drive + "\\" + stripped.lstrip("/").replace("/", "\\")
        return _new_top_level_entry(alt, cwd, bare_slash_is_drive_root=False, env=env)
    except Exception:
        return None


def _new_top_level_entry(
    target: str,
    cwd: Optional[str],
    *,
    bare_slash_is_drive_root: bool = False,
    env: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    try:
        if not target or not target.strip():
            return None
        target = target.strip()
        nt = _sinks._native_path()
        windows = _sinks._host_is_windows()
        base_cwd = cwd or ""
        if (
            windows
            and bare_slash_is_drive_root
            and target.startswith("/")
            and not target.startswith("//")
            and _sinks.translate_msys_path(target) is None
        ):
            cwd_native = _sinks.translate_msys_path(base_cwd) if base_cwd else None
            drive = nt.splitdrive(cwd_native or "")[0]
            if not drive or drive.startswith("\\\\"):
                return None
            target = drive + "\\" + target.lstrip("/")
        resolved = _sinks.resolve_relative(base_cwd, target)
        if not resolved or not nt.isabs(resolved):
            return None
        np = nt.normpath(resolved)
        nn = nt.normcase(np)

        for home in home_roots(env):
            hit = _first_below(nt, np, nn, nt.normcase(nt.normpath(home)))
            if hit is None:
                continue
            root, name = hit
            if nn[len(root):] == nt.normcase(name):
                return None
            return None if os.path.lexists(nt.join(root, name)) else name

        configured = configured_drive_roots(env)
        if configured:
            for r in configured:
                hit = _first_below(nt, np, nn, nt.normcase(nt.normpath(r)))
                if hit is not None:
                    break
            else:
                return None
        else:
            drive, tail = nt.splitdrive(np)
            if drive.startswith("\\\\") or (windows and not drive):
                return None
            hit = _first_below(nt, np, nn, nt.normcase(drive + nt.sep))
            if hit is None:
                return None
        root, name = hit
        return None if os.path.lexists(nt.join(root, name)) else name
    except Exception:
        return None

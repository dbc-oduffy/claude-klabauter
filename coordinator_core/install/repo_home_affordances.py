"""repo_home install-time affordances: shortcuts to key homes and a Dev Drive advisory.

Both are advisory: no function here denies or raises on a recoverable condition.
Shortcuts are junctions (symlinks off Windows) made through `junction.py`; the Dev Drive
probe is a ctypes volume query. Neither spawns a process.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from coordinator_core.install import junction
from coordinator_core.machine_resolver import SettingsKeyMissing, require_key

REPO_HOME_KEY = "coordinator.repo_home"

CREATED = "created"
EXISTS = "exists"
CONFLICT = "conflict"
TARGET_MISSING = "target-missing"
REPO_HOME_MISSING = "repo-home-missing"
FAILED = "failed"

_FSCTL_QUERY_PERSISTENT_VOLUME_STATE = 0x0009023C
_PERSISTENT_VOLUME_STATE_DEV_VOLUME = 0x00002000


@dataclass(frozen=True)
class ShortcutResult:
    """Outcome for one shortcut: `status` is one of the module status constants."""

    name: str
    link: Path
    target: Path
    status: str
    detail: str = ""


def resolve_repo_home() -> Path:
    """repo_home from the `coordinator.repo_home` settings-home key.

    Raises `SettingsKeyMissing` (its `.key` names `coordinator.repo_home`) when unset.
    """
    return Path(require_key(REPO_HOME_KEY))


def ensure_repo_home_shortcuts(
    repo_home: Path | str, targets: Mapping[str, Path | str]
) -> list[ShortcutResult]:
    """Ensure `repo_home/<name>` links to each `targets[name]`; never clobbers.

    An existing link already pointing at the target is `exists`; anything else at the
    link path is `conflict`. A missing repo_home or target is reported per entry, not
    created. Idempotent.
    """
    home = Path(repo_home)
    results: list[ShortcutResult] = []
    for name, raw_target in targets.items():
        target = Path(raw_target)
        link = home / name
        if not home.is_dir():
            results.append(ShortcutResult(name, link, target, REPO_HOME_MISSING, f"{home} is not a directory"))
            continue
        if not target.exists():
            results.append(ShortcutResult(name, link, target, TARGET_MISSING, f"{target} does not exist"))
            continue
        if os.path.lexists(link):
            current = junction.junction_target(link)
            if current is not None and _same(current, target):
                results.append(ShortcutResult(name, link, target, EXISTS))
            else:
                results.append(ShortcutResult(name, link, target, CONFLICT, f"{link} exists and is not a link to {target}"))
            continue
        try:
            junction.create_junction(link, target)
        except (OSError, junction.JunctionUnsupported) as exc:
            results.append(ShortcutResult(name, link, target, FAILED, f"{type(exc).__name__}: {exc}"))
            continue
        results.append(ShortcutResult(name, link, target, CREATED))
    return results


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _is_windows() -> bool:
    return os.name == "nt"


def _volume_probe(root: str) -> tuple[str | None, bool | None]:
    """(filesystem name, Dev Drive flag) for volume `root` (drive letter plus backslash); each None when unreadable."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fs_buf = ctypes.create_unicode_buffer(64)
    ok = kernel32.GetVolumeInformationW(
        ctypes.c_wchar_p(root), None, 0, None, None, None, fs_buf, len(fs_buf)
    )
    fs_name = fs_buf.value if ok else None

    flag: bool | None = None
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(
        ctypes.c_wchar_p("\\\\.\\" + root.rstrip("\\")), 0, 0x7, None, 3, 0, None
    )
    if handle not in (None, wintypes.HANDLE(-1).value, -1):
        try:
            buf = (wintypes.ULONG * 4)(0, _PERSISTENT_VOLUME_STATE_DEV_VOLUME, 1, 0)
            returned = wintypes.DWORD(0)
            if kernel32.DeviceIoControl(
                wintypes.HANDLE(handle), _FSCTL_QUERY_PERSISTENT_VOLUME_STATE,
                buf, ctypes.sizeof(buf), buf, ctypes.sizeof(buf), ctypes.byref(returned), None,
            ):
                flag = bool(buf[0] & _PERSISTENT_VOLUME_STATE_DEV_VOLUME)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(handle))
    return fs_name, flag


def dev_drive_advisory(repo_home: Path | str) -> str | None:
    """One-line advisory when repo_home is not on a Dev Drive; None when it is, or off Windows.

    When the Dev Drive flag is unreadable the verdict falls back to the filesystem
    (ReFS or not) and the line says so; an unreadable volume yields None.
    """
    if not _is_windows():
        return None
    drive, _ = os.path.splitdrive(str(repo_home))
    if not drive or drive.startswith("\\\\"):
        return None
    root = drive + "\\"
    try:
        fs_name, flag = _volume_probe(root)
    except (OSError, AttributeError, ValueError):
        return None
    if flag is True:
        return None
    if flag is False:
        return (
            f"repo_home {repo_home} is not on a Dev Drive "
            f"(volume {drive} is {fs_name or 'unknown filesystem'}; Dev Drive flag read)"
        )
    if fs_name is None or fs_name.upper() == "REFS":
        return None
    return (
        f"repo_home {repo_home} is not on a Dev Drive "
        f"(filesystem is not ReFS: {fs_name}; Dev Drive flag unreadable via ctypes)"
    )


__all__ = [
    "REPO_HOME_KEY",
    "ShortcutResult",
    "SettingsKeyMissing",
    "dev_drive_advisory",
    "ensure_repo_home_shortcuts",
    "resolve_repo_home",
]

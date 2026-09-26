"""
coordinator_core.install.junction — Windows junction/reparse-point primitives
with a POSIX symlink branch (C1 of the fleet-env junction-publication plan).

Purpose: gives `fleet_env.py`'s publication path (C2 onward) a way to swap
`env_root` between sibling generation directories WITHOUT ever renaming or
removing a real directory that a fleet reader may hold a plain `open()`
handle inside — see
`docs/plans/2026-08-20-the-fleet-env-publishes-through-a-juncti.md` for why
that rename fails under Windows fleet load (`WinError 5`).

THE TRAP THIS MODULE EXISTS TO CLOSE — negative spec, measured on this host
(Python 3.13.1, Windows, `os.name == "nt"`), not inferred:

  - For a junction, `os.path.islink()` and `pathlib.Path.is_symlink()` both
    return FALSE, while `os.path.isdir()` returns TRUE. A junction reads as
    an ordinary directory to every islink-based check.
  - `shutil.rmtree(junction)` independently REFUSES with
    `OSError: Cannot call rmtree on a symbolic link` — and leaves the
    target payload untouched when it does.
  - So the obvious guard `if os.path.islink(p): os.rmdir(p) else:
    shutil.rmtree(p)` takes the WRONG branch on a junction (islink is
    False, so it falls to rmtree) and then raises. There is no islink-based
    way to write this guard correctly.
  - The correct discriminator is
    `os.lstat(p).st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT`
    (observed value 2684354563 on this host). `is_junction` below uses
    exactly this test, never `islink()`.

AC2 — the stdlib dependency pin. `_winapi.CreateJunction` is a PRIVATE
CPython API (no leading-underscore-free public equivalent exists). Its
argument order is `(target, link)` — verified by running it on this host,
not assumed from the name. `coordinator_core/install/tests/test_junction.py`
asserts it is present and callable on `nt` and FAILS LOUDLY if it is absent;
that assertion must never be written as a skip. Without it, `create_junction`
on `nt` REFUSES outright (raises `JunctionUnsupported`) rather than silently
falling back to the directory-rename path this module exists to replace — a
silent fallback at fleet-rebuild time is exactly the failure mode a dropped
private API would otherwise reintroduce.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Callable, Optional

#: NEGATIVE SPEC — forcing the `nt` branch of these primitives on a POSIX
#: Windows-only API surface (`_winapi`, `stat.IO_REPARSE_TAG_MOUNT_POINT`)
#: stat.IO_REPARSE_TAG_MOUNT_POINT` raises `AttributeError` on POSIX
#: (`getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)`) rather than a bare
_host_is_nt_override: "Optional[Callable[[], bool]]" = None


def _host_is_nt() -> bool:
    if _host_is_nt_override is not None:
        return _host_is_nt_override()
    return os.name == "nt"


class JunctionUnsupported(RuntimeError):
    pass


def create_junction(link: "os.PathLike[str] | str", target: "os.PathLike[str] | str") -> None:
    target_str = str(Path(target))
    link_str = str(Path(link))
    if _host_is_nt():
        try:
            import _winapi
        except ImportError as exc:  # pragma: no cover - stdlib absence
            raise JunctionUnsupported(
                "_winapi is unavailable on this nt interpreter; refusing "
                "rather than falling back to a directory rename"
            ) from exc
        create = getattr(_winapi, "CreateJunction", None)
        if create is None:
            raise JunctionUnsupported(
                "_winapi.CreateJunction is absent from this stdlib build; "
                "refusing rather than falling back to a directory rename"
            )
        create(target_str, link_str)
    else:
        os.symlink(target_str, link_str, target_is_directory=True)


def is_junction(path: "os.PathLike[str] | str") -> bool:
    p = Path(path)
    if _host_is_nt():
        try:
            st = os.lstat(p)
        except OSError:
            return False
        mount_point_tag = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)
        return bool(mount_point_tag is not None and getattr(st, "st_reparse_tag", 0) == mount_point_tag)
    return p.is_symlink()


def remove_junction(path: "os.PathLike[str] | str") -> None:
    p = Path(path)
    if _host_is_nt():
        os.rmdir(p)
    else:
        os.unlink(p)


def junction_target(path: "os.PathLike[str] | str") -> Path | None:
    p = Path(path)
    if not is_junction(p):
        return None
    if _host_is_nt():
        raw = os.readlink(p)
        if raw.startswith("\\\\?\\"):
            raw = raw[4:]
        return Path(raw)
    return Path(os.readlink(p))

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

#: Test-injectable platform predicate for every `nt`-vs-posix branch in this
#: module, including its own four filesystem primitives (`create_junction`,
#: `is_junction`, `remove_junction`, `junction_target`) as well as
#: `fleet_env._is_transient_rename_failure`, which imports this rather than
#: re-deriving its own platform read. A test overrides this module attribute
#: (`monkeypatch.setattr(junction, "_host_is_nt", lambda: True)`) to exercise
#: the `nt` branch WITHOUT flipping the process-global `os.name` — flipping
#: `os.name` process-wide makes every `pathlib.Path(...)` constructed
#: anywhere in the same process for the rest of the test (including inside
#: unrelated library code) pick `WindowsPath`, so `str()` of any such path
#: silently turns `/` into `\`. That corrupted a lock-file path built from
#: `str(Path(...))` in `fleet_env.py`, which was then opened relative to the
#: POSIX cwd and created a stray backslash-named file at the repo root —
#: the xdist-parallel (`popen-gw2`) sighting this seam fixes. Production
#: behaviour is byte-identical to reading `os.name` directly — this is a
#: redirection point for tests only, never a runtime override.
#:
#: NEGATIVE SPEC — forcing the `nt` branch of these primitives on a POSIX
#: interpreter does NOT simulate a Windows filesystem; it can only reach real
#: Windows-only API surface (`_winapi`, `stat.IO_REPARSE_TAG_MOUNT_POINT`)
#: that genuinely does not exist there (measured on this host: `import stat;
#: stat.IO_REPARSE_TAG_MOUNT_POINT` raises `AttributeError` on POSIX
#: CPython). `test_fleet_env_cutover.py` proves this seam is already
#: exercised with `_host_is_nt` patched True on a POSIX runner while calling
#: `is_junction` on a real (non-reparse) directory
#: (`test_retry_exhausted_raises_named_remediation_and_mutates_nothing`,
#: `test_restore_under_load_goes_through_bounded_retry`,
#: `test_restore_retry_exhausted_names_generation_dir_and_absent_state`) — so
#: `is_junction` reads the reparse-tag constant defensively
#: (`getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)`) rather than a bare
#: module attribute: on real `nt` this is byte-identical (the constant always
#: exists there), and on POSIX-with-forced-`nt` it degrades to "not a
#: junction" instead of raising, which is also the factually correct answer —
#: a real POSIX directory is never an `nt` reparse point no matter which
#: predicate a test forces. `create_junction`'s forced-`nt`-on-POSIX case
#: already has a named outcome that is not a crash: `import _winapi` fails
#: with `ImportError`, which the function turns into `JunctionUnsupported`
#: (see that class) rather than propagating a raw `AttributeError`. Directly
#: faking Windows reparse-point *behaviour* (e.g. a POSIX symlink reporting a
#: `st_reparse_tag`) is out of contract for this seam — the four primitives
#: are a fact about the filesystem the process stands on, and their
#: Windows-primitive coverage comes from running on Windows, never from this
#: attribute.
_host_is_nt_override: "Optional[Callable[[], bool]]" = None


def _host_is_nt() -> bool:
    """True iff the CURRENT process is running on `nt`. Delegates to
    `os.name == "nt"` unless a test has installed an override via
    `_host_is_nt_override` — see that name's docstring."""
    if _host_is_nt_override is not None:
        return _host_is_nt_override()
    return os.name == "nt"


class JunctionUnsupported(RuntimeError):
    """Raised on `nt` when `_winapi.CreateJunction` is unavailable.

    There is no silent fallback to a directory rename — that rename is the
    defect this module exists to close (see module docstring). A caller
    that hits this must treat it as a hard stop, not a degrade.
    """


def create_junction(link: "os.PathLike[str] | str", target: "os.PathLike[str] | str") -> None:
    """Create `link` as a junction (nt) or directory symlink (posix) pointing at `target`.

    `target` must exist and be a directory; `link` must not already exist.
    Argument order to the underlying `_winapi.CreateJunction(target, link)`
    call is target-first — verified on this host, see module docstring.
    """
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
    """True if `path` is a junction/reparse mount point (nt) or symlink (posix).

    Reparse-tag based on `nt`, NEVER `os.path.islink()` /
    `Path.is_symlink()` — both read False for a junction. See module
    docstring's negative-spec block.
    """
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
    """Remove the junction/symlink at `path`, never the target it points at.

    `nt`: `os.rmdir` — removes the reparse point only; the target directory
    and its contents are untouched (measured). `shutil.rmtree` must NEVER be
    used here — it refuses on a junction and would be a bug even if it
    didn't, since the intent is link removal, not target removal.
    posix: `os.unlink`.
    """
    p = Path(path)
    if _host_is_nt():
        os.rmdir(p)
    else:
        os.unlink(p)


def junction_target(path: "os.PathLike[str] | str") -> Path | None:
    """Return the resolved target of the junction/symlink at `path`, or None.

    None when `path` does not exist or is not a junction/symlink (per
    `is_junction`) — never raises for that case.
    """
    p = Path(path)
    if not is_junction(p):
        return None
    if _host_is_nt():
        raw = os.readlink(p)
        if raw.startswith("\\\\?\\"):
            raw = raw[4:]
        return Path(raw)
    return Path(os.readlink(p))

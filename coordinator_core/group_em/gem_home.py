"""Machine-local group-em home, resolve-only.

Port of coordinator-content-repo@2c24f601e7 `coordinator/hooks/scripts/_gem_home.py` (resolve path only:
`ensure_home`, `migrate_legacy_home` and `gem_holder` are not ported). A change to either
side is made to both. Zero spawn, zero write: registry TOML is read directly.

Also carries the G-EM memo identity (A-GROUP-EM-SENDS-MEMOS-AND-NEVER-RECEIVES-THEM): the
home signs as SENDER_ID and never receives; RECEIVER_IDS name the addresses that reroute.
`machine_resolver` imports this module lazily to avoid a cycle.
"""

from __future__ import annotations

import os
from pathlib import Path

from coordinator_core import machine_resolver
from coordinator_core._settings_home import settings_home

KEY = "coordinator.group_em_home"
LEGACY_KEY = "coordinator.general_work_home"
DEFAULT_DIRNAME = "group-em"
LEGACY_DIRNAME = "general-work"
SENDER_ID = "group-em-em"
RECEIVER_IDS = frozenset({SENDER_ID, "general-work-em"})


class HomeRefused(Exception):
    """The resolved home sits inside a checkout; `.checkout` names it."""

    def __init__(self, home: str, checkout: str):
        self.home = home
        self.checkout = checkout
        super().__init__(f"group-em home {home} is inside checkout {checkout}")


def _norm(p) -> str:
    return os.path.normcase(os.path.realpath(str(p)))


def contains(root, path) -> bool:
    """True when `path` is `root` or lies under it, compared on normalised real paths."""
    r, p = _norm(root), _norm(path)
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def inside_checkout(path) -> str | None:
    """The checkout `path` lies in, else None: the first strict ancestor holding `.git`, or
    the first `repos.*` registry root equal to or containing it."""
    target = _norm(path)
    for anc in Path(target).parents:
        if (anc / ".git").exists():
            return str(anc)
    for key, val in machine_resolver.merged_flat_registry().items():
        if key.startswith("repos.") and isinstance(val, str) and val and contains(val, target):
            return val
    return None


def resolve_home() -> Path:
    """The group-em home: configured KEY, else LEGACY_KEY, else `<settings-home>/group-em`
    (the legacy dirname only when the default is absent and the legacy dir exists).
    Raises HomeRefused when it lies inside a checkout."""
    flat = machine_resolver.merged_flat_registry()
    # registry_value returns "" for an empty declaration; that reads as unset.
    configured = (
        machine_resolver.registry_value(KEY, flat)
        or machine_resolver.registry_value(LEGACY_KEY, flat)
    )
    if configured:
        home = Path(configured)
    else:
        base = settings_home()
        home = base / DEFAULT_DIRNAME
        if not home.exists() and (base / LEGACY_DIRNAME).exists():
            home = base / LEGACY_DIRNAME
    hit = inside_checkout(home)
    if hit:
        raise HomeRefused(str(home), hit)
    return home


def is_home(path) -> bool:
    """True when `path` is the home or under it. A refused home is never a home."""
    try:
        return contains(resolve_home(), path)
    except HomeRefused:
        return False


def is_group_em_receiver(receiver: str) -> bool:
    """True when `receiver`, stripped and lowercased, is a G-EM receiver id."""
    return receiver.strip().lower() in RECEIVER_IDS


def home_inbox_dirs(home: Path) -> tuple[Path, ...]:
    """The home's inbox directories: current layout first, then the legacy one."""
    return (home / "state" / "cross-repo" / "inbox", home / "cross-repo" / "inbox")

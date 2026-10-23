"""Machine-local group-em home, resolve-only.

Port of coordinator-content-repo `coordinator/hooks/scripts/_gem_home.py`. A change to either side is made
to both. The resolve path is zero spawn, zero write (registry TOML read directly); only
`ensure_home` writes, and spawns `git init`.

Also carries the G-EM memo identity (A-GROUP-EM-SENDS-MEMOS-AND-NEVER-RECEIVES-THEM): the
home signs as SENDER_ID and never receives; RECEIVER_IDS name the addresses that reroute.
`machine_resolver` imports this module lazily to avoid a cycle.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from coordinator_core import machine_resolver
from coordinator_core._settings_home import settings_home
from coordinator_core.session import record_homes

KEY = "coordinator.group_em_home"
LEGACY_KEY = "coordinator.general_work_home"
DEFAULT_DIRNAME = "group-em"
LEGACY_DIRNAME = "general-work"
SENDER_ID = "group-em-em"
RECEIVER_IDS = frozenset({SENDER_ID})
# Machine-local runtime state the home never commits.
_IGNORED = (".coordinator-local/",)


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


def _configured() -> str | None:
    flat = machine_resolver.merged_flat_registry()
    return (
        machine_resolver.registry_value(KEY, flat)
        or machine_resolver.registry_value(LEGACY_KEY, flat)
        or None
    )


def gem_root() -> str | None:
    rec = gem_holder()
    root = rec.get("repo_root") if rec else None
    return root if isinstance(root, str) and root else None


def _legacy_home_live(legacy: Path) -> bool:
    """True when anything runs in `legacy`. Fails closed: any error reads as live."""
    try:
        if contains(legacy, os.getcwd()):
            return True
        from coordinator_core.group_em import session_registry

        for row in session_registry.read_rows():
            if row.cwd and session_registry.pid_alive(row.pid) and contains(legacy, row.cwd):
                return True
        root = gem_root()
        return bool(root and contains(legacy, root))
    except Exception:
        return True


def migrate_legacy_home() -> str:
    """Rename the default-path `<settings-home>/general-work` to `group-em` when safe.

    Returns one of moved, no-legacy, configured, both-present, live, busy, refused. Only the
    default-path legacy dir moves; a configured home is never touched, neither key is written,
    and an existing target is never merged or overwritten. Never raises."""
    try:
        if _configured():
            return "configured"
        base = settings_home()
        legacy, target = base / LEGACY_DIRNAME, base / DEFAULT_DIRNAME
        if not legacy.exists():
            return "no-legacy"
        if target.exists():
            return "both-present"
        if inside_checkout(target):
            return "refused"
        if _legacy_home_live(legacy):
            return "live"
        os.rename(legacy, target)
        return "moved"
    except OSError:
        return "busy"
    except Exception:
        return "live"


def _ensure_ignored(home: Path, entries: tuple[str, ...] = _IGNORED) -> None:
    """Append any missing `entries` line to the home's `.gitignore`; never rewrites a line."""
    path = home / ".gitignore"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    missing = [entry for entry in entries if entry not in lines]
    if missing:
        path.write_text("\n".join([*lines, *missing]) + "\n", encoding="utf-8")


def default_template() -> Path | None:
    """The DoE-owned home-CLAUDE.md under the DoE content root, else None. Never raises."""
    try:
        from coordinator_core import content_root

        root = content_root.read_content_root()
    except Exception:
        return None
    if not root:
        return None
    for base in (Path(root), Path(root) / "coordinator"):
        cand = base / "templates" / "group-em-home" / "home-CLAUDE.md"
        if cand.is_file():
            return cand
    return None


def ensure_home(template: Path | None = None, refresh: bool = False) -> Path:
    """`ensure_home_outcome` for a caller that does not report the seed outcome."""
    return ensure_home_outcome(template, refresh)[0]


def ensure_home_outcome(template: Path | None = None, refresh: bool = False) -> tuple[Path, str]:
    """Migrate a legacy home when safe, then create the home, `git init` it, add `state/`, ignore
    `.coordinator-local/`, seed CLAUDE.md when absent (or on refresh, which overwrites it).

    The template defaults to `default_template()`. Outcome is one of seeded, kept (CLAUDE.md
    present, no refresh), no-template (none resolved or not a file; nothing written)."""
    migrate_legacy_home()
    home = resolve_home()
    home.mkdir(parents=True, exist_ok=True)
    if not (home / ".git").exists():
        subprocess.run(
            ["git", "init", "-q", str(home)],
            check=True,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    (home / "state").mkdir(exist_ok=True)
    _ensure_ignored(home)
    src = template or default_template()
    if src is None or not src.is_file():
        return home, "no-template"
    seed = home / "CLAUDE.md"
    if not refresh and seed.exists():
        return home, "kept"
    seed.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return home, "seeded"


def is_group_em_receiver(receiver: str) -> bool:
    """True when `receiver`, stripped and lowercased, is a G-EM receiver id."""
    return receiver.strip().lower() in RECEIVER_IDS


def gem_holder() -> dict | None:
    """The live box-wide G-EM holder record, else None. Never raises; writes nothing."""
    try:
        from coordinator_core.group_em import nomination

        rec = nomination.box_holder()
        if isinstance(rec, dict) and rec.get("live") is True:
            return rec
    except Exception:
        pass
    return None


def home_inbox_dirs(home: Path) -> tuple[Path, ...]:
    """The home's inbox directories: current layout first, then the legacy one."""
    return (
        Path(record_homes.home_dir(str(home), "cross-repo")) / "inbox",
        home / "cross-repo" / "inbox",
    )

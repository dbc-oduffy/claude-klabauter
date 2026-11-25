"""grok writer: one `[folders.'<checkout>']` trust table per checkout in <grok home>/trusted_folders.toml.

apply() appends only the tables that are missing and never rewrites existing bytes, so a folder
Grok already decided (trusted or not) is left as it is. The write is taken under Grok's own
`trusted_folders.toml.lock` (O_EXCL create, released after); a held lock skips with a note and
writes nothing. A lock file this writer did not create is taken with an advisory lock and left in
place; no lock is ever removed by age.
A file that is unreadable or not valid TOML is reported malformed and left byte-identical.
Works with no grok CLI on the box: the home comes from host_registry.
"""
from __future__ import annotations

import os
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Optional

from coordinator_core.install import host_registry
from coordinator_core.install.host_install_agy import HostInstallRefused
from coordinator_core.install.write_surface import StaticClause, WriteSurfaceDeclaration, WriteSurfaceEntry

TRUSTED_FILE = "trusted_folders.toml"
LOCK_FILE = "trusted_folders.toml.lock"

#: mcp status values (field name mirrors host_install_agy.AgyApply so setup.py consumes both alike).
MCP_WRITTEN = "written"
MCP_CURRENT = "current"
MCP_WOULD_WRITE = "would-write"
MCP_MALFORMED = "malformed"
MCP_SKIPPED = "skipped"


@dataclass(frozen=True)
class GrokApply:
    """Outcome of one apply(): the status, the file it targets, and human-readable notes."""

    mcp: str
    config_path: Path
    notes: tuple[str, ...] = field(default_factory=tuple)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def default_checkouts(claude_klabauter_root: Optional[Path] = None) -> list[Path]:
    """The install's registered clones (registry.toml `repos.*` paths, then unset keys found beside
    the claude-klabauter root) plus the claude-klabauter root itself; existing directories only, de-duplicated."""
    from coordinator_core.install import repo_discovery

    root = Path(claude_klabauter_root) if claude_klabauter_root else Path(__file__).resolve().parents[2]
    found: list[Path] = [root]
    try:
        keys = repo_discovery.declared_repo_keys()
        for clone in repo_discovery.find_clones([root.parent], repo_discovery.wanted_for_keys(keys))[0].values():
            found.append(clone)
        found.extend(_registry_repo_paths())
    except Exception:  # noqa: BLE001 - best-effort: a discovery fault is dropped silently (no note), the claude-klabauter root still stages
        pass
    return [p for p in found if p.is_dir()]


def _registry_repo_paths() -> list[Path]:
    from coordinator_core import _settings_home
    from coordinator_core._fleet_names import _flatten
    from coordinator_core.shared_read import read_bytes_shared

    try:
        data = tomllib.loads(read_bytes_shared(_settings_home.machine_local_dir() / "registry.toml").decode("utf-8"))
    except (OSError, ValueError):
        return []
    return [Path(v) for k, v in _flatten(data) if k.startswith("repos.") and isinstance(v, str) and v]


def _read_state(path: Path, notes: list[str]) -> Optional[tuple[bytes, set[str]]]:
    """(raw bytes, existing folder keys), or None (note appended) when the file cannot be trusted."""
    if not path.exists():
        return b"", set()
    try:
        raw = path.read_bytes()
        doc = tomllib.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        notes.append(f"{path}: unreadable or not valid TOML ({exc}); left untouched")
        return None
    folders = doc.get("folders", {})
    if not isinstance(folders, dict):
        notes.append(f"{path}: folders is not a table; left untouched")
        return None
    return raw, set(folders)


def _try_lock(fd: int) -> bool:
    """Take a non-blocking advisory lock on fd; False when another holder has it."""
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _key(path: str) -> str:
    return os.path.normcase(path)


def _plan_tables(
    checkouts: Iterable[Path], existing: set[str], now: int, notes: list[str]
) -> list[str]:
    """Missing-table text per checkout."""
    seen = {_key(k) for k in existing}
    tables: list[str] = []
    for c in checkouts:
        try:
            p = str(Path(c).resolve())
        except OSError as exc:
            notes.append(f"{c}: cannot resolve ({exc}); skipped")
            continue
        if "'" in p or "\n" in p or "\r" in p:
            notes.append(f"{p}: path has a quote or newline a TOML literal key cannot carry; skipped")
            continue
        if _key(p) in seen:
            continue
        seen.add(_key(p))
        tables.append(f"[folders.'{p}']\ntrusted = true\ndecided_at = {now}\n")
    return tables


def apply(
    home: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    check: bool = False,
    platform: Optional[str] = None,
    checkouts: Optional[Iterable[Path]] = None,
    now: Optional[int] = None,
) -> GrokApply:
    """Trust each checkout in Grok's trusted_folders.toml; check=True writes nothing.

    `home` is the user home (the Grok dir is <home>/.grok, or GROK_HOME in env), as host_install_agy.
    """
    env = os.environ if env is None else env
    plat = platform or sys.platform
    spec = host_registry.spec("grok")
    if home is not None and not (spec.home_env and env.get(spec.home_env)):
        grok_dir = Path(home) / spec.home_default
    else:
        grok_dir = host_registry.home(spec, env, plat)
    path = grok_dir / TRUSTED_FILE
    notes: list[str] = []
    wanted = list(default_checkouts() if checkouts is None else checkouts)

    state = _read_state(path, notes)
    if state is None:
        return GrokApply(MCP_MALFORMED, path, tuple(notes))
    stamp = int(time.time()) if now is None else now
    tables = _plan_tables(wanted, state[1], stamp, notes)
    if not tables:
        return GrokApply(MCP_CURRENT, path, tuple(notes))
    if check:
        return GrokApply(MCP_WOULD_WRITE, path, tuple(notes))

    from coordinator_core.install import substrate as _substrate_mod

    # Trap: Grok's own state dir, outside the install territory, so the machine-mutation switch
    # must reach it before the lock is created; temp paths are allowed (tests root home in tmp_path).
    blocked = _substrate_mod._refuse_machine_mutation(str(path), what="grok trusted_folders write", check_temp_path=False)
    if blocked:
        raise HostInstallRefused(blocked)
    grok_dir.mkdir(parents=True, exist_ok=True)
    lock = grok_dir / LOCK_FILE
    created = False
    try:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            created = True
        except FileExistsError:
            fd = os.open(lock, os.O_RDWR)
    except OSError as exc:
        notes.append(f"deferred: cannot take {lock} ({exc}); {TRUSTED_FILE} not written")
        return GrokApply(MCP_SKIPPED, path, tuple(notes))
    if not _try_lock(fd):
        os.close(fd)
        if created:
            lock.unlink(missing_ok=True)
        notes.append(f"deferred: {lock} is held; {TRUSTED_FILE} not written (rerun setup to retry)")
        return GrokApply(MCP_SKIPPED, path, tuple(notes))
    try:
        # Re-read under the lock: a writer that held it before us may have added tables.
        state = _read_state(path, notes)
        if state is None:
            return GrokApply(MCP_MALFORMED, path, tuple(notes))
        raw, existing = state
        tables = _plan_tables(wanted, existing, stamp, [])
        if not tables:
            return GrokApply(MCP_CURRENT, path, tuple(notes))
        sep = b"" if not raw or raw.endswith(b"\n") else b"\n"
        lead = b"\n" if raw.strip() else b""
        _atomic_write(path, raw + sep + lead + "\n".join(tables).encode("utf-8"))
    finally:
        _unlock(fd)
        os.close(fd)
        # Trap: unlinking a lock another process has already opened lets a third writer create a fresh
        # lock and enter alongside it; _atomic_write keeps the file whole, a lost table is re-staged next run.
        if created:
            lock.unlink(missing_ok=True)
    return GrokApply(MCP_WRITTEN, path, tuple(notes))


WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="host-install-grok",
    source_module="coordinator_core.install.host_install_grok",
    clauses=(
        StaticClause(
            entries=(
                WriteSurfaceEntry(kind="file-path", path="<home>/.grok/trusted_folders.toml", reason="apply: (under $GROK_HOME when set) appends a trusted [folders.'<checkout>'] table per missing checkout"),
                WriteSurfaceEntry(kind="file-path", path="<home>/.grok/trusted_folders.toml.lock", reason="apply: advisory lock held across the write; the file is removed after only when apply created it"),
            ),
        ),
    ),
)
